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
        # print(), not log — this runs before configure_logging() exists.
        print(f"WARNING: {name}={raw!r} is not a valid integer — using {default}.")
        return default


_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    print(f"WARNING: {name}={raw!r} is not a recognized boolean — using {default}.")
    return default


def _clamped_int_env(name: str, default: int, lo: int, hi: int) -> int:
    value = _int_env(name, default)
    clamped = max(lo, min(hi, value))
    if clamped != value:
        print(f"WARNING: {name}={value} is outside the allowed range {lo}-{hi} — using {clamped}.")
    return clamped


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_level_env(name: str, default: str) -> str:
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    if raw not in _VALID_LOG_LEVELS:
        print(f"WARNING: {name}={raw!r} is not a valid log level — using {default}.")
        return default
    return raw


def _check_cookies_path_writable(raw: str, path: Path) -> None:
    # Only data/ and logs/ are writable under the systemd unit's hardening,
    # and yt-dlp rewrites this file on every extraction — fail loudly at
    # startup instead of on every !sr.
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


# yt-dlp player_client names and whether each accepts cookie auth (mirrors
# yt_dlp.extractor.youtube._base.INNERTUBE_CLIENTS[*]["SUPPORTS_COOKIES"]).
# Hardcoded since that's a private yt-dlp module; re-verify against the
# pinned yt-dlp version in requirements.txt if this needs updating.
# Checked against yt-dlp==2026.08.19.
_VALID_PLAYER_CLIENTS = {
    "web": True, "web_safari": True, "web_embedded": True, "web_music": True,
    "web_creator": True, "android": False, "android_vr": False, "ios": False,
    "visionos": False, "mweb": True, "tv": True, "tv_downgraded": True, "tv_simply": False,
}


