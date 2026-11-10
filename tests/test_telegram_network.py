"""Opt-in Telegram delivery and real-download FIFO tests."""
import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.types import FSInputFile

from core import Settings
from downloader import fetch_video
from jobs import GlobalFIFO, Job


def _settings() -> Settings:
    return Settings.from_env({
        "BOT_TOKEN": os.environ["BOT_TOKEN"],
        "TELEGRAM_ID_1": os.environ["TELEGRAM_ID_1"],
        "WEBHOOK_SECRET": "S" * 32,
        "WEBHOOK_BASE_URL": "https://example.com",
    })


@pytest.mark.network
@pytest.mark.asyncio
async def test_real_telegram_video_send_and_temp_cleanup() -> None:
    if os.getenv("RUN_TELEGRAM_TESTS") != "1":
        pytest.skip("Set RUN_TELEGRAM_TESTS=1 with a dedicated test bot and private chat")
    url = os.getenv("TELEGRAM_TEST_VIDEO_URL", "").strip()
    chat_id = os.getenv("TELEGRAM_ID_1", "").strip()
    if not os.getenv("BOT_TOKEN") or not chat_id or not url:
        pytest.skip("BOT_TOKEN, TELEGRAM_ID_1, and TELEGRAM_TEST_VIDEO_URL are required")
    settings = _settings()
    with tempfile.TemporaryDirectory(prefix="tg-video-telegram-test-") as temp:
        result = await fetch_video(url, Path(temp), settings)
        item = result.items[0]
        async with Bot(settings.token) as bot:
            await bot.send_video(
                chat_id=int(chat_id), video=FSInputFile(item.path),
                width=item.width or None, height=item.height or None, duration=item.duration or None,
            )
        assert item.path.is_file()
    assert not Path(temp).exists()


@pytest.mark.network
@pytest.mark.asyncio
async def test_two_real_links_are_downloaded_and_sent_in_fifo_order() -> None:
    if os.getenv("RUN_TELEGRAM_TESTS") != "1":
        pytest.skip("Set RUN_TELEGRAM_TESTS=1 with a dedicated test bot and private chat")
    urls = [os.getenv("TELEGRAM_TEST_VIDEO_URL_1", "").strip(), os.getenv("TELEGRAM_TEST_VIDEO_URL_2", "").strip()]
    chat_id = os.getenv("TELEGRAM_ID_1", "").strip()
    if not os.getenv("BOT_TOKEN") or not chat_id or not all(urls):
        pytest.skip("BOT_TOKEN, TELEGRAM_ID_1, and both TELEGRAM_TEST_VIDEO_URL_n variables are required")
    settings = _settings()
    events: list[tuple[str, int, float]] = []
    async with Bot(settings.token) as bot, tempfile.TemporaryDirectory(prefix="tg-video-fifo-test-") as root:
        async def process(job: Job) -> None:
            index = urls.index(job.url) + 1
            directory = Path(root) / str(index)
            directory.mkdir()
            events.append(("download-start", index, time.monotonic()))
            media = await fetch_video(job.url, directory, settings)
            item = media.items[0]
            await bot.send_video(chat_id=int(chat_id), video=FSInputFile(item.path))
            events.append(("send-complete", index, time.monotonic()))

        async def ignore_error(_job: Job) -> None:
            raise AssertionError("A real FIFO job failed")

        worker = GlobalFIFO(limit=2, processor=process, on_error=ignore_error)
        worker.add(Job(int(chat_id), urls[0]))
        worker.add(Job(int(chat_id), urls[1]))
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(worker.queue.join(), timeout=settings.timeout_sec * 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert [event[:2] for event in events] == [
        ("download-start", 1), ("send-complete", 1),
        ("download-start", 2), ("send-complete", 2),
    ]
    assert events[1][2] <= events[2][2]
