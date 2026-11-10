import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from downloader import _metadata, classify_ytdlp_error, disk_used, fetch_video, select_format


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_ytdlp_errors_are_classified_without_echoing_raw_text(self):
        examples = {
            "This video is private": "private",
            "Sign in to confirm your age": "login",
            "not available in your country": "geo",
            "This content is protected by DRM": "drm",
            "Video unavailable: deleted": "deleted",
            "No video formats found": "unsupported",
            "File is larger than max-filesize": "size",
            "HTTP Error 429: Too Many Requests": "rate_limit",
            "Connection timed out": "network",
        }
        for stderr, expected in examples.items():
            with self.subTest(stderr=stderr):
                error = classify_ytdlp_error(stderr)
                self.assertEqual(error.category, expected)
                self.assertTrue(error.user_message)
                self.assertNotIn(stderr, error.user_message)

    async def test_format_selection_prefers_best_under_limit_and_compatible_codec(self):
        formats = [
            {"format_id": "v1", "ext": "mp4", "vcodec": "avc1.64001f", "acodec": "mp4a.40.2", "height": 720, "filesize": 40_000_000},
            {"format_id": "v2", "ext": "webm", "vcodec": "vp9", "acodec": "opus", "height": 1080, "filesize": 38_000_000},
            {"format_id": "v3", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "mp4a.40.2", "height": 1080, "filesize": 60_000_000},
        ]
        choice = select_format(formats, 49_000_000)
        self.assertEqual(choice.format_selector, "v2")
        self.assertTrue(choice.fits_limit)

    async def test_format_selection_combines_video_and_audio_sizes(self):
        formats = [
            {"format_id": "v1080", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 1080, "filesize_approx": 40_000_000},
            {"format_id": "v720", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 28_000_000},
            {"format_id": "a1", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 5_000_000},
        ]
        choice = select_format(formats, 35_000_000)
        self.assertEqual(choice.format_selector, "v720+a1")
        self.assertEqual(choice.estimated_size, 33_000_000)

    async def test_metadata_retries_network_errors_with_exponential_backoff(self):
        config = SimpleNamespace(timeout_sec=60)
        success = (0, '{"formats": []}', "")
        with patch("downloader._run_process", new=AsyncMock(side_effect=[
            (1, "", "ERROR: Connection timed out"),
            (1, "", "ERROR: Connection timed out"),
            success,
        ])) as run, patch("downloader.asyncio.sleep", new=AsyncMock()) as sleep, \
             tempfile.TemporaryDirectory() as temp:
            result = await _metadata("https://example.org/v", Path(temp), config, None, [False])
        self.assertEqual(result, {"formats": []})
        self.assertEqual(run.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])

    async def test_extractor_failure_updates_once_then_uses_generic_fallback(self):
        config = SimpleNamespace(timeout_sec=60)
        success = (0, '{"formats": []}', "")
        with patch("downloader._run_process", new=AsyncMock(side_effect=[
            (1, "", "ERROR: Unsupported URL"),
            (1, "", "ERROR: Unsupported URL"),
            success,
        ])) as run, patch("downloader._update_ytdlp", new=AsyncMock(return_value=True)) as update, \
             tempfile.TemporaryDirectory() as temp:
            state = [False]
            result = await _metadata("https://example.org/v", Path(temp), config, None, state)
        self.assertEqual(result, {"formats": []})
        self.assertTrue(state[0])
        update.assert_awaited_once()
        self.assertEqual(run.await_count, 3)
        self.assertIn("--force-generic-extractor", run.await_args_list[-1].args[0])
    async def test_disk_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.bin").write_bytes(b"a" * 150)
            (root / "two.bin").write_bytes(b"b" * 50)
            self.assertEqual(disk_used(root), 200)

    async def test_cleanup_subprocess_when_cancelled(self):
        class FakeProc:
            pid = 98765
            returncode = None
            async def communicate(self):
                await asyncio.sleep(100)

        proc = FakeProc()
        config = SimpleNamespace(max_file_mb=45, timeout_sec=180)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("downloader.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
            patch("downloader.terminate_process", new_callable=AsyncMock) as kill, \
             patch("downloader._metadata", new=AsyncMock(return_value={"formats": [
                 {"format_id": "v1", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 360, "filesize": 100}
             ]})):
            task = asyncio.create_task(fetch_video("https://youtu.be/abc", Path(tmp), config))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_awaited_once_with(proc)


if __name__ == "__main__":
    unittest.main()
