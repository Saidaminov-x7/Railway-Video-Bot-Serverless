import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from downloader import disk_used, DownloadProblem, fetch_video


class DownloadTests(unittest.IsolatedAsyncioTestCase):
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
            async def wait(self):
                await asyncio.sleep(100)

        proc = FakeProc()
        config = SimpleNamespace(max_file_mb=45, timeout_sec=180)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("downloader.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
                 patch("downloader.terminate_process", new_callable=AsyncMock) as kill:
                task = asyncio.create_task(fetch_video("https://youtu.be/abc", Path(tmp), config))
                await asyncio.sleep(0.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                kill.assert_awaited_once_with(proc)


if __name__ == "__main__":
    unittest.main()