def _check_player_clients(raw_clients: tuple[str, ...], cookies_configured: bool) -> None:
    unknown = [c for c in raw_clients if c not in _VALID_PLAYER_CLIENTS]
    if unknown:
        print(
            f"WARNING: YTDLP_PLAYER_CLIENT has unrecognized client name(s) {unknown} — yt-dlp "
            f"will just skip them with a warning. Valid names: {sorted(_VALID_PLAYER_CLIENTS)}"
        )
    if cookies_configured and raw_clients:
        cookie_ok = [c for c in raw_clients if _VALID_PLAYER_CLIENTS.get(c)]
        if not cookie_ok:
            raise RuntimeError(
                f"YTDLP_PLAYER_CLIENT={','.join(raw_clients)!r} has no client that supports "
                f"cookie auth, but YTDLP_COOKIES_FILE is set — every client gets skipped and "
                f"every request fails. android/android_vr/ios/visionos/tv_simply all reject "
                f"cookies outright; mix in at least one of web/web_safari/web_embedded/"
                f"web_music/web_creator/mweb/tv/tv_downgraded, or unset YTDLP_COOKIES_FILE."
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
    # If True, don't start a new track while nobody's subscribed to
    # /stream.mp3 — holds at the current boundary and resumes once someone
    # (re)connects. A track already playing finishes normally either way.
    # Off by default (the queue has always run on a real-time clock
    # regardless of listeners); opt in via PAUSE_QUEUE_WHEN_NO_LISTENERS=true.
    pause_when_no_listeners: bool

    # Local HTTP surface — serves /stream.mp3, /overlay, /nowplaying.json, /settings
    nowplaying_host: str
    nowplaying_port: int
    settings_password: str | None
    # Externally-reachable base URL for the public /commands page (no
    # trailing slash), e.g. "https://radio.example.com" or
    # "http://203.0.113.5:8098". None if unset — nowplaying_host is almost
    # always 127.0.0.1 or 0.0.0.0, neither of which means anything typed
    # into a browser on someone else's machine, so it can't be derived
    # automatically the way the other local endpoints are. Unset, !commands
    # falls back to the old terse in-chat listing instead of a broken link.
    public_base_url: str | None
    # Both set, or both None — parsed together below and only accepted as a
    # pair, since aiohttp's load_cert_chain needs both to do anything.
    tls_cert_file: Path | None
    tls_key_file: Path | None

    # Persistence — all under DATA_DIR so one ReadWritePaths entry in the
    # systemd unit covers everything this process writes.
    token_path: Path
    tunables_path: Path
    blocklist_path: Path
    specs_path: Path
    toggles_path: Path
    db_path: Path

    # yt-dlp
    ytdlp_cookies_file: Path | None
    ytdlp_js_runtime_path: str | None
    ytdlp_js_runtime_name: str
    ytdlp_concurrency: int
    ytdlp_extract_timeout_seconds: int
    ytdlp_player_client: tuple[str, ...]
    ytdlp_cache_ttl_seconds: int
    ytdlp_pot_provider_url: str | None
    # "process" (default) runs extraction in long-lived child processes;
    # "thread" is the original in-process ThreadPoolExecutor path, kept as
    # an escape hatch. See extraction.py for the trade-off.
    ytdlp_worker_mode: str

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
        # Guards against e.g. TWITCH_BOT_ID pasted as "Twitch ID:1536026185"
        # instead of just the digits — Helix rejects that with a bare
        # "Bad Identifiers" error.
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
        # against yt-dlp==2026.08.19) is ('web_embedded', 'tv_downgraded',
        # 'web') — tv_downgraded has a known open bug (yt-dlp#17389). Pin
        # to the other two already-default clients to avoid it.
        player_client_raw = "web_embedded,web"
    ytdlp_player_client = tuple(c.strip() for c in player_client_raw.split(",") if c.strip())
    _check_player_clients(ytdlp_player_client, cookies_configured=cookies_path is not None)

    worker_mode = os.getenv("YTDLP_WORKER_MODE", "process").strip().lower() or "process"
    if worker_mode not in ("process", "thread"):
        print(f"WARNING: YTDLP_WORKER_MODE={worker_mode!r} is not 'process' or 'thread' — using 'process'.")
        worker_mode = "process"

    nowplaying_host = os.getenv("TWITCH_NOWPLAYING_HOST", "127.0.0.1").strip() or "127.0.0.1"
    settings_password = os.getenv("TWITCH_SETTINGS_PASSWORD", "").strip() or None
    if nowplaying_host not in ("127.0.0.1", "localhost") and settings_password is None:
        print(
            f"WARNING: TWITCH_NOWPLAYING_HOST={nowplaying_host!r} is reachable off this machine, "
            f"but TWITCH_SETTINGS_PASSWORD is unset — anyone who finds the port can change your "
            f"queue/cooldown settings via /settings. Set TWITCH_SETTINGS_PASSWORD."
        )

    public_base_url = os.getenv("TWITCH_PUBLIC_BASE_URL", "").strip().rstrip("/") or None
    if public_base_url is not None and not public_base_url.startswith(("http://", "https://")):
        print(
            f"WARNING: TWITCH_PUBLIC_BASE_URL={public_base_url!r} has no http(s):// scheme — "
            f"ignoring it. !commands will use the terse in-chat listing instead of a link."
        )
        public_base_url = None
    elif public_base_url is not None and public_base_url.startswith("http://"):
        print(
            f"WARNING: TWITCH_PUBLIC_BASE_URL={public_base_url!r} uses http://, not https:// — "
            f"this is the link every viewer gets from !commands, so it's worth serving over TLS. "
            f"See the README's \"Serving over HTTPS\" section."
        )

    tls_cert_raw = os.getenv("TWITCH_TLS_CERT_FILE", "").strip()
    tls_key_raw = os.getenv("TWITCH_TLS_KEY_FILE", "").strip()
    tls_cert_file = Path(tls_cert_raw) if tls_cert_raw else None
    tls_key_file = Path(tls_key_raw) if tls_key_raw else None
    if bool(tls_cert_file) != bool(tls_key_file):
        print(
            "WARNING: TWITCH_TLS_CERT_FILE and TWITCH_TLS_KEY_FILE must both be set to enable "
            "native HTTPS — only one was provided, so the server will run plain HTTP. (If you're "
            "terminating TLS with a reverse proxy instead, leave both of these unset — that's the "
            "normal setup and this warning doesn't apply to you.)"
        )
        tls_cert_file = tls_key_file = None
    elif tls_cert_file is not None:
        if not tls_cert_file.is_file():
            print(f"WARNING: TWITCH_TLS_CERT_FILE={tls_cert_file} does not exist — server will run plain HTTP.")
            tls_cert_file = tls_key_file = None
        elif not tls_key_file.is_file():  # type: ignore[union-attr]
            print(f"WARNING: TWITCH_TLS_KEY_FILE={tls_key_file} does not exist — server will run plain HTTP.")
            tls_cert_file = tls_key_file = None

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        owner_id=owner_id,
        prefix=os.getenv("TWITCH_PREFIX", "!").strip() or "!",
        audio_bitrate_kbps=_clamped_int_env("AUDIO_BITRATE_KBPS", 128, 64, 320),
        pause_when_no_listeners=_bool_env("PAUSE_QUEUE_WHEN_NO_LISTENERS", False),
        nowplaying_host=nowplaying_host,
        nowplaying_port=_clamped_int_env("TWITCH_NOWPLAYING_PORT", 8098, 1024, 65535),
        settings_password=settings_password,
        public_base_url=public_base_url,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        token_path=DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip(),
        tunables_path=DATA_DIR / os.getenv("TWITCH_TUNABLES_FILE", "tunables.json").strip(),
        blocklist_path=DATA_DIR / os.getenv("TWITCH_BLOCKLIST_FILE", "blocklist.json").strip(),
        specs_path=DATA_DIR / os.getenv("TWITCH_SPECS_FILE", "specs.json").strip(),
        toggles_path=DATA_DIR / os.getenv("TWITCH_TOGGLES_FILE", "toggles.json").strip(),
        db_path=DATA_DIR / os.getenv("TWITCH_DB_FILE", "community.db").strip(),
        ytdlp_cookies_file=cookies_path,
        ytdlp_js_runtime_path=os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip() or None,
        ytdlp_js_runtime_name=os.getenv("YTDLP_JS_RUNTIME_NAME", "deno").strip() or "deno",
        ytdlp_concurrency=_clamped_int_env("YTDLP_CONCURRENCY", 2, 1, 4),
        ytdlp_extract_timeout_seconds=_clamped_int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45, 10, 120),
        ytdlp_player_client=ytdlp_player_client,
        # Skips the player's second extraction (chat resolves once to queue,
        # then re-resolves right before playing) for anything near the front
        # of the queue. 0 disables caching.
        ytdlp_cache_ttl_seconds=_clamped_int_env("YTDLP_CACHE_TTL_SECONDS", 300, 0, 3600),
        # Points yt-dlp's PO-token plugin at a bgutil-ytdlp-pot-provider
        # instance, if one's set up (see README). None is a no-op.
        ytdlp_pot_provider_url=os.getenv("YTDLP_POT_PROVIDER_URL", "").strip() or None,
        ytdlp_worker_mode=worker_mode,
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )
