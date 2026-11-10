"""Local HTTP integration tests using real aiohttp/aiogram routing and mocked Telegram API."""
import asyncio
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiohttp.test_utils import TestClient, TestServer

from app import cleanup_orphaned_tempdirs, make_app
from core import Settings
from downloader import DownloadedItem, DownloadedMedia

OWNER_ID = 123456789
GUEST_ID = 112233445
SECRET = "S" * 32


def settings():
    return Settings.from_env({
        "BOT_TOKEN": "123456789:" + "X" * 35,
        "TELEGRAM_ID_1": str(OWNER_ID),
        "RATE_LIMIT_PER_MIN": "1",
        "WEBHOOK_SECRET": SECRET,
        "WEBHOOK_BASE_URL": "https://test.up.railway.app",
    })


def update(user_id, text, seq=1):
    return {
        "update_id": seq,
        "message": {
            "message_id": seq, "date": int(time.time()),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }


def callback_update(user_id, data, seq=1):
    return {
        "update_id": seq,
        "callback_query": {
            "id": f"callback-{seq}", "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "chat-instance",
            "message": {
                "message_id": seq, "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"}, "text": "Menu",
            },
            "data": data,
        },
    }


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_cleanup_removes_only_bot_temp_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orphan = root / "tg-video-old"
            orphan.mkdir()
            (orphan / "stale.mp4").write_bytes(b"x")
            other = root / "other-temp"
            other.mkdir()
            self.assertEqual(cleanup_orphaned_tempdirs(root), 1)
            self.assertFalse(orphan.exists())
            self.assertTrue(other.exists())

    async def asyncSetUp(self):
        self.send = AsyncMock()
        progress = MagicMock()
        progress.edit_text = AsyncMock()
        self.send.return_value = progress
        self.video = AsyncMock()
        self.audio = AsyncMock()
        self.photo = AsyncMock()
        self.media_group = AsyncMock()
        self.webhook = AsyncMock()
        self.bot_call = AsyncMock(return_value=progress)
        self.patchers = [
            patch.object(Bot, "send_message", new=self.send),
            patch.object(Bot, "send_video", new=self.video),
            patch.object(Bot, "send_audio", new=self.audio),
            patch.object(Bot, "send_photo", new=self.photo),
            patch.object(Bot, "send_media_group", new=self.media_group),
            patch.object(Bot, "set_webhook", new=self.webhook),
            patch.object(Bot, "get_webhook_info", new=AsyncMock(return_value=MagicMock(url="https://test.up.railway.app/telegram-webhook", max_connections=1, allowed_updates=["message", "callback_query"]))),
            patch.object(Bot, "__call__", new=self.bot_call),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(TestServer(make_app(settings())))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, uid, text, secret=SECRET, seq=1):
        response = await self.client.post(
            "/telegram-webhook", json=update(uid, text, seq=seq),
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )
        if response.status == 200:
            for _ in range(50):
                if self.bot_call.await_count:
                    break
                await asyncio.sleep(0.01)
        return response

    async def post_callback(self, uid, data, seq=1):
        response = await self.client.post(
            "/telegram-webhook", json=callback_update(uid, data, seq=seq),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
        if response.status == 200:
            async with asyncio.timeout(2):
                while self.bot_call.await_count < 2:
                    await asyncio.sleep(0.01)
        return response

    async def test_secret_header_and_health(self):
        health = await self.client.get("/health")
        self.assertEqual(health.status, 200)
        self.assertEqual(await health.text(), "OK")
        response = await self.post(OWNER_ID, "/start", secret="WRONG")
        self.assertEqual(response.status, 401)
        self.bot_call.assert_not_awaited()
        self.webhook.assert_not_awaited()

    async def test_authorized_id_has_access_without_password(self):
        response = await self.post(OWNER_ID, "/start")
        self.assertEqual(response.status, 200)
        start_message = self.bot_call.await_args.args[0]
        self.assertIn("Привет! Я помогу скачать видео", start_message.text)
        self.assertIn("выберите качество видео или аудио MP3", start_message.text)
        self.assertIsNone(start_message.reply_markup)

    async def test_link_prompts_for_quality_or_audio_without_global_menu(self):
        with patch("app.public_url", new=AsyncMock(return_value=False)):
            response = await self.post(OWNER_ID, "https://example.org/video", seq=20)
        self.assertEqual(response.status, 200)
        prompt = self.bot_call.await_args.args[0]
        self.assertIn("Выберите формат", prompt.text)
        buttons = [button.callback_data for row in prompt.reply_markup.inline_keyboard for button in row]
        self.assertEqual(buttons, ["pick:1:v360", "pick:1:v720", "pick:1:v1080", "pick:1:mp3"])

    async def test_format_button_queues_selected_choice(self):
        with patch("app.public_url", new=AsyncMock(return_value=False)):
            await self.post(OWNER_ID, "https://example.org/video", seq=20)
        response = await self.post_callback(OWNER_ID, "pick:1:v720", seq=21)
        self.assertEqual(response.status, 200)
        self.assertTrue(any("Видео 720p" in str(call.args[0]) for call in self.bot_call.await_args_list))

    async def test_mp3_choice_sends_audio_instead_of_video(self):
        async def fake_fetch(url: str, folder: Path, cfg: Settings, choice: str) -> DownloadedMedia:
            self.assertEqual(choice, "mp3")
            audio_path = folder / "audio.mp3"
            audio_path.write_bytes(b"audio")
            return DownloadedMedia((DownloadedItem("audio", audio_path),), "Test audio", "example.org")

        with patch("app.public_url", new=AsyncMock(return_value=True)), patch("app.fetch_video", new=fake_fetch):
            await self.post(OWNER_ID, "https://example.org/audio", seq=30)
            prompt = self.bot_call.await_args.args[0]
            await self.post_callback(OWNER_ID, prompt.reply_markup.inline_keyboard[2][0].callback_data, seq=31)
            async with asyncio.timeout(2):
                while self.audio.await_count < 1:
                    await asyncio.sleep(0.025)
        self.audio.assert_awaited_once()
        self.video.assert_not_awaited()
        self.assertEqual(self.audio.await_args.kwargs["title"], "Test audio")

    async def test_unauthorized_link_does_not_enqueue(self):
        response = await self.post(GUEST_ID, "https://youtu.be/example")
        self.assertEqual(response.status, 200)
        text = self.bot_call.await_args.args[0].text
        self.assertIn("Доступ к боту ограничен", text)
        self.video.assert_not_awaited()

    async def test_any_user_can_read_own_id_but_not_download(self):
        response = await self.post(GUEST_ID, "/id")
        self.assertEqual(response.status, 200)
        self.assertIn(str(GUEST_ID), self.bot_call.await_args.args[0].text)

    async def test_authorized_downloads_reach_upload(self):
        async def fake_fetch(url: str, folder: Path, cfg: Settings, choice: str) -> Path:
            self.assertEqual(choice, "v720")
            file = folder / "video.mp4"
            file.write_bytes(b"test-content")
            return DownloadedMedia((DownloadedItem("video", file, 1920, 1080, 7),), "Test video", "youtube.com")

        with patch("app.public_url", new=AsyncMock(return_value=True)), patch("app.fetch_video", new=fake_fetch):
            response = await self.post(OWNER_ID, "https://youtu.be/abc")
            self.assertEqual(response.status, 200)
            prompt = self.bot_call.await_args.args[0]
            callback_data = prompt.reply_markup.inline_keyboard[0][1].callback_data
            await self.post_callback(OWNER_ID, callback_data, seq=100)
            async with asyncio.timeout(2):
                while self.video.await_count < 1:
                    await asyncio.sleep(0.025)
            self.assertEqual(self.video.await_count, 1)
            args = self.video.await_args.kwargs
            self.assertEqual((args["width"], args["height"], args["duration"]), (1920, 1080, 7))
            self.assertEqual(args["reply_to_message_id"], 1)

    async def test_photo_gallery_is_sent_as_ordered_media_group(self):
        async def fake_fetch(url: str, folder: Path, cfg: Settings, choice: str) -> DownloadedMedia:
            files = []
            for index in range(2):
                path = folder / f"photo-{index}.jpg"
                path.write_bytes(b"photo")
                files.append(DownloadedItem("photo", path))
            return DownloadedMedia(tuple(files), "Album", "pinterest.com")

        with patch("app.public_url", new=AsyncMock(return_value=True)), patch("app.fetch_video", new=fake_fetch):
            response = await self.post(OWNER_ID, "https://example.org/a https://example.org/b", seq=2)
            self.assertEqual(response.status, 200)
            prompt = self.bot_call.await_args_list[0].args[0]
            await self.post_callback(OWNER_ID, prompt.reply_markup.inline_keyboard[0][0].callback_data, seq=22)
            async with asyncio.timeout(2):
                while self.media_group.await_count < 1:
                    await asyncio.sleep(0.025)
        self.assertEqual(len(self.media_group.await_args.kwargs["media"]), 2)
        self.assertEqual(self.media_group.await_args.kwargs["reply_to_message_id"], 2)
        self.photo.assert_not_awaited()

    async def test_per_user_rate_limit_applies_before_enqueueing(self):
        with patch("app.public_url", new=AsyncMock(return_value=False)):
            response = await self.post(OWNER_ID, "https://example.org/a https://example.org/b", seq=3)
        self.assertEqual(response.status, 200)
        self.assertIn("не более 1 ссылку", self.bot_call.await_args.args[0].text)
