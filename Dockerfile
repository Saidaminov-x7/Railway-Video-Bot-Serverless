FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" botuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py core.py jobs.py downloader.py ./
USER botuser
CMD ["python", "app.py"]
