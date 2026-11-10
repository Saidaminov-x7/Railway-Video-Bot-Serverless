"""yt-dlp subprocess with runtime, size and disk-use limits."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core import Settings

LOG = logging.getLogger("video-bot.downloader")


class DownloadProblem(Exception):
    pass


@dataclass(frozen=True)
class ClassifiedError:
    category: str
    user_message: str
    retryable: bool = False
    extractor_failure: bool = False


@dataclass(frozen=True)
class FormatSelection:
    format_selector: str
    estimated_size: int | None
    fits_limit: bool


_ERRORS = (
    ("drm", re.compile(r"\bdrm\b|digital rights management|protected by drm", re.I), "Это видео защищено DRM и не может быть обработано."),
    ("private", re.compile(r"private video|video is private|private content", re.I), "Видео закрыто или доступно только его владельцу."),
    ("login", re.compile(r"sign in|log ?in|authentication required|confirm your age|cookies are required", re.I), "Для этого видео требуется вход в аккаунт."),
    ("geo", re.compile(r"not available in your country|geo.?restricted|country.{0,20}blocked|geographic restriction", re.I), "Видео недоступно в вашем регионе."),
    ("deleted", re.compile(r"has been removed|video unavailable.{0,30}deleted|this video does not exist|page not found", re.I), "Видео удалено или ссылка больше не действует."),
    ("size", re.compile(r"max.?filesize|file is larger|file size.{0,20}(?:exceed|limit)|too large", re.I), "Видео превышает лимит размера отправки."),
    ("rate_limit", re.compile(r"http error 429|too many requests|rate.?limit", re.I), "Сайт временно ограничил запросы. Попробуйте позже.", True),
    ("network", re.compile(r"timed? ?out|connection reset|temporary failure|name resolution|network is unreachable|http error 5\d\d", re.I), "Не удалось связаться с сайтом. Повторяю попытку.", True),
    ("unsupported", re.compile(r"no video formats found|unsupported url|no suitable extractor|unsupported site", re.I), "Этот формат или сайт пока не поддерживается."),
)


def classify_ytdlp_error(stderr: str) -> ClassifiedError:
    """Map extractor diagnostics to a safe stable category and Russian user message."""
    for category, pattern, message, *flags in _ERRORS:
        if pattern.search(stderr):
            return ClassifiedError(category, message, bool(flags and flags[0]), category == "unsupported")
    return ClassifiedError("extractor", "Не удалось обработать эту ссылку.", extractor_failure=True)


def _format_size(item: dict[str, Any]) -> int | None:
    size = item.get("filesize") or item.get("filesize_approx")
    return int(size) if isinstance(size, (int, float)) and size > 0 else None


def _format_score(video: dict[str, Any], audio: dict[str, Any] | None = None) -> tuple[int, int, int, int]:
    codecs = f"{video.get('vcodec', '')} {video.get('acodec', '')} {audio.get('acodec', '') if audio else ''}".lower()
    compatible = int(video.get("ext") == "mp4") + int("avc" in codecs or "h264" in codecs) + int("mp4a" in codecs or "aac" in codecs)
    return int(video.get("height") or 0), compatible, int(video.get("fps") or 0), int(video.get("tbr") or 0)


def select_format(formats: list[dict[str, Any]], max_bytes: int) -> FormatSelection:
    """Choose the highest-quality known-size single or split format under the cap."""
    valid_id = re.compile(r"^[A-Za-z0-9_.-]+$")
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and valid_id.fullmatch(str(f.get("format_id", "")))]
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and valid_id.fullmatch(str(f.get("format_id", "")))]
    candidates: list[tuple[tuple[int, int, int, int], int | None, str]] = []
    for video in videos:
        video_size = _format_size(video)
        if video.get("acodec") not in (None, "none"):
            candidates.append((_format_score(video), video_size, str(video["format_id"])))
            continue
        for audio in audios:
            audio_size = _format_size(audio)
            size = video_size + audio_size if video_size is not None and audio_size is not None else None
            candidates.append((_format_score(video, audio), size, f"{video['format_id']}+{audio['format_id']}"))
    if not candidates:
        raise DownloadProblem("На странице не найден доступный видеоформат.")
    fitting = [candidate for candidate in candidates if candidate[1] is not None and candidate[1] <= max_bytes]
    pool = fitting or candidates
    score, size, selector = max(pool, key=lambda candidate: (candidate[0], candidate[1] or 0))
    del score
    return FormatSelection(selector, size, bool(fitting))


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


async def _run_process(
    args: list[str], folder: Path, timeout_sec: int, *, capture_stdout: bool = False, disk_limit: int | None = None
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=folder,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise DownloadProblem("Сервис загрузки недоступен.") from exc
    communicate = asyncio.create_task(proc.communicate())
    deadline = time.monotonic() + timeout_sec
    try:
        while not communicate.done():
            if time.monotonic() >= deadline:
                raise DownloadProblem("Истекло время обработки ссылки.")
            if disk_limit is not None and disk_used(folder) > disk_limit:
                raise DownloadProblem("Превышен лимит временного диска.")
            await asyncio.sleep(0.2)
        stdout, stderr = await communicate
        return proc.returncode or 0, stdout.decode("utf-8", "replace") if stdout else "", stderr.decode("utf-8", "replace") if stderr else ""
    finally:
        await terminate_process(proc)
        if not communicate.done():
            communicate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate


def _yt_dlp_args(url: str, *, selector: str | None = None, metadata: bool = False, generic: bool = False, cookies: Path | None = None) -> list[str]:
    args = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-cache-dir", "--no-progress",
        "--no-write-thumbnail", "--no-write-subs", "--no-write-auto-subs", "--retries", "0",
        "--fragment-retries", "0", "--concurrent-fragments", "1", "--socket-timeout", "30",
        "--impersonate", "chrome",
    ]
    if generic:
        args.append("--force-generic-extractor")
    if cookies:
        args.extend(["--cookies", str(cookies)])
    if metadata:
        args.extend(["-J", "--skip-download"])
    else:
        args.extend(["-f", selector or "best", "--merge-output-format", "mp4", "-o", str(Path("video.%(ext)s"))])
    args.extend(["--", url])
    return args


async def _update_ytdlp(folder: Path, timeout_sec: int) -> bool:
    # pip-installed yt-dlp is not self-updatable via `yt-dlp -U`.
    code, _out, _err = await _run_process(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "yt-dlp[default,curl-cffi]"],
        folder, min(timeout_sec, 180), capture_stdout=True,
    )
    return code == 0


async def _metadata(
    url: str, folder: Path, settings: Settings, cookies: Path | None, update_state: list[bool]
) -> dict[str, Any]:
    for generic in (False, True):
        attempts = 0
        while True:
            code, output, stderr = await _run_process(
                _yt_dlp_args(url, metadata=True, generic=generic, cookies=cookies),
                folder, settings.timeout_sec, capture_stdout=True,
            )
            if code == 0:
                try:
                    return json.loads(output)
                except json.JSONDecodeError:
                    stderr = "Invalid JSON metadata: " + stderr
            error = classify_ytdlp_error(stderr)
            _log_error(url, error.category)
            if error.retryable and attempts < 2:
                await asyncio.sleep(2**attempts)
                attempts += 1
                continue
            if error.extractor_failure and not update_state[0]:
                update_state[0] = True
                if await _update_ytdlp(folder, settings.timeout_sec):
                    # One update-triggered retry, then generic fallback if needed.
                    code, output, stderr = await _run_process(
                        _yt_dlp_args(url, metadata=True, generic=generic, cookies=cookies),
                        folder, settings.timeout_sec, capture_stdout=True,
                    )
                    if code == 0:
                        return json.loads(output)
                    error = classify_ytdlp_error(stderr)
                    _log_error(url, error.category)
            if generic or not error.extractor_failure:
                raise DownloadProblem(error.user_message)
            break
    raise DownloadProblem("Этот сайт не удалось обработать.")


def _log_error(url: str, category: str) -> None:
    host = urlsplit(url).hostname or "unknown"
    LOG.warning("download failed category=%s domain=%s", category, host[:253])


async def _fetch_video(url: str, folder: Path, settings: Settings) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.max_file_mb * 1_000_000
    update_state = [False]
    metadata = await _metadata(url, folder, settings, None, update_state)
    selection = select_format(metadata.get("formats", []), max_bytes)
    for attempt in range(3):
        code, _output, stderr = await _run_process(
            _yt_dlp_args(url, selector=selection.format_selector),
            folder, settings.timeout_sec, disk_limit=max_bytes * 3,
        )
        if code == 0:
            break
        error = classify_ytdlp_error(stderr)
        _log_error(url, error.category)
        if error.extractor_failure and not update_state[0]:
            update_state[0] = True
            if await _update_ytdlp(folder, settings.timeout_sec):
                code, output, stderr = await _run_process(
                    _yt_dlp_args(url, selector=selection.format_selector),
                    folder, settings.timeout_sec, disk_limit=max_bytes * 3,
                )
                if code == 0:
                    break
                error = classify_ytdlp_error(stderr)
                _log_error(url, error.category)
        if not error.retryable or attempt == 2:
            raise DownloadProblem(error.user_message)
        await asyncio.sleep(2**attempt)
    output = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if len(output) != 1:
        raise DownloadProblem("Не удалось определить готовое видео.")
    return output[0]


async def fetch_video(url: str, folder: Path, settings: Settings) -> Path:
    try:
        async with asyncio.timeout(settings.timeout_sec):
            return await _fetch_video(url, folder, settings)
    except TimeoutError as exc:
        raise DownloadProblem("Истекло общее время обработки ссылки.") from exc
