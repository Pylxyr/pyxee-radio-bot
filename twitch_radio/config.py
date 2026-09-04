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
    # logging.Logger.setLevel() raises ValueError on an unrecognized level
    # name — a typo'd LOG_LEVEL shouldn't be able to crash startup before
    # logging even exists to explain why. print() here is deliberate: this
    # runs before configure_logging() has set anything up.
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    if raw not in _VALID_LOG_LEVELS:
        print(f"WARNING: {name}={raw!r} is not a valid log level — using {default}.")
        return default
    return raw


@dataclass(frozen=True, slots=True)
class Settings:
    # Twitch app credentials — from https://dev.twitch.tv/console/apps
    client_id: str
    client_secret: str
    bot_id: str
    owner_id: str
    stream_key: str
    ingest_url: str
    prefix: str

    # Video track
    background_image: Path
    video_bitrate_kbps: int
    video_fps: int

    # Local HTTP surface
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
            raise RuntimeError(
                f"{name} is not set. This service has no optional/disabled mode — "
                f"add it to .env before starting. See .env.example."
            )
        return value

    client_id = _required("TWITCH_CLIENT_ID")
    client_secret = _required("TWITCH_CLIENT_SECRET")
    bot_id = _required("TWITCH_BOT_ID")
    owner_id = _required("TWITCH_OWNER_ID")
    stream_key = _required("TWITCH_STREAM_KEY")

    cookies_raw = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    cookies_path = (BASE_DIR / cookies_raw) if cookies_raw else None

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        owner_id=owner_id,
        stream_key=stream_key,
        ingest_url=os.getenv("TWITCH_INGEST_URL", "rtmp://live.twitch.tv/app").strip()
        or "rtmp://live.twitch.tv/app",
        prefix=os.getenv("TWITCH_PREFIX", "!").strip() or "!",
        background_image=BASE_DIR / os.getenv("TWITCH_BACKGROUND_IMAGE", "deploy/background.png").strip(),
        video_bitrate_kbps=max(300, min(3000, _int_env("TWITCH_VIDEO_BITRATE_KBPS", 800))),
        video_fps=max(1, min(10, _int_env("TWITCH_VIDEO_FPS", 2))),
        nowplaying_host=os.getenv("TWITCH_NOWPLAYING_HOST", "127.0.0.1").strip() or "127.0.0.1",
        nowplaying_port=max(1024, min(65535, _int_env("TWITCH_NOWPLAYING_PORT", 8098))),
        settings_password=os.getenv("TWITCH_SETTINGS_PASSWORD", "").strip() or None,
        token_path=DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip(),
        tunables_path=DATA_DIR / os.getenv("TWITCH_TUNABLES_FILE", "tunables.json").strip(),
        ytdlp_cookies_file=cookies_path,
        ytdlp_js_runtime_path=os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip() or None,
        # Only matters when YTDLP_JS_RUNTIME_PATH is also set — it tells
        # yt-dlp what kind of binary that path is. Defaults to "deno" since
        # that's what setup.sh actually installs.
        ytdlp_js_runtime_name=os.getenv("YTDLP_JS_RUNTIME_NAME", "deno").strip() or "deno",
        # No Discord process to contend with anymore — this is the only
        # consumer of yt-dlp in this service, so a single modest concurrency
        # knob is enough (unlike the Discord bot's tiered guild/curation/
        # playback semaphores, which existed specifically to prevent
        # cross-feature contention within one shared process).
        ytdlp_concurrency=max(1, min(4, _int_env("YTDLP_CONCURRENCY", 2))),
        ytdlp_extract_timeout_seconds=max(10, min(120, _int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45))),
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )
