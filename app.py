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


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ℹ️ Как пользоваться", callback_data="menu:help")],
        [InlineKeyboardButton(text="📊 Очередь", callback_data="menu:status"), InlineKeyboardButton(text="❌ Отмена", callback_data="menu:cancel")],
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
        job.chat_id, "⏳ Начинаю обработку видео…",
        reply_to_message_id=job.message_id,
    )

    async def activity() -> None:
        # Sends outbound packets during long downloads/FFmpeg work to prevent Railway
        # serverless from suspending a service in the middle of an active job.
        while True:
            await asyncio.sleep(25)
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=job.chat_id, action="upload_video")

    task = asyncio.create_task(activity(), name="upload-keep-awake")
    try:
        with tempfile.TemporaryDirectory(prefix="tg-video-") as scratch:
            downloaded: DownloadedMedia = await fetch_video(job.url, Path(scratch), settings)
            await progress.edit_text("📤 Почти готово — отправляю видео…")
            caption = f"{downloaded.title} · {downloaded.source}"[:1024]
            items = downloaded.items[:settings.max_items]
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
            await progress.edit_text("✅ Готово! Видео отправлено выше.", reply_markup=main_menu())
    except DownloadProblem as exc:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text(f"⚠️ {exc}", reply_markup=main_menu())
    except TelegramAPIError:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("Не получилось отправить файл. Попробуйте прислать ссылку ещё раз.", reply_markup=main_menu())
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("⏹ Задача отменена.", reply_markup=main_menu())
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
                reply_to_message_id=job.message_id, reply_markup=main_menu(),
            )
            return
        await upload_video(bot, job, settings)

    async def notify_job_error(job: Job) -> None:
        await bot.send_message(
            job.chat_id, "Не удалось обработать ссылку. Попробуйте ещё раз чуть позже.",
            reply_to_message_id=job.message_id, reply_markup=main_menu(),
        )

    worker = GlobalFIFO(
        limit=settings.max_queue,
        processor=process,
        on_error=notify_job_error,
    )
    router = Router()
    dp = Dispatcher()
    user_requests: dict[int, deque[float]] = defaultdict(deque)

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
                "Просто отправьте ссылку — я сам подберу качество и формат, а затем пришлю готовое видео сюда.\n\n"
                "🎬 Выбирать разрешение или аудиоформат не нужно.\n"
                "📌 Отправляйте только контент, который вы вправе скачивать.",
                reply_markup=main_menu(),
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
            "3. Дождитесь готового видео — я сам выберу подходящий формат.\n\n"
            "Можно отправить несколько ссылок одним сообщением. Задачи обрабатываются по очереди.",
            reply_markup=main_menu(),
        )

    @router.message(Command("status"), F.chat.type == "private")
    async def command_status(message: Message) -> None:
        if allowed(message):
            state = "⏳ Сейчас обрабатываю видео." if worker.active else "💤 Сейчас ничего не скачиваю."
            await message.answer(
                f"📊 В очереди: {worker.queue.qsize()} {_task_word(worker.queue.qsize())}.\n{state}",
                reply_markup=main_menu(),
            )
        else:
            await message.answer("🔒 Эта команда доступна только пользователям бота.")

    @router.message(Command("cancel"), F.chat.type == "private")
    async def command_cancel(message: Message) -> None:
        if allowed(message):
            removed, active = worker.cancel_chat(message.chat.id)
            messages = []
            if removed:
                messages.append(f"Убрал из очереди: {removed} {_link_word(removed)}")
            if active:
                messages.append("Текущую загрузку остановил")
            result = "\n".join(messages) if messages else "У вас нет активных или ожидающих задач."
            await message.answer(f"🗑 {result}.", reply_markup=main_menu())
        else:
            await message.answer("🔒 Эта команда доступна только пользователям бота.")

    @router.callback_query(F.data.startswith("menu:"))
    async def menu_action(callback: CallbackQuery) -> None:
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer()
            return
        if callback.from_user.id not in settings.allowed_user_ids:
            await callback.answer("Доступ к боту ограничен.", show_alert=True)
            return
        action = callback.data.split(":", 1)[1] if callback.data else ""
        if action == "help":
            text = (
                "📥 Как скачать видео\n"
                "Отправьте ссылку на видео или публикацию в этот чат.\n"
                "Я сам выберу подходящее качество и пришлю готовое видео.\n\n"
                "Можно отправить несколько ссылок одним сообщением — обработаю их по очереди."
            )
        elif action == "status":
            state = "⏳ Сейчас обрабатываю видео." if worker.active else "💤 Сейчас ничего не скачиваю."
            queued = worker.queue.qsize()
            text = f"📊 В общей очереди: {queued} {_task_word(queued)}.\n{state}"
        elif action == "cancel":
            removed, active = worker.cancel_chat(callback.message.chat.id)
            messages = []
            if removed:
                messages.append(f"Убрал из очереди: {removed} {_link_word(removed)}")
            if active:
                messages.append("Текущую загрузку остановил")
            result = "\n".join(messages) if messages else "У вас нет активных или ожидающих задач."
            text = f"🗑 {result}."
        else:
            text = "👋 Отправьте ссылку на видео — я сам подготовлю файл для Telegram."
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_text(text, reply_markup=main_menu())
        await callback.answer()

    @router.message(F.chat.type == "private", F.text)
    async def message_text(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        if not allowed(message):
            await message.answer("🔒 Доступ к боту ограничен. Узнать свой ID можно командой /id.")
            return
        urls, invalid = extract_links(message.text, settings.domains, blocked=settings.blocked_domains)
        if not urls:
            await message.answer(
                "🔗 Не нашёл ссылку в сообщении. Отправьте публичную ссылку на видео или публикацию.",
                reply_markup=main_menu(),
            )
            return
        positions = []
        now = time.monotonic()
        rate_limited = False
        requests = user_requests[message.from_user.id]
        while requests and requests[0] <= now - 60:
            requests.popleft()
        for url in urls:
            if len(requests) >= settings.rate_limit_per_min:
                rate_limited = True
                break
            position = worker.add(Job(chat_id=message.chat.id, url=url, message_id=message.message_id))
            if position is None:
                break
            positions.append(position)
            requests.append(now)
        info_lines = []
        if positions:
            accepted = len(positions)
            confirmation = "✅ Ссылка принята." if accepted == 1 else f"✅ Ссылки приняты: {accepted}."
            info_lines.append(f"{confirmation} Обработаю по очереди и пришлю видео в этот чат.")
        if invalid:
            info_lines.append(f"⚠️ Не удалось распознать ссылок: {invalid}.")
        if rate_limited:
            limit = settings.rate_limit_per_min
            limit_word = "ссылку" if limit == 1 else _link_word(limit)
            info_lines.append(f"⏱ Лимит — не более {limit} {limit_word} в минуту.")
        elif len(positions) < len(urls):
            info_lines.append("⌛ Очередь сейчас заполнена. Попробуйте отправить оставшиеся ссылки позже.")
        info = "\n".join(info_lines) or "⌛ Сейчас очередь заполнена. Попробуйте отправить ссылку чуть позже."
        # If sending acknowledgement fails, do not return 5xx to Telegram:
        # otherwise Telegram retries the same update and duplicates queued URLs.
        with contextlib.suppress(TelegramAPIError):
            await message.answer(info, reply_markup=main_menu())

    @router.message(F.chat.type == "private")
    async def unknown_message(message: Message) -> None:
        if allowed(message):
            await message.answer(
                "💡 Отправьте ссылку на видео, и я подготовлю его для просмотра в Telegram.",
                reply_markup=main_menu(),
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
        if info.url != settings.webhook_url or info.max_connections != 1:
            await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret, max_connections=1, allowed_updates=["message"])
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
