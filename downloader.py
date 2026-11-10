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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core import Settings

LOG = logging.getLogger("video-bot.downloader")
_STARTUP_PACKAGE_ROOT: Path | None = None


class DownloadProblem(Exception):
    def __init__(self, message: str, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


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


@dataclass(frozen=True)
class DownloadedItem:
    kind: str
    path: Path
    width: int = 0
    height: int = 0
    duration: int = 0
    thumbnail: Path | None = None


@dataclass(frozen=True)
class DownloadedMedia:
    items: tuple[DownloadedItem, ...]
    title: str
    source: str


@dataclass
class UpdateState:
    attempted: bool = False
    package_root: Path | None = None


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
    return compatible, int(video.get("height") or 0), int(video.get("fps") or 0), int(video.get("tbr") or 0)


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
    args: list[str], folder: Path, timeout_sec: int, *, capture_stdout: bool = False,
    disk_limit: int | None = None, env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=folder,
            start_new_session=os.name != "nt",
            env=env,
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
        if disk_limit is not None and disk_used(folder) > disk_limit:
            raise DownloadProblem("Превышен лимит временного диска.", "size")
        return proc.returncode or 0, stdout.decode("utf-8", "replace") if stdout else "", stderr.decode("utf-8", "replace") if stderr else ""
    finally:
        await terminate_process(proc)
        if not communicate.done():
            communicate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate


def _yt_dlp_args(
    url: str, *, selector: str | None = None, metadata: bool = False, generic: bool = False,
    cookies: Path | None = None, package_root: Path | None = None,
) -> list[str]:
    executable = [sys.executable, "-m", "yt_dlp"]
    args = [
        *executable, "--ignore-config", "--no-playlist", "--no-cache-dir", "--no-progress",
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


def _package_env(package_root: Path | None) -> dict[str, str] | None:
    if package_root is None:
        return None
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(package_root) + (os.pathsep + existing if existing else "")
    return env


def _write_cookie_file(folder: Path, encoded: str | None) -> Path | None:
    if not encoded:
        return None
    import base64

    try:
        contents = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise DownloadProblem("Не удалось прочитать файл cookies.") from exc
    if not contents or len(contents) > 1_000_000:
        raise DownloadProblem("Файл cookies пустой или превышает 1 MB.")
    path = folder / "cookies.txt"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as cookie_file:
        cookie_file.write(contents)
    os.chmod(path, 0o600)
    return path


async def _update_ytdlp(folder: Path, timeout_sec: int, disk_limit: int) -> Path | None:
    # Use a job-owned package directory so the non-root app user can safely update once.
    package_root = folder / "yt-dlp-runtime"
    code, _out, _err = await _run_process(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "--target", str(package_root),
         "yt-dlp[default,curl-cffi]", "yt-dlp-ejs"],
        folder, min(timeout_sec, 180), capture_stdout=True, disk_limit=disk_limit,
    )
    return package_root if code == 0 and (package_root / "yt_dlp" / "__main__.py").is_file() else None


async def _run_ytdlp(
    url: str, folder: Path, settings: Settings, state: UpdateState, *,
    selector: str | None = None, metadata: bool = False, generic: bool = False, cookies: Path | None = None,
) -> tuple[int, str, str]:
    return await _run_process(
        _yt_dlp_args(url, selector=selector, metadata=metadata, generic=generic, cookies=cookies, package_root=state.package_root),
        folder, settings.timeout_sec, capture_stdout=metadata, disk_limit=settings.max_file_mb * 1_000_000 * 3,
        env=_package_env(state.package_root),
    )


async def _metadata(
    url: str, folder: Path, settings: Settings, cookies: Path | None, update_state: UpdateState
) -> dict[str, Any]:
    for generic in (False, True):
        attempts = 0
        while True:
            code, output, stderr = await _run_ytdlp(url, folder, settings, update_state, metadata=True, generic=generic, cookies=cookies)
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
            if error.extractor_failure and not update_state.attempted:
                update_state.attempted = True
                update_state.package_root = await _update_ytdlp(folder, settings.timeout_sec, settings.max_file_mb * 3_000_000)
                if update_state.package_root:
                    # One update-triggered retry, then generic fallback if needed.
                    code, output, stderr = await _run_ytdlp(url, folder, settings, update_state, metadata=True, generic=generic, cookies=cookies)
                    if code == 0:
                        return json.loads(output)
                    error = classify_ytdlp_error(stderr)
                    _log_error(url, error.category)
            if generic or not error.extractor_failure:
                raise DownloadProblem(error.user_message, error.category)
            break
    raise DownloadProblem("Этот сайт не удалось обработать.", "unsupported")


def _log_error(url: str, category: str) -> None:
    host = urlsplit(url).hostname or "unknown"
    LOG.warning("download failed category=%s domain=%s", category, host[:253])


async def _probe(path: Path, folder: Path, timeout_sec: int, disk_limit: int) -> dict[str, Any]:
    code, output, _stderr = await _run_process(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        folder, min(timeout_sec, 60), capture_stdout=True, disk_limit=disk_limit,
    )
    if code:
        raise DownloadProblem("Не удалось прочитать параметры видео.")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise DownloadProblem("Не удалось прочитать параметры видео.") from exc


def _video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    stream = next((item for item in probe.get("streams", []) if item.get("codec_type") == "video"), None)
    if stream is None:
        raise DownloadProblem("В ссылке не найден видеопоток.")
    return stream


def _ffmpeg_args(source: Path, output: Path, *, transcode: bool, video_bitrate: int | None, has_audio: bool) -> list[str]:
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if transcode:
        command.extend(["-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-vf", "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'"])
        if video_bitrate is not None:
            command.extend(["-b:v", str(video_bitrate), "-maxrate", str(video_bitrate), "-bufsize", str(video_bitrate * 2)])
        else:
            command.extend(["-crf", "27", "-maxrate", "1800k", "-bufsize", "3600k"])
        if has_audio:
            command.extend(["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"])
        else:
            command.append("-an")
    else:
        command.extend(["-map", "0", "-c", "copy"])
    command.extend(["-movflags", "+faststart", "-threads", "2", str(output)])
    if os.name != "nt":
        return ["nice", "-n", "10", *command]
    return command


async def _normalize_video(source: Path, folder: Path, settings: Settings, duration_hint: float | None) -> tuple[Path, dict[str, Any]]:
    max_bytes = settings.max_file_mb * 1_000_000
    probe = await _probe(source, folder, settings.timeout_sec, max_bytes * 3)
    video = _video_stream(probe)
    audio_exists = any(item.get("codec_type") == "audio" for item in probe.get("streams", []))
    duration_value = (probe.get("format", {}).get("duration") or duration_hint or 0)
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        duration = 0
    codecs_ok = video.get("codec_name") == "h264" and (not audio_exists or any(
        item.get("codec_type") == "audio" and item.get("codec_name") == "aac" for item in probe.get("streams", [])
    ))
    remux_only = source.suffix.lower() == ".mp4" and codecs_ok
    output = folder / "normalized.mp4"
    bitrate = max(64_000, int((max_bytes * 8 / duration) * 0.88) - (64_000 if audio_exists else 0)) if duration > 0 else None
    factors = (1.0, 0.78, 0.60)
    for factor in factors:
        scaled_bitrate = int(bitrate * factor) if bitrate else None
        code, _out, _err = await _run_process(
            _ffmpeg_args(source, output, transcode=not remux_only or (source.stat().st_size > max_bytes), video_bitrate=scaled_bitrate, has_audio=audio_exists),
            folder, settings.timeout_sec, disk_limit=max_bytes * 3,
        )
        if code == 0 and output.is_file() and output.stat().st_size <= max_bytes:
            final_probe = await _probe(output, folder, settings.timeout_sec, max_bytes * 3)
            return output, final_probe
        output.unlink(missing_ok=True)
    raise DownloadProblem("Видео не помещается в лимит Telegram даже после сжатия. Для больших файлов нужен режим large.")


async def _make_thumbnail(video: Path, folder: Path, timeout_sec: int, disk_limit: int) -> Path | None:
    thumbnail = folder / "thumbnail.jpg"
    code, _out, _err = await _run_process(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "4", str(thumbnail)],
        folder, min(timeout_sec, 60), disk_limit=disk_limit,
    )
    if code or not thumbnail.is_file() or thumbnail.stat().st_size > 200_000:
        thumbnail.unlink(missing_ok=True)
        return None
    return thumbnail


