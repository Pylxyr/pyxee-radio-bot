from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

load_dotenv(BASE_DIR / ".env")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_level_env(name: str, default: str) -> str:
    # print() is deliberate — this runs before configure_logging() exists.
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    if raw not in _VALID_LOG_LEVELS:
        print(f"WARNING: {name}={raw!r} is not a valid log level — using {default}.")
        return default
    return raw


def _check_cookies_path_writable(raw: str, path: Path) -> None:
    # yt-dlp saves this file back on every single extraction once configured
    # at all, and only data/ and logs/ are writable under the systemd unit's
    # hardening — checked here so a bad path fails loudly at startup instead
    # of on every !sr.
    cookies_dir = path.parent
    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but its directory ({cookies_dir}) "
            f"couldn't be created: {exc}. Use a path under data/ instead, e.g. "
            f"YTDLP_COOKIES_FILE=data/cookies.txt."
        ) from exc
    if not os.access(cookies_dir, os.W_OK):
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but {cookies_dir} isn't writable. "
            f"Use a path under data/ instead, e.g. YTDLP_COOKIES_FILE=data/cookies.txt."
        )


@dataclass(frozen=True, slots=True)
class Settings:
    # Twitch app credentials — from https://dev.twitch.tv/console/apps
    client_id: str
    client_secret: str
    bot_id: str
    owner_id: str
    prefix: str

    # Audio
    audio_bitrate_kbps: int

    # Local HTTP surface — serves /stream.mp3, /overlay, /nowplaying.json, /settings
    nowplaying_host: str
    nowplaying_port: int
    settings_password: str | None

    # Persistence — both under DATA_DIR so a single ReadWritePaths entry in
    # the systemd unit covers everything this process needs to write.
    token_path: Path
    tunables_path: Path

    # yt-dlp
    ytdlp_cookies_file: Path | None
    ytdlp_js_runtime_path: str | None
    ytdlp_js_runtime_name: str
    ytdlp_concurrency: int
    ytdlp_extract_timeout_seconds: int
    ytdlp_player_client: tuple[str, ...]
    ytdlp_cache_ttl_seconds: int

    # Logging
    log_level: str
    log_to_file: bool
    log_dir: Path


def load_settings() -> Settings:
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    def _required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is not set — add it to .env before starting. See .env.example.")
        return value

    def _required_numeric_id(name: str) -> str:
        # Real production failure this guards against: TWITCH_BOT_ID pasted
        # as "Twitch ID:1536026185" instead of just the digits — Helix
        # rejects that with a bare "Bad Identifiers" error.
        value = _required(name)
        if not value.isdigit():
            raise RuntimeError(
                f"{name}={value!r} isn't a plain numeric Twitch user ID — digits only, no "
                f"username, no label. Look one up at "
                f"https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/"
            )
        return value

    client_id = _required("TWITCH_CLIENT_ID")
    client_secret = _required("TWITCH_CLIENT_SECRET")
    bot_id = _required_numeric_id("TWITCH_BOT_ID")
    owner_id = _required_numeric_id("TWITCH_OWNER_ID")

    cookies_raw = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    cookies_path = (BASE_DIR / cookies_raw) if cookies_raw else None
    if cookies_path is not None:
        _check_cookies_path_writable(cookies_raw, cookies_path)

    player_client_raw = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
    if not player_client_raw and cookies_path is not None:
        # yt-dlp's own default client list when cookies are set (verified
        # against yt-dlp==2026.08.19) is
        # ('web_embedded', 'tv_downgraded', 'web') — tv_downgraded has a
        # known open bug (yt-dlp#17389, "The page needs to be reloaded").
        # Pinning to the other two already-default clients avoids it.
        player_client_raw = "web_embedded,web"
    ytdlp_player_client = tuple(c.strip() for c in player_client_raw.split(",") if c.strip())

    nowplaying_host = os.getenv("TWITCH_NOWPLAYING_HOST", "127.0.0.1").strip() or "127.0.0.1"
    settings_password = os.getenv("TWITCH_SETTINGS_PASSWORD", "").strip() or None
    if nowplaying_host not in ("127.0.0.1", "localhost") and settings_password is None:
        print(
            f"WARNING: TWITCH_NOWPLAYING_HOST={nowplaying_host!r} is reachable off this machine, "
            f"but TWITCH_SETTINGS_PASSWORD is unset — anyone who finds the port can change your "
            f"queue/cooldown settings via /settings. Set TWITCH_SETTINGS_PASSWORD."
        )

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        owner_id=owner_id,
        prefix=os.getenv("TWITCH_PREFIX", "!").strip() or "!",
        audio_bitrate_kbps=max(64, min(320, _int_env("AUDIO_BITRATE_KBPS", 128))),
        nowplaying_host=nowplaying_host,
        nowplaying_port=max(1024, min(65535, _int_env("TWITCH_NOWPLAYING_PORT", 8098))),
        settings_password=settings_password,
        token_path=DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip(),
        tunables_path=DATA_DIR / os.getenv("TWITCH_TUNABLES_FILE", "tunables.json").strip(),
        ytdlp_cookies_file=cookies_path,
        ytdlp_js_runtime_path=os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip() or None,
        ytdlp_js_runtime_name=os.getenv("YTDLP_JS_RUNTIME_NAME", "deno").strip() or "deno",
        ytdlp_concurrency=max(1, min(4, _int_env("YTDLP_CONCURRENCY", 2))),
        ytdlp_extract_timeout_seconds=max(10, min(120, _int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45))),
        ytdlp_player_client=ytdlp_player_client,
        # Skips the relay's second extraction (chat resolves once to queue,
        # then it re-resolves right before playing) for anything near the
        # front of the queue. 0 disables caching.
        ytdlp_cache_ttl_seconds=max(0, min(3600, _int_env("YTDLP_CACHE_TTL_SECONDS", 300))),
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )
