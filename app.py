"""Railway webhook bot. One worker; ephemeral sessions and per-video temp files."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from core import Settings, extract_links, public_url
from downloader import DownloadProblem, DownloadedMedia, cleanup_ytdlp_runtime, fetch_video, update_ytdlp_runtime
from jobs import GlobalFIFO, Job

LOG = logging.getLogger("video-bot")
QUEUE_TASK_KEY = web.AppKey("queue_task", asyncio.Task)
SEEN_UPDATES = web.AppKey("seen_updates", OrderedDict)


def format_menu(token: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Видео 360p", callback_data=f"pick:{token}:v360"), InlineKeyboardButton(text="🎬 Видео 720p", callback_data=f"pick:{token}:v720")],
        [InlineKeyboardButton(text="✨ Видео 1080p", callback_data=f"pick:{token}:v1080")],
        [InlineKeyboardButton(text="🎵 Аудио MP3", callback_data=f"pick:{token}:mp3")],
    ])


def _link_word(count: int) -> str:
    remainder_10, remainder_100 = count % 10, count % 100
    if remainder_10 == 1 and remainder_100 != 11:
        return "ссылка"
    if remainder_10 in (2, 3, 4) and remainder_100 not in (12, 13, 14):
        return "ссылки"
    return "ссылок"


def _task_word(count: int) -> str:
    remainder_10, remainder_100 = count % 10, count % 100
    if remainder_10 == 1 and remainder_100 != 11:
        return "задача"
    if remainder_10 in (2, 3, 4) and remainder_100 not in (12, 13, 14):
        return "задачи"
    return "задач"


def cleanup_orphaned_tempdirs(root: Path | None = None) -> int:
    """Remove only the bot's own temporary job directories before serving updates."""
    temp_root = root or Path(tempfile.gettempdir())
    removed = 0
    for path in temp_root.glob("tg-video-*"):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError as exc:
            LOG.warning("orphan temp cleanup failed: %s", type(exc).__name__)
    return removed


