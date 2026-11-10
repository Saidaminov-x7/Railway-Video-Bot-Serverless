"""Railway webhook bot. One worker; ephemeral sessions and per-video temp files."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from core import Settings, extract_links
from downloader import DownloadProblem, fetch_video
from jobs import GlobalFIFO, Job

LOG = logging.getLogger("video-bot")


async def upload_video(bot: Bot, job: Job, settings: Settings) -> None:
    progress = await bot.send_message(job.chat_id, "Скачиваю видео…")

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
            file = await fetch_video(job.url, Path(scratch), settings)
            await progress.edit_text("Отправляю видео в Telegram…")
            item = FSInputFile(file, filename="video" + file.suffix.lower())
            if file.suffix.lower() == ".mp4":
                await bot.send_video(
                    chat_id=job.chat_id, video=item, supports_streaming=True,
                    request_timeout=settings.upload_timeout_sec,
                )
            else:
                await bot.send_document(
                    chat_id=job.chat_id, document=item,
                    request_timeout=settings.upload_timeout_sec,
                )
        # TemporaryDirectory cleans up even after failure; no retained video.
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("Видео отправлено. Временный файл удалён.")
    except DownloadProblem as exc:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text(str(exc))
    except TelegramAPIError:
        with contextlib.suppress(TelegramAPIError):
            await progress.edit_text("Ошибка отправки в Telegram.")
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

    worker = GlobalFIFO(
        limit=settings.max_queue,
        processor=lambda job: upload_video(bot, job, settings),
        on_error=lambda job: bot.send_message(job.chat_id, "Ошибка обработки. Повтори отправку ссылки."),
    )
    router = Router()
    dp = Dispatcher()

    def allowed(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in settings.allowed_user_ids)

    @router.message(Command("id"), F.chat.type == "private")
    async def command_id(message: Message) -> None:
        if message.from_user:
            await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    @router.message(Command("start"), F.chat.type == "private")
    async def command_start(message: Message) -> None:
        if allowed(message):
            await message.answer("Доступ открыт. Пришли ссылку или несколько ссылок на видео.")
        else:
            await message.answer("Доступ запрещён. Узнай свой ID командой /id и добавь его в Railway Variables.")

    @router.message(F.chat.type == "private", F.text)
    async def message_text(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        if not allowed(message):
            await message.answer("Доступ запрещён. Для получения Telegram ID отправь /id.")
            return
        urls, invalid = extract_links(message.text, settings.domains)
        if not urls:
            await message.answer("Пришли HTTPS-ссылку на видео с поддерживаемого сайта.")
            return
        positions = []
        for url in urls:
            position = worker.add(Job(chat_id=message.chat.id, url=url))
            if position is None:
                break
            positions.append(position)
        info = f"Принято: {len(positions)}. Позиции в очереди: {', '.join(map(str, positions)) or 'нет'}."
        if invalid:
            info += f" Отклонено ссылок: {invalid}."
        if len(positions) < len(urls):
            info += " Очередь заполнена."
        # If sending acknowledgement fails, do not return 5xx to Telegram:
        # otherwise Telegram retries the same update and duplicates queued URLs.
        with contextlib.suppress(TelegramAPIError):
            await message.answer(info)

    @router.message(F.chat.type == "private")
    async def unknown_message(message: Message) -> None:
        if allowed(message):
            await message.answer("Отправь текстовую HTTPS-ссылку на видео.")
        else:
            await message.answer("Доступ запрещён. Для получения Telegram ID отправь /id.")

    dp.include_router(router)

    async def start(bot: Bot) -> None:
        # One in-memory worker for all users: no overlapping download or upload.
        app["queue_task"] = asyncio.create_task(worker.run(), name="global-single-worker")
        # No drop_pending_updates: avoids discarding user messages during cold start.
        # max_connections=1 preserves order of delivery from Telegram as much as API allows.
        await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret, max_connections=1, allowed_updates=["message"])
        LOG.warning("Bot started in mode=%s; one global worker", settings.mode)

    async def stop(bot: Bot) -> None:
        task = app.get("queue_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Do NOT delete webhook: it must remain registered while sleeping.

    dp.startup.register(start)
    dp.shutdown.register(stop)

    app = web.Application(client_max_size=1 * 1024 * 1024)

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
        handle_in_background=False,
    ).register(app, path="/telegram-webhook")
    setup_application(app, dp, bot=bot)
    return app


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    web.run_app(make_app(settings), host="0.0.0.0", port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
