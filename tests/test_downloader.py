import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import downloader

from downloader import DownloadProblem, UpdateState, _download_gallery, _make_thumbnail, _metadata, _normalize_video, _run_process, _write_cookie_file, _yt_dlp_args, classify_ytdlp_error, cleanup_ytdlp_runtime, disk_used, fetch_video, select_audio_format, select_format, update_ytdlp_runtime


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_updater_runtime_is_used_until_cleanup(self):
        config = SimpleNamespace(timeout_sec=60, max_file_mb=49)
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "yt-dlp-runtime"
            (runtime / "yt_dlp").mkdir(parents=True)
            (runtime / "yt_dlp" / "__main__.py").touch()
            with patch("downloader._update_ytdlp", new=AsyncMock(return_value=runtime)):
                await update_ytdlp_runtime(config)
            self.assertEqual(downloader._STARTUP_PACKAGE_ROOT, runtime)
            state = UpdateState(attempted=downloader._STARTUP_PACKAGE_ROOT is not None, package_root=downloader._STARTUP_PACKAGE_ROOT)
            self.assertEqual(state.package_root, runtime)
            self.assertTrue(state.attempted)
            cleanup_ytdlp_runtime()
            self.assertIsNone(downloader._STARTUP_PACKAGE_ROOT)
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
        self.assertEqual(choice.format_selector, "v1")
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

    async def test_format_choices_cap_video_height_and_select_audio(self):
        formats = [
            {"format_id": "v360", "vcodec": "avc1", "acodec": "none", "height": 360, "filesize": 3_000_000},
            {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 8_000_000},
            {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 15_000_000},
            {"format_id": "a1", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 2_000_000},
        ]
        self.assertEqual(select_format(formats, 49_000_000, 360).format_selector, "v360+a1")
        self.assertEqual(select_audio_format(formats, 49_000_000).format_selector, "a1")
        args = _yt_dlp_args("https://example.org/video", selector="a1", audio=True)
        self.assertIn("--audio-format", args)
        self.assertIn("mp3", args)

    async def test_metadata_retries_network_errors_with_exponential_backoff(self):
        config = SimpleNamespace(timeout_sec=60, max_file_mb=49)
        success = (0, '{"formats": []}', "")
        with patch("downloader._run_process", new=AsyncMock(side_effect=[
            (1, "", "ERROR: Connection timed out"),
            (1, "", "ERROR: Connection timed out"),
            success,
        ])) as run, patch("downloader.asyncio.sleep", new=AsyncMock()) as sleep, \
             tempfile.TemporaryDirectory() as temp:
            result = await _metadata("https://example.org/v", Path(temp), config, None, UpdateState())
        self.assertEqual(result, {"formats": []})
        self.assertEqual(run.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])

    async def test_extractor_failure_updates_once_then_uses_generic_fallback(self):
        config = SimpleNamespace(timeout_sec=60, max_file_mb=49)
        success = (0, '{"formats": []}', "")
        package_root = Path("updated-runtime")
        with patch("downloader._run_process", new=AsyncMock(side_effect=[
            (1, "", "ERROR: Unsupported URL"),
            (1, "", "ERROR: Unsupported URL"),
            success,
        ])) as run, patch("downloader._yt_dlp_args", wraps=__import__("downloader")._yt_dlp_args) as build_args, \
             patch("downloader._update_ytdlp", new=AsyncMock(return_value=package_root)) as update, \
             tempfile.TemporaryDirectory() as temp:
            state = UpdateState()
            result = await _metadata("https://example.org/v", Path(temp), config, None, state)
        self.assertEqual(result, {"formats": []})
        self.assertTrue(state.attempted)
        self.assertEqual(state.package_root, package_root)
        update.assert_awaited_once()
        self.assertEqual(run.await_count, 3)
        self.assertTrue(build_args.call_args_list[-1].kwargs["generic"])

    async def test_non_mp4_video_is_transcoded_and_verified_below_limit(self):
        config = SimpleNamespace(max_file_mb=1, timeout_sec=120)
        source_probe = {"streams": [{"codec_type": "video", "codec_name": "vp9", "width": 1080, "height": 1920}], "format": {"duration": "30"}}
        final_probe = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280}], "format": {"duration": "30"}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.webm"
            source.write_bytes(b"source")

            async def fake_run(args, folder, timeout, **kwargs):
                Path(args[-1]).write_bytes(b"encoded")
                return 0, "", ""

            with patch("downloader._probe", new=AsyncMock(side_effect=[source_probe, final_probe])), \
                 patch("downloader._run_process", new=AsyncMock(side_effect=fake_run)) as run:
                output, probe = await _normalize_video(source, root, config, 30)
        self.assertEqual(output.suffix, ".mp4")
        self.assertEqual(_video_codec(probe), "h264")
        ffmpeg_args = run.await_args.args[0]
        self.assertIn("libx264", ffmpeg_args)
        self.assertIn("+faststart", ffmpeg_args)
        self.assertIn("-threads", ffmpeg_args)
        self.assertIn("-maxrate", ffmpeg_args)

    async def test_thumbnail_is_removed_when_telegram_size_cap_is_exceeded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "video.mp4"

            async def oversized_thumbnail(args, folder, timeout, **kwargs):
                Path(args[-1]).write_bytes(b"x" * 200_001)
                return 0, "", ""

            with patch("downloader._run_process", new=AsyncMock(side_effect=oversized_thumbnail)):
                result = await _make_thumbnail(video, root, 60, 1_000_000)
            self.assertIsNone(result)
            self.assertFalse((root / "thumbnail.jpg").exists())

    async def test_cookies_are_decoded_to_private_temp_file(self):
        import base64

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _write_cookie_file(root, base64.b64encode(b"# Netscape HTTP Cookie File").decode())
            self.assertEqual(path.read_bytes(), b"# Netscape HTTP Cookie File")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(path.exists())

    async def test_gallery_fallback_collects_only_the_item_limit(self):
        config = SimpleNamespace(max_file_mb=49, max_items=2, timeout_sec=60)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            async def fake_gallery(args, folder, timeout, **kwargs):
                for name in ("01.jpg", "02.webp", "03.png"):
                    (folder / name).write_bytes(b"image")
                self.assertIn("1-2", args)
                return 0, "", ""

            with patch("downloader._run_process", new=AsyncMock(side_effect=fake_gallery)):
                result = await _download_gallery("https://example.org/gallery", root, config)
        self.assertEqual([item.path.name for item in result.items], ["01.jpg", "02.webp"])
        self.assertEqual([item.kind for item in result.items], ["photo", "photo"])


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
        config = SimpleNamespace(max_file_mb=49, timeout_sec=180)
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

    async def test_disk_cap_is_checked_after_fast_subprocess_exit(self):
        class FakeProc:
            pid = 98766
            returncode = 0

            def __init__(self, root):
                self.root = root

            async def communicate(self):
                (self.root / "oversized.bin").write_bytes(b"x" * 20)
                return b"", b""

            async def wait(self):
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc = FakeProc(root)
            with patch("downloader.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
                with self.assertRaisesRegex(DownloadProblem, "лимит временного диска"):
                    await _run_process(["fake"], root, 5, disk_limit=10)


def _video_codec(probe):
    return next(stream["codec_name"] for stream in probe["streams"] if stream["codec_type"] == "video")


if __name__ == "__main__":
    unittest.main()
