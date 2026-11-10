"""yt-dlp subprocess with runtime, size and disk-use limits."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from pathlib import Path

from core import Settings


class DownloadProblem(Exception):
    pass


def disk_used(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with contextlib.suppress(OSError):
                total += path.stat().st_size
    return total


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    with contextlib.suppress(Exception):
        await proc.wait()


async def fetch_video(url: str, folder: Path, settings: Settings) -> Path:
    args = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-cache-dir",
        "--no-warnings", "--no-progress", "--no-write-thumbnail", "--no-write-subs",
        "--no-write-auto-subs", "--no-part", "--retries", "2",
        "--fragment-retries", "2", "--concurrent-fragments", "1",
        "--socket-timeout", "30", "--max-filesize", f"{settings.max_file_mb}M",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/best",
        "--merge-output-format", "mp4",
        "-o", str(folder / "video.%(ext)s"),
        "--", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise DownloadProblem("Сервис загрузки недоступен.") from exc
    deadline = time.monotonic() + settings.timeout_sec
    try:
        while proc.returncode is None:
            if time.monotonic() >= deadline:
                raise DownloadProblem("Истекло время скачивания.")
            # Separate video/audio may coexist during merging; bound total disk use.
            if disk_used(folder) > settings.max_file_mb * 1_000_000 * 3:
                raise DownloadProblem("Недостаточно лимита для временных файлов.")
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except TimeoutError:
                pass
        if proc.returncode != 0:
            raise DownloadProblem("Не удалось скачать видео. Проверь ссылку или ограничения сайта.")
        output = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
        if len(output) != 1:
            raise DownloadProblem("Не удалось определить готовое видео.")
        if output[0].stat().st_size > settings.max_file_mb * 1_000_000:
            raise DownloadProblem("Видео превышает установленный лимит размера.")
        return output[0]
    finally:
        await terminate_process(proc)
