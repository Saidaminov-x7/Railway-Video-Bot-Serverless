# Telegram video bot (Railway)

Python 3.12, aiogram 3 webhook bot with one in-memory FIFO worker. Downloads are temporary and deleted after each job. Access is limited to Telegram user IDs configured in Railway Variables. Use only for content you have the right to download.

## Implemented behavior and verification status

The bot uses one bounded global FIFO worker, private-chat access by Telegram ID, webhook delivery, and temporary per-job files. The downloader inspects yt-dlp metadata, selects a known-size format under the configured cap when possible, retries transient network failures, classifies common extractor errors, and can use generic extraction and gallery-dl. FFmpeg normalizes video to MP4/H.264/AAC with faststart and an eco size target; ffprobe dimensions, duration, and a small thumbnail are passed to Telegram. Photo and mixed video/photo collections are delivered as albums, capped by `MAX_ITEMS`.

Cookies are optional and intended only for the owner's lawful access. They are written with mode 0600 under the job's temporary directory and removed with it. The application removes its own orphaned `tg-video-*` directories at startup. `YTDLP_AUTO_UPDATE=1` opts into a startup update and can increase cold-start time; leave it disabled for normal Serverless use. Extractor failures can trigger one per-job yt-dlp update attempt.

No site download or Telegram delivery is marked successful unless it has actually been run. The table below is updated from real opt-in tests; current local test results and untested platforms are reported at the end of a development run, not inferred from yt-dlp's extractor list.

| Site | Real download result in this environment |
| --- | --- |
| YouTube | **Failed / inconclusive:** two public sample URLs were attempted; one returned unavailable, and extraction could not complete because Deno is not installed in the local Windows environment. This does not establish platform-wide failure. |
| TikTok | Not tested: no lawful public test URL configured |
| Instagram | Not tested: no lawful public test URL configured |
| Pinterest | Not tested: no lawful public test URL configured |
| X / Twitter | Not tested: no lawful public test URL configured |
| Facebook | Not tested: no lawful public test URL configured |
| Vimeo | Not tested: no lawful public test URL configured |
| Reddit | Not tested: no lawful public test URL configured |
| Dailymotion | Not tested: no lawful public test URL configured |

No real Telegram send has been performed; no test bot credentials were configured. Opt-in pytest cases exist for a real send and two real downloads through the FIFO worker. Both require explicit test URLs and a dedicated test bot/private chat.

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
| `MAX_FILE_MB` | no | Upload limit; defaults to 49 MB (`eco`) or 1950 MB (`large`) |
| `MAX_QUEUE` | no | Bounded queue capacity; default 20 |
| `DOWNLOAD_TIMEOUT` | no | Per-download seconds; default 3600 |
| `UPLOAD_TIMEOUT` | no | Telegram upload timeout; default 3600 |
| `MAX_ITEMS` | no | Maximum media files from one gallery; default 10 |
| `RATE_LIMIT_PER_MIN` | no | Maximum accepted links per user per minute; default 10 |
| `COOKIES_B64` | no | Base64 `cookies.txt` (up to 1 MB), used only for content the owner may access; written mode `0600` in task temp and deleted afterwards |
| `YTDLP_AUTO_UPDATE` | no | `0` by default; `1` opts in to an update at startup |
| `ALLOWED_DOMAINS` | no | Optional comma-separated host suffix allowlist; unset means any syntactically public URL |
| `BLOCKED_DOMAINS` | no | Comma-separated host suffix denylist |

Never place cookies or private content credentials in a URL. Cookie import is optional and does not bypass DRM or access restrictions.

## Railway deployment notes

Build from the repository Dockerfile, expose the configured `PORT`, set one replica, and configure a public domain before starting the bot. The image installs FFmpeg, Deno `2.9.8`, and yt-dlp `2026.10.26`. `requirements.txt` and the Docker build argument `YTDLP_VERSION` are checked against the same version so a mismatch fails the image build. When upgrading yt-dlp, update the pin in both places; update `DENO_VERSION` independently when upgrading Deno.

Webhook delivery may have a cold-start delay or a 502 on the first request; confirm actual retry and wake behavior in Railway logs and Metrics before relying on Serverless. This repository has not been deployed or observed on Railway as part of this work. Do not add external uptime pings. The bot checks webhook configuration on startup and has an in-memory queue, so pending jobs are lost if the process restarts.

`large` requires a separate Local Bot API service with valid Telegram API ID/hash configuration and a one-time cloud `logOut` migration when switching API servers. Large uploads and Serverless interaction with that service have not been tested here; do not enable sleeping on the Local Bot API without an end-to-end wake/delivery test. The present code does not establish a tested >50 MB path.

## Tests and limitations

Unit tests and local mocked webhook/queue tests run without external credentials. Real-site smoke tests are tagged `network`, disabled by default, and require a lawful public sample URL for each platform via `NETWORK_URL_YOUTUBE`, `NETWORK_URL_TIKTOK`, `NETWORK_URL_INSTAGRAM`, `NETWORK_URL_PINTEREST`, `NETWORK_URL_X`, `NETWORK_URL_FACEBOOK`, `NETWORK_URL_VIMEO`, `NETWORK_URL_REDDIT`, and `NETWORK_URL_DAILYMOTION`. Run them separately with `RUN_NETWORK_TESTS=1 pytest -m network -v`. Public access can vary by region, account state, rate limits, and platform changes. A passing download is not evidence of successful delivery to Telegram.

Real Telegram delivery and two-link FIFO timing require a test bot and a test user's authorization; these must be run only in an explicitly configured test environment. Railway wake/sleep, Docker build, and large-file upload also require their real target environments. DRM and access-control circumvention are out of scope.

yt-dlp is installed with `default` and `curl-cffi` extras, plus `yt-dlp-ejs`; YouTube currently needs a supported JavaScript runtime such as Deno for full extractor functionality. Browser impersonation and extractor success are not guaranteed for any individual site. DRM, access-control circumvention, and unauthorized content are out of scope.