async def _download_gallery(url: str, folder: Path, settings: Settings, cookies: Path | None = None) -> DownloadedMedia:
    args = ["gallery-dl", "--no-input", "--destination", str(folder), "--range", f"1-{settings.max_items}"]
    if cookies:
        args.extend(["--cookies", str(cookies)])
    args.append(url)
    code, _output, stderr = await _run_process(
        [sys.executable, "-m", "gallery_dl", *args[1:]], folder, settings.timeout_sec,
        capture_stdout=True, disk_limit=settings.max_file_mb * 3_000_000,
    )
    source = urlsplit(url).hostname or ""
    if code:
        error = classify_ytdlp_error(stderr)
        _log_error(url, error.category)
        raise DownloadProblem(error.user_message, error.category)
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    video_suffixes = {".mp4", ".mkv", ".webm", ".mov"}
    paths = sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in image_suffixes | video_suffixes),
        key=lambda path: str(path.relative_to(folder)).lower(),
    )[:settings.max_items]
    items = []
    for index, path in enumerate(paths):
        if path.suffix.lower() in image_suffixes:
            items.append(DownloadedItem("photo", path))
            continue
        normalized, probe = await _normalize_video(path, folder, settings, None)
        video = _video_stream(probe)
        duration = round(float(probe.get("format", {}).get("duration") or 0))
        thumbnail = await _make_thumbnail(normalized, folder, settings.timeout_sec, settings.max_file_mb * 3_000_000)
        items.append(DownloadedItem("video", normalized, int(video.get("width") or 0), int(video.get("height") or 0), max(0, duration), thumbnail))
    if not items:
        error = classify_ytdlp_error(stderr)
        if error.category != "extractor":
            raise DownloadProblem(error.user_message, error.category)
        raise DownloadProblem("На странице не найдено доступных фото или видео.", "unsupported")
    return DownloadedMedia(tuple(items), "Медиа с сайта", source)


