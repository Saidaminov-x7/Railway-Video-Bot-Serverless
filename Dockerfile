FROM python:3.12-slim
ARG YTDLP_VERSION=2026.10.26
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" botuser \
    && curl -fsSL https://deno.land/install.sh -o /tmp/deno-install.sh \
    && DENO_INSTALL=/usr/local sh /tmp/deno-install.sh \
    && rm /tmp/deno-install.sh
ENV YTDLP_AUTO_UPDATE=0
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default,curl-cffi]==${YTDLP_VERSION}"
COPY app.py core.py jobs.py downloader.py ./
USER botuser
CMD ["python", "app.py"]
