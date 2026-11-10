# Telegram video bot (Railway)

Python 3.12, aiogram 3 webhook bot with one in-memory FIFO worker. Downloads are temporary and deleted after each job. Access is limited to Telegram user IDs configured in Railway Variables. Use only for content you have the right to download.

## Current implementation status

This repository is **not yet production ready**. The current downloader supports yt-dlp video downloads with a job timeout, download retry settings, disk-size checks, and temporary-directory cleanup. It does not yet implement all requested behavior: metadata-based format selection/transcoding, cookies, gallery/carousel delivery, media probing/thumbnails, classified extractor errors, active-job cancellation, per-user rate limiting, and complete resource caps. Do not rely on the bot for arbitrary URLs until redirect and DNS protections have been independently validated against real sites.

No site matrix is claimed here: network downloads and Telegram sends have not been run in this development session. Public site access varies by region, account state, platform changes, and yt-dlp extractor version.

## Local setup

Python 3.12 and FFmpeg/ffprobe are required. Install and run the local checks:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py core.py jobs.py downloader.py
```

Set the environment variables below before running `python app.py`. Do not commit `.env` or any real credentials.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | yes | BotFather token; secret |
| `TELEGRAM_ID_1`, `TELEGRAM_ID_2`, … | yes | Positive numeric Telegram user IDs allowed to use the bot |
| `WEBHOOK_SECRET` | yes | 16–256 URL-safe characters used to authenticate Telegram webhooks |
| `WEBHOOK_BASE_URL` | one of these | Public HTTPS origin for the webhook |
| `RAILWAY_PUBLIC_DOMAIN` | one of these | Used to construct the webhook origin when `WEBHOOK_BASE_URL` is unset |
| `BOT_MODE` | no | `eco` (default) or `large` |
| `TELEGRAM_API_BASE` | large only | Private Local Bot API service URL |
| `MAX_FILE_MB` | no | Upload limit; defaults to 45 MB (`eco`) or 1900 MB (`large`) |
| `MAX_QUEUE` | no | Bounded queue capacity; default 20 |
| `DOWNLOAD_TIMEOUT` | no | Per-download seconds; default 3600 |
| `UPLOAD_TIMEOUT` | no | Telegram upload timeout; default 3600 |
| `ALLOWED_DOMAINS` | no | Optional comma-separated host suffix allowlist; unset means any syntactically public URL |
| `BLOCKED_DOMAINS` | no | Comma-separated host suffix denylist |

Never place cookies or private content credentials in a URL. Cookies support is not implemented yet.

## Railway deployment notes

Build from the repository Dockerfile, expose the configured `PORT`, set one replica, and configure a public domain before starting the bot. The image installs FFmpeg, Deno, and the pinned yt-dlp version. Override the Docker build argument `YTDLP_VERSION` to select another release. `requirements.txt` lists the runtime extras and unpinned local development dependency; the Docker argument pins the production image. Current pin: `2026.10.26`.

Webhook delivery may have a cold-start delay or a 502 on the first request; confirm actual retry and wake behavior in Railway logs and Metrics before relying on Serverless. This repository has not been deployed or observed on Railway as part of this work. Do not add external uptime pings. The bot checks webhook configuration on startup and has an in-memory queue, so pending jobs are lost if the process restarts.

`large` requires a separate Local Bot API service with valid Telegram API ID/hash configuration and a one-time cloud `logOut` migration when switching API servers. Large uploads and Serverless interaction with that service have not been tested here; do not enable sleeping on the Local Bot API without an end-to-end wake/delivery test. The present code does not establish a tested >50 MB path.

## Tests and limitations

The default unit suite is local and mocked. Network tests, actual Telegram delivery, large-file upload, Railway sleep/wake behavior, and site-by-site results remain unverified. `ruff` and the Docker build are configured in GitHub Actions, but must pass in CI before release.

yt-dlp is installed with `default` and `curl-cffi` extras, plus `yt-dlp-ejs`; YouTube currently needs a supported JavaScript runtime such as Deno for full extractor functionality. Browser impersonation and extractor success are not guaranteed for any individual site. DRM, access-control circumvention, and unauthorized content are out of scope.