async def upload_video(bot: Bot, job: Job, settings: Settings) -> None:
    progress = await bot.send_message(
        job.chat_id, "⏳ Начинаю подготовку файла…",
        reply_to_message_id=job.message_id,
    )

    async def activity() -> None:
        # Sends outbound packets during long downloads/FFmpeg work to prevent Railway
        # serverless from suspending a service in the middle of an active job.
        while True:
            await asyncio.sleep(25)
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=job.chat_id, action="upload_audio" if job.format_choice == "mp3" else "upload_video")

    task = asyncio.create_task(activity(), name="upload-keep-awake")
    try:
        with tempfile.TemporaryDirectory(prefix="tg-video-") as scratch:
            downloaded: DownloadedMedia = await fetch_video(job.url, Path(scratch), settings, job.format_choice)
            await progress.edit_text("📤 Почти готово — отправляю файл…")
            items = downloaded.items[:settings.max_items]
            quality = {"v360": " · 360p", "v720": " · 720p", "v1080": " · 1080p", "mp3": " · MP3"}.get(job.format_choice, "")
            if job.format_choice != "mp3" and items:
                actual_height = max((item.height for item in items), default=0)
                quality = f" · {actual_height}p" if actual_height else ""
            caption = f"{downloaded.title}{quality}\n🌐 {downloaded.source}"[:1024]
            reply = {"reply_to_message_id": job.message_id} if job.message_id is not None else {}
            if len(items) == 1:
                media = items[0]
                common = {"chat_id": job.chat_id, "caption": caption, "request_timeout": settings.upload_timeout_sec, **reply}
                if media.kind == "video":
                    options = {
                        **common, "video": FSInputFile(media.path, filename="video.mp4"),
                        "supports_streaming": True, "width": media.width, "height": media.height, "duration": media.duration,
                    }
                    if media.thumbnail:
                        options["thumbnail"] = FSInputFile(media.thumbnail)
                    await bot.send_video(**options)
                elif media.kind == "audio":
                    await bot.send_audio(audio=FSInputFile(media.path, filename="audio.mp3"), title=downloaded.title[:64], performer=downloaded.source[:64], **common)
                elif media.kind == "photo":
                    await bot.send_photo(photo=FSInputFile(media.path), **common)
                else:
                    await bot.send_document(document=FSInputFile(media.path), **common)
            else:
                telegram_media = []
                for index, media in enumerate(items):
                    item_caption = caption if index == 0 else None
                    if media.kind == "video":
                        telegram_media.append(InputMediaVideo(
                            media=FSInputFile(media.path, filename=f"video-{index + 1}.mp4"),
                            width=media.width or None, height=media.height or None,
                            duration=media.duration or None, caption=item_caption,
                            supports_streaming=True,
                        ))
                    else:
                        telegram_media.append(InputMediaPhoto(
                            media=FSInputFile(media.path), caption=item_caption,
                        ))
                await bot.send_media_group(
                    chat_id=job.chat_id, media=telegram_media,
                    request_timeout=settings.upload_timeout_sec, **reply,
                )
        # TemporaryDirectory cleans up even after failure; no retained video.
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("✅ Готово! Файл отправлен выше.")
    except DownloadProblem as exc:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text(f"⚠️ {exc}")
    except TelegramAPIError:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("Не получилось отправить файл. Попробуйте прислать ссылку ещё раз.")
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("⏹ Задача отменена.")
        raise
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def make_app(settings: Settings) -> web.Application:
    if settings.mode == "large":
        # API runs in a different container, so upload multipart bytes, NOT a local file path.
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.local_api_url, is_local=False), timeout=settings.upload_timeout_sec)
        bot = Bot(token=settings.token, session=session)
    else:
        bot = Bot(token=settings.token)

    async def process(job: Job) -> None:
        if not await public_url(job.url, settings.domains, settings.blocked_domains):
            await bot.send_message(
                job.chat_id, "⚠️ Эту ссылку нельзя безопасно открыть. Проверьте её и попробуйте другую.",
                reply_to_message_id=job.message_id,
            )
            return
        await upload_video(bot, job, settings)

    async def notify_job_error(job: Job) -> None:
        await bot.send_message(
            job.chat_id, "Не удалось обработать ссылку. Попробуйте ещё раз чуть позже.",
            reply_to_message_id=job.message_id,
        )

    worker = GlobalFIFO(
        limit=settings.max_queue,
        processor=process,
        on_error=notify_job_error,
    )
    router = Router()
    dp = Dispatcher()
    user_requests: dict[int, deque[float]] = defaultdict(deque)
    pending_choices: dict[int, tuple[Job, float]] = {}
    next_choice_token = 1

    def discard_expired_choices(now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        expired = [token for token, (_, created_at) in pending_choices.items() if current - created_at > 900]
        for token in expired:
            pending_choices.pop(token, None)

    def allowed(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in settings.allowed_user_ids)

    @router.message(Command("id"), F.chat.type == "private")
    async def command_id(message: Message) -> None:
        if message.from_user:
            await message.answer(f"Ваш Telegram ID: {message.from_user.id}")

    @router.message(Command("start"), F.chat.type == "private")
    async def command_start(message: Message) -> None:
        if allowed(message):
            await message.answer(
                "👋 Привет! Я помогу скачать видео.\n\n"
                "Отправьте ссылку, затем выберите качество видео или аудио MP3.\n\n"
                "📌 Отправляйте только контент, который вы вправе скачивать.",
            )
        else:
            user_id = message.from_user.id if message.from_user else "неизвестен"
            await message.answer(
                f"🔒 Доступ к боту ограничен.\nВаш Telegram ID: {user_id}\n"
                "Попросите владельца бота добавить ваш ID."
            )

    @router.message(Command("help"), F.chat.type == "private")
    async def command_help(message: Message) -> None:
        await message.answer(
            "📥 Как скачать видео\n"
            "1. Скопируйте ссылку на видео или публикацию.\n"
            "2. Отправьте её мне в личный чат.\n"
            "3. Выберите разрешение видео или MP3.\n"
            "4. Дождитесь готового файла.\n\n"
            "Можно отправить несколько ссылок одним сообщением. Задачи обрабатываются по очереди.",
        )

    @router.message(Command("status"), F.chat.type == "private")
    async def command_status(message: Message) -> None:
        if allowed(message):
            state = "⏳ Сейчас обрабатываю видео." if worker.active else "💤 Сейчас ничего не скачиваю."
            await message.answer(
                f"📊 В очереди: {worker.queue.qsize()} {_task_word(worker.queue.qsize())}.\n{state}",
            )
        else:
            await message.answer("🔒 Эта команда доступна только пользователям бота.")

    @router.message(Command("cancel"), F.chat.type == "private")
    async def command_cancel(message: Message) -> None:
        if allowed(message):
            removed, active = worker.cancel_chat(message.chat.id)
            pending = [token for token, (job, _) in pending_choices.items() if job.chat_id == message.chat.id]
            for token in pending:
                pending_choices.pop(token, None)
            messages = []
            if removed:
                messages.append(f"Убрал из очереди: {removed} {_link_word(removed)}")
            if active:
                messages.append("Текущую загрузку остановил")
            if pending:
                messages.append(f"Отменил выбор формата: {len(pending)} {_link_word(len(pending))}")
            result = "\n".join(messages) if messages else "У вас нет активных или ожидающих задач."
            await message.answer(f"🗑 {result}.")
        else:
            await message.answer("🔒 Эта команда доступна только пользователям бота.")

    @router.callback_query(F.data.startswith("pick:"))
    async def choose_format(callback: CallbackQuery) -> None:
        discard_expired_choices()
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer()
            return
        if callback.from_user.id not in settings.allowed_user_ids:
            await callback.answer("Доступ к боту ограничен.", show_alert=True)
            return
        try:
            _, raw_token, choice = callback.data.split(":", 2)
            token = int(raw_token)
        except (AttributeError, ValueError):
            await callback.answer("Эта кнопка устарела. Пришлите ссылку ещё раз.", show_alert=True)
            return
        if choice not in {"v360", "v720", "v1080", "mp3"}:
            await callback.answer("Неизвестный формат.", show_alert=True)
            return
        pending = pending_choices.pop(token, None)
        job = pending[0] if pending else None
        if not job or job.chat_id != callback.message.chat.id:
            await callback.answer("Эта ссылка уже обработана или устарела.", show_alert=True)
            return
        position = worker.add(Job(job.chat_id, job.url, job.message_id, choice))
        if position is None:
            await callback.message.edit_text("⌛ Очередь заполнена. Отправьте ссылку ещё раз чуть позже.")
            await callback.answer()
            return
        label = {"v360": "видео 360p", "v720": "видео 720p", "v1080": "видео 1080p", "mp3": "аудио MP3"}[choice]
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_text(f"✅ Выбрано: {label}. Ссылка добавлена в очередь.")
        await callback.answer()

    @router.message(F.chat.type == "private", F.text)
    async def message_text(message: Message) -> None:
        discard_expired_choices()
        if not message.from_user or not message.text:
            return
        if not allowed(message):
            await message.answer("🔒 Доступ к боту ограничен. Узнать свой ID можно командой /id.")
            return
        urls, invalid = extract_links(message.text, settings.domains, blocked=settings.blocked_domains)
        if not urls:
            await message.answer(
                "🔗 Не нашёл ссылку в сообщении. Отправьте публичную ссылку на видео или публикацию.",
            )
            return
        now = time.monotonic()
        rate_limited = False
        requests = user_requests[message.from_user.id]
        while requests and requests[0] <= now - 60:
            requests.popleft()
        for url in urls:
            if len(requests) >= settings.rate_limit_per_min:
                rate_limited = True
                break
            if len(pending_choices) + worker.queue.qsize() + int(worker.active) >= settings.max_queue:
                break
            requests.append(now)
            nonlocal next_choice_token
            token = next_choice_token
            next_choice_token += 1
            pending_choices[token] = (Job(chat_id=message.chat.id, url=url, message_id=message.message_id), now)
            host = urlsplit(url).hostname or "сайт"
            with contextlib.suppress(TelegramAPIError):
                await message.answer(
                    f"🎬 Выберите формат\n🌐 {host}\n\nКакой файл подготовить?",
                    reply_markup=format_menu(token),
                )
        accepted = sum(1 for job, _ in pending_choices.values() if job.message_id == message.message_id and job.chat_id == message.chat.id)
        info_lines = []
        if invalid:
            info_lines.append(f"⚠️ Не удалось распознать ссылок: {invalid}.")
        if rate_limited:
            limit = settings.rate_limit_per_min
            limit_word = "ссылку" if limit == 1 else _link_word(limit)
            info_lines.append(f"⏱ Лимит — не более {limit} {limit_word} в минуту.")
        elif accepted < len(urls):
            info_lines.append("⌛ Очередь сейчас заполнена. Попробуйте отправить оставшиеся ссылки позже.")
        info = "\n".join(info_lines)
        # If sending acknowledgement fails, do not return 5xx to Telegram:
        # otherwise Telegram retries the same update and duplicates queued URLs.
        if info:
            with contextlib.suppress(TelegramAPIError):
                await message.answer(info)

    @router.message(F.chat.type == "private")
    async def unknown_message(message: Message) -> None:
        if allowed(message):
            await message.answer(
                "💡 Отправьте ссылку на видео, и я подготовлю его для просмотра в Telegram.",
            )
        else:
            await message.answer("🔒 Доступ к боту ограничен. Узнать свой ID можно командой /id.")

    dp.include_router(router)

    async def start(bot: Bot) -> None:
        # One in-memory worker for all users: no overlapping download or upload.
        if settings.ytdlp_auto_update:
            await update_ytdlp_runtime(settings)
        app[QUEUE_TASK_KEY] = asyncio.create_task(worker.run(), name="global-single-worker")
        # No drop_pending_updates: avoids discarding user messages during cold start.
        # max_connections=1 preserves order of delivery from Telegram as much as API allows.
        info = await bot.get_webhook_info()
        if info.url != settings.webhook_url or info.max_connections != 1 or set(info.allowed_updates or []) != {"message", "callback_query"}:
            await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret, max_connections=1, allowed_updates=["message", "callback_query"])
        LOG.warning("Bot started in mode=%s; one global worker", settings.mode)

    async def stop(bot: Bot) -> None:
        task = app.get(QUEUE_TASK_KEY)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        cleanup_ytdlp_runtime()
        # Do NOT delete webhook: it must remain registered while sleeping.

    dp.startup.register(start)
    dp.shutdown.register(stop)

    app = web.Application(client_max_size=1 * 1024 * 1024)
    app[SEEN_UPDATES] = OrderedDict()

    async def deduplicate(handler, update, data):
        seen = app[SEEN_UPDATES]
        if update.update_id in seen:
            return None
        seen[update.update_id] = None
        if len(seen) > 2048:
            seen.popitem(last=False)
        return await handler(update, data)

    dp.update.outer_middleware(deduplicate)

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
        handle_in_background=True,
    ).register(app, path="/telegram-webhook")
    setup_application(app, dp, bot=bot)
    return app


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cleanup_orphaned_tempdirs()
    settings = Settings.from_env()
    web.run_app(make_app(settings), host="0.0.0.0", port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
