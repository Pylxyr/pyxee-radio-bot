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


def _check_cookies_path_writable(raw: str, path: Path) -> None:
    # yt-dlp only *reads* a configured cookiefile lazily, but it always
    # tries to *save* it back on every single extraction (every !sr) once
    # one is configured at all — see yt_dlp.YoutubeDL.close()/save_cookies().
    # Under the systemd unit's hardening (ProtectSystem=full), only
    # data/ and logs/ are writable; anything else — including the app's own
    # root directory, which is what a bare "cookies.txt" resolves to —
    # fails with a bare OSError deep inside yt-dlp on every request, which
    # is a confusing way to discover a one-line config mistake. Checked
    # once, up front, so it fails loudly at startup instead.
    cookies_dir = path.parent
    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but its directory ({cookies_dir}) "
            f"couldn't be created: {exc}. Point it at a path under data/ instead, e.g. "
            f"YTDLP_COOKIES_FILE=data/cookies.txt — that's the only directory this service's "
            f"systemd sandbox (see deploy/twitch-radio.service's ReadWritePaths) allows it to "
            f"write to. Leave YTDLP_COOKIES_FILE unset entirely unless you actually need "
            f"age-restricted or region-gated content — most requests don't."
        ) from exc
    if not os.access(cookies_dir, os.W_OK):
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but {cookies_dir} isn't writable by "
            f"this process. Point it at a path under data/ instead, e.g. "
            f"YTDLP_COOKIES_FILE=data/cookies.txt — that's the only directory this service's "
            f"systemd sandbox (see deploy/twitch-radio.service's ReadWritePaths) allows it to "
            f"write to. Leave YTDLP_COOKIES_FILE unset entirely unless you actually need "
            f"age-restricted or region-gated content — most requests don't."
        )


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
            raise RuntimeError(
                f"{name} is not set. This service has no optional/disabled mode — "
                f"add it to .env before starting. See .env.example."
            )
        return value

    def _required_numeric_id(name: str) -> str:
        # Real production failure this guards against: TWITCH_BOT_ID pasted
        # as "Twitch ID:1536026185" (label included) instead of just the
        # digits — Helix rejects that with a bare "Bad Identifiers" error
        # that gives no hint what's actually wrong with it. Caught here,
        # at startup, with a message that names the exact value so it's
        # obvious what to fix.
        value = _required(name)
        if not value.isdigit():
            raise RuntimeError(
                f"{name}={value!r} isn't a plain numeric Twitch user ID. It must be digits "
                f"only — no username, no label like 'Twitch ID:', nothing else. Look one up "
                f"from a username at "
                f"https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/"
            )
        return value

    client_id = _required("TWITCH_CLIENT_ID")
    client_secret = _required("TWITCH_CLIENT_SECRET")
    bot_id = _required_numeric_id("TWITCH_BOT_ID")
    owner_id = _required_numeric_id("TWITCH_OWNER_ID")
    stream_key = _required("TWITCH_STREAM_KEY")

    cookies_raw = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    cookies_path = (BASE_DIR / cookies_raw) if cookies_raw else None
    if cookies_path is not None:
        _check_cookies_path_writable(cookies_raw, cookies_path)

    player_client_raw = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
    if not player_client_raw and cookies_path is not None:
        # yt-dlp's own default client priority list when cookies are present
        # (verified against the installed yt-dlp==2026.08.19 source —
        # YoutubeIE._DEFAULT_AUTHED_CLIENTS) is
        # ('web_embedded', 'tv_downgraded', 'web'), and tv_downgraded
        # currently has a known, still-open breakage ("The page needs to be
        # reloaded" — see yt-dlp#17389) that a cookie-authenticated request
        # can fall through to. Pinning to the two *other* clients already in
        # that same default list — never changing which clients are
        # "trusted" for an authenticated session, just refusing to ever
        # reach the broken one — avoids it without guessing at some
        # unrelated client combination. Only applied as a default; an
        # explicit YTDLP_PLAYER_CLIENT always wins, and this is skipped
        # entirely when cookies aren't configured (yt-dlp's unauthenticated
        # default doesn't involve tv_downgraded at all).
        player_client_raw = "web_embedded,web"
    ytdlp_player_client = tuple(c.strip() for c in player_client_raw.split(",") if c.strip())

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
        ytdlp_player_client=ytdlp_player_client,
        # How long a resolved track is reused instead of running a second
        # full yt-dlp extraction. A !sr resolves once in chat (to confirm/
        # queue it) and again in the relay right before it actually plays —
        # identical work, ~15-20s each with a JS challenge solve involved,
        # for what's the same request arriving twice. YouTube's direct
        # media URLs are normally valid for hours, so this window is
        # deliberately much shorter than that: long enough to skip the
        # second extraction for a typical short queue, short enough that
        # anything sitting in a longer queue still gets a genuine
        # re-resolve (catching a video that went live/private/deleted in
        # the meantime) rather than reusing a resolve that's actually gone
        # stale. 0 disables caching entirely.
        ytdlp_cache_ttl_seconds=max(0, min(3600, _int_env("YTDLP_CACHE_TTL_SECONDS", 300))),
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )
