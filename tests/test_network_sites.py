"""Opt-in real-site download smoke tests. Set RUN_NETWORK_TESTS=1 to enable."""
import os
import tempfile
from pathlib import Path

import pytest

from core import Settings
from downloader import fetch_video


SITES = ("YOUTUBE", "TIKTOK", "INSTAGRAM", "PINTEREST", "X", "FACEBOOK", "VIMEO", "REDDIT", "DAILYMOTION")


def _settings() -> Settings:
    return Settings.from_env({
        "BOT_TOKEN": "123456789:" + "X" * 35,
        "TELEGRAM_ID_1": "123456789",
        "WEBHOOK_SECRET": "S" * 32,
        "WEBHOOK_BASE_URL": "https://example.com",
        "DOWNLOAD_TIMEOUT": "300",
        "UPLOAD_TIMEOUT": "300",
    })


@pytest.mark.network
@pytest.mark.parametrize("site", SITES, ids=SITES)
@pytest.mark.asyncio
async def test_public_site_download(site: str) -> None:
    if os.getenv("RUN_NETWORK_TESTS") != "1":
        pytest.skip("Set RUN_NETWORK_TESTS=1 for real public-site downloads")
    url = os.getenv(f"NETWORK_URL_{site}", "").strip()
    if not url:
        pytest.skip(f"NETWORK_URL_{site} is not configured with a lawful public test URL")
    with tempfile.TemporaryDirectory(prefix="tg-video-network-") as temp:
        result = await fetch_video(url, Path(temp), _settings())
        assert result.items
        assert all(item.path.is_file() for item in result.items)
