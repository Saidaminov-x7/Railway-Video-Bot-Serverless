"""Pure, dependency-free configuration, authentication and URL validation."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

TELEGRAM_ID_KEY = re.compile(r"TELEGRAM_ID_[1-9][0-9]*", flags=re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)
DOMAIN_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", flags=re.IGNORECASE)
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,256}")


def list_telegram_ids(env: dict[str, str]) -> frozenset[int]:
    """Allow only decimal positive Telegram user IDs, never usernames or chat titles."""
    users: set[int] = set()
    for key, raw in env.items():
        if not TELEGRAM_ID_KEY.fullmatch(key):
            continue
        value = raw.strip()
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > (2**63 - 1):
            raise ValueError(f"{key}: требуется положительный числовой Telegram ID")
        users.add(int(value))
    return frozenset(users)


def site_allowlist(env: dict[str, str]) -> tuple[str, ...]:
    raw = env.get("ALLOWED_DOMAINS", "").strip()
    if not raw:
        return ()
    domains = _domain_list(raw, "ALLOWED_DOMAINS")
    if not domains:
        raise ValueError("ALLOWED_DOMAINS: ожидается список доменов через запятую")
    return domains


def _domain_list(raw: str, name: str) -> tuple[str, ...]:
    domains = tuple(part.strip().lower().strip(".") for part in raw.split(",") if part.strip())
    if any(not DOMAIN_RE.fullmatch(domain) for domain in domains):
        raise ValueError(f"{name}: ожидается список доменов через запятую")
    return domains


def valid_url(url: str, domains: tuple[str, ...] = (), blocked: tuple[str, ...] = ()) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            return False
        host = parsed.hostname.lower().rstrip(".")
        if len(url) > 2048 or host.startswith("-") or host == "localhost" or host.endswith((".localhost", ".local", ".railway.internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return False
        except ValueError:
            # Reject historical integer/octal/hex IPv4 spellings accepted by libc.
            if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", host, re.IGNORECASE):
                try:
                    if not ipaddress.ip_address(socket.inet_aton(host)).is_global:  # type: ignore[arg-type]
                        return False
                except (OSError, ValueError):
                    return False
        def matches(entries: tuple[str, ...]) -> bool:
            return any(host == dom or host.endswith("." + dom) for dom in entries)

        return not matches(blocked) and (not domains or matches(domains))
    except ValueError:
        return False


async def public_url(url: str, domains: tuple[str, ...] = (), blocked: tuple[str, ...] = ()) -> bool:
    """Validate the URL and every redirect target's DNS answers before downloading."""
    import aiohttp

    current = url
    timeout = aiohttp.ClientTimeout(total=12, connect=4)
    for _ in range(6):
        if not valid_url(current, domains, blocked):
            return False
        host = urlsplit(current).hostname
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror):
            return False
        if not answers or any(not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in answers):
            return False
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session, session.head(
                current, allow_redirects=False
            ) as response:
                if response.status not in {301, 302, 303, 307, 308}:
                    return urlsplit(current).scheme == "https"
                location = response.headers.get("Location")
                if not location:
                    return False
                from yarl import URL
                current = str(response.url.join(URL(location)))
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return False
    return False


def extract_links(text: str, domains: tuple[str, ...] = (), max_links: int = 10, blocked: tuple[str, ...] = ()) -> tuple[list[str], int]:
    accepted: list[str] = []
    rejected = 0
    for match in URL_RE.finditer(text):
        url = match.group().rstrip(".,;!?)]}\"'")
        if len(url) > 2048 or not valid_url(url, domains, blocked):
            rejected += 1
        elif url in accepted:
            continue
        elif len(accepted) == max_links:
            rejected += 1
        else:
            accepted.append(url)
    return accepted, rejected


def _positive_int(env: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(env.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key}: требуется целое число") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{key}: допустимо {minimum}..{maximum}")
    return number


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_ids: frozenset[int]
    webhook_url: str
    webhook_secret: str
    mode: str
    local_api_url: str
    domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]
    port: int
    max_file_mb: int
    max_queue: int
    timeout_sec: int
    upload_timeout_sec: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        values = dict(os.environ if env is None else env)
        token = values.get("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d{5,}:[a-zA-Z0-9_-]{20,}", token):
            raise ValueError("BOT_TOKEN: некорректный токен BotFather")
        allowed_ids = list_telegram_ids(values)
        if not allowed_ids:
            raise ValueError("Нужен хотя бы один TELEGRAM_ID_1 / TELEGRAM_ID_2 / ...")
        secret = values.get("WEBHOOK_SECRET", "").strip()
        if not SECRET_RE.fullmatch(secret):
            raise ValueError("WEBHOOK_SECRET: 16–256 символов [A-Za-z0-9_-]")
        public = values.get("WEBHOOK_BASE_URL", "").strip().rstrip("/")
        if not public and values.get("RAILWAY_PUBLIC_DOMAIN"):
            public = "https://" + values["RAILWAY_PUBLIC_DOMAIN"].strip().strip("/")
        parsed = urlsplit(public)
        if not public or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("WEBHOOK_BASE_URL: требуется публичный адрес https://...")
        mode = values.get("BOT_MODE", "eco").lower().strip()
        if mode not in {"eco", "large"}:
            raise ValueError("BOT_MODE: eco или large")
        local_api = values.get("TELEGRAM_API_BASE", "").strip().rstrip("/")
        if mode == "large":
            parsed_api = urlsplit(local_api)
            if parsed_api.scheme != "http" or not parsed_api.hostname or parsed_api.username or parsed_api.password:
                raise ValueError("TELEGRAM_API_BASE: укажи внутренний http://...:8081")
            # Reject public bot API here, which would silently break uploads >50 MB.
            if not parsed_api.hostname.endswith(".railway.internal") and parsed_api.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("TELEGRAM_API_BASE должен указывать на приватный сервис Railway")
        default_mb = 45 if mode == "eco" else 1900
        return cls(
            token=token,
            allowed_user_ids=allowed_ids,
            webhook_url=public + "/telegram-webhook",
            webhook_secret=secret,
            mode=mode,
            local_api_url=local_api,
            domains=site_allowlist(values),
            blocked_domains=_domain_list(values.get("BLOCKED_DOMAINS", ""), "BLOCKED_DOMAINS"),
            port=_positive_int(values, "PORT", 8080, 1, 65535),
            max_file_mb=_positive_int(values, "MAX_FILE_MB", default_mb, 1, 45 if mode == "eco" else 1950),
            max_queue=_positive_int(values, "MAX_QUEUE", 20, 1, 200),
            timeout_sec=_positive_int(values, "DOWNLOAD_TIMEOUT", 3600, 60, 14400),
            upload_timeout_sec=_positive_int(values, "UPLOAD_TIMEOUT", 3600, 60, 14400),
        )