async def _fetch_video(url: str, folder: Path, settings: Settings) -> DownloadedMedia:
    folder.mkdir(parents=True, exist_ok=True)
    cookie_file = _write_cookie_file(folder, getattr(settings, "cookies_b64", None))
    max_bytes = settings.max_file_mb * 1_000_000
    update_state = UpdateState(attempted=_STARTUP_PACKAGE_ROOT is not None, package_root=_STARTUP_PACKAGE_ROOT)
    try:
        metadata = await _metadata(url, folder, settings, cookie_file, update_state)
        selection = select_format(metadata.get("formats", []), max_bytes)
    except DownloadProblem as exc:
        if exc.category in {"unsupported", "extractor", "unknown"}:
            return await _download_gallery(url, folder, settings, cookie_file)
        raise
    for attempt in range(3):
        code, _output, stderr = await _run_ytdlp(url, folder, settings, update_state, selector=selection.format_selector)
        if code == 0:
            break
        error = classify_ytdlp_error(stderr)
        _log_error(url, error.category)
        if error.extractor_failure and not update_state.attempted:
            update_state.attempted = True
            update_state.package_root = await _update_ytdlp(folder, settings.timeout_sec, max_bytes * 3)
            if update_state.package_root:
                code, _output, stderr = await _run_ytdlp(url, folder, settings, update_state, selector=selection.format_selector)
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
    title = str(metadata.get("title") or "Видео")[:300]
    normalized, probe = await _normalize_video(output[0], folder, settings, metadata.get("duration"))
    video = _video_stream(probe)
    duration = round(float(probe.get("format", {}).get("duration") or metadata.get("duration") or 0))
    thumbnail = await _make_thumbnail(normalized, folder, settings.timeout_sec, max_bytes * 3)
    return DownloadedMedia(
        items=(DownloadedItem("video", normalized, int(video.get("width") or 0), int(video.get("height") or 0), max(0, duration), thumbnail),),
        title=title, source=urlsplit(url).hostname or "",
    )


async def fetch_video(url: str, folder: Path, settings: Settings) -> DownloadedMedia:
    try:
        async with asyncio.timeout(settings.timeout_sec):
            return await _fetch_video(url, folder, settings)
    except TimeoutError as exc:
        raise DownloadProblem("Истекло общее время обработки ссылки.") from exc


async def update_ytdlp_runtime(settings: Settings) -> None:
    """Opt-in cold-start updater. Disabled by default to preserve fast wakeups."""
    global _STARTUP_PACKAGE_ROOT
    parent = Path(tempfile.mkdtemp(prefix="yt-dlp-runtime-"))
    updated = await _update_ytdlp(parent, settings.timeout_sec, settings.max_file_mb * 3_000_000)
    if updated is None:
        import shutil

        shutil.rmtree(parent, ignore_errors=True)
        LOG.warning("startup yt-dlp update failed")
        return
    _STARTUP_PACKAGE_ROOT = updated


def cleanup_ytdlp_runtime() -> None:
    """Remove the optional updated runtime after the worker has stopped."""
    global _STARTUP_PACKAGE_ROOT
    if _STARTUP_PACKAGE_ROOT is not None:
        import shutil

        shutil.rmtree(_STARTUP_PACKAGE_ROOT.parent, ignore_errors=True)
        _STARTUP_PACKAGE_ROOT = None
