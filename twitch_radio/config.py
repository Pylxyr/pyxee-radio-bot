from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from twitch_radio.admin.passwords import validate_stored_password
from twitch_radio.netutil import DEFAULT_TRUSTED_PROXIES, IPNetwork, is_loopback_host, parse_networks

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


# Every extra emote source the chat overlay understands (see emotes.py).
EMOTE_SOURCES = ("7tv", "bttv", "ffz", "cheermotes")


def _emote_sources_env(name: str) -> tuple[str, ...]:
    """Comma-separated subset of the known emote sources; unset means all of
    them, "none" means none. Unknown entries are reported and skipped."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return EMOTE_SOURCES
    if raw in ("none", "off", "false", "0"):
        return ()
    chosen: list[str] = []
    for token in (part.strip() for part in raw.replace(";", ",").split(",")):
        if not token:
            continue
        if token not in EMOTE_SOURCES:
            print(f"WARNING: {name} lists unknown source {token!r} — known sources: {', '.join(EMOTE_SOURCES)}.")
        elif token not in chosen:
            chosen.append(token)
    return tuple(chosen)


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


@dataclass(frozen=True, slots=True)
class Settings:
    # Twitch app credentials — from https://dev.twitch.tv/console/apps
    client_id: str
    client_secret: str
    bot_id: str
    owner_id: str
    prefix: str

    # Local HTTP surface — serves /chat-overlay, /commands, /settings
    nowplaying_host: str
    nowplaying_port: int
    settings_password: str | None
    settings_allow_open: bool
    session_hours: int
    session_remember_days: int
    trusted_proxies: tuple[IPNetwork, ...]
    # Public base URL for the /commands link (no trailing slash); None if unset.
    public_base_url: str | None
    # Which extra emote sources the chat overlay draws as images: any of
    # 7tv, bttv, ffz, cheermotes (Twitch's own emotes always work). Empty
    # tuple = none. TWITCH_CHAT_EMOTE_SOURCES.
    chat_emote_sources: tuple[str, ...]

    # Persistence — all under DATA_DIR so one ReadWritePaths entry in the
    # systemd unit covers everything this process writes.
    token_path: Path
    tunables_path: Path
    specs_path: Path
    toggles_path: Path
    db_path: Path

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

    chat_emote_sources = _emote_sources_env("TWITCH_CHAT_EMOTE_SOURCES")

    nowplaying_host = os.getenv("TWITCH_NOWPLAYING_HOST", "127.0.0.1").strip() or "127.0.0.1"
    settings_password = os.getenv("TWITCH_SETTINGS_PASSWORD", "").strip() or None
    if settings_password is not None:
        password_problem = validate_stored_password(settings_password)
        if password_problem is not None:
            raise RuntimeError(f"TWITCH_SETTINGS_PASSWORD {password_problem}")
    settings_allow_open = _bool_env("TWITCH_SETTINGS_ALLOW_OPEN", False)
    host_is_exposed = not is_loopback_host(nowplaying_host)
    if settings_password is None and settings_allow_open:
        print(
            "WARNING: TWITCH_SETTINGS_PASSWORD is unset with TWITCH_SETTINGS_ALLOW_OPEN on — anyone "
            "who can reach this server (directly, or through a reverse proxy) can change your "
            "settings via /settings. Set TWITCH_SETTINGS_PASSWORD."
        )
    elif settings_password is None and host_is_exposed:
        print(
            f"WARNING: TWITCH_NOWPLAYING_HOST={nowplaying_host!r} is reachable off this machine but "
            f"TWITCH_SETTINGS_PASSWORD is unset — /settings is DISABLED until you set a password. "
            f"(TWITCH_SETTINGS_ALLOW_OPEN=true re-enables it without one; only do that on a network "
            f"you trust.)"
        )

    trusted_proxies, rejected_proxies = parse_networks(
        os.getenv("TWITCH_TRUSTED_PROXIES", "").strip() or DEFAULT_TRUSTED_PROXIES
    )
    if rejected_proxies:
        print(
            f"WARNING: TWITCH_TRUSTED_PROXIES has entries that aren't an IP or CIDR range, ignoring "
            f"them: {', '.join(rejected_proxies)}"
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
            f"See the README's \"Publishing with Caddy\" section."
        )

    if settings_password is None and public_base_url is not None and not settings_allow_open:
        print(
            "WARNING: TWITCH_PUBLIC_BASE_URL is set but TWITCH_SETTINGS_PASSWORD is not. /settings "
            "will refuse everyone arriving through the reverse proxy — set a password to use it "
            "from anywhere but this machine."
        )

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        owner_id=owner_id,
        prefix=os.getenv("TWITCH_PREFIX", "!").strip() or "!",
        nowplaying_host=nowplaying_host,
        nowplaying_port=_clamped_int_env("TWITCH_NOWPLAYING_PORT", 8098, 1024, 65535),
        settings_password=settings_password,
        settings_allow_open=settings_allow_open,
        session_hours=_clamped_int_env("TWITCH_SESSION_HOURS", 12, 1, 168),
        session_remember_days=_clamped_int_env("TWITCH_SESSION_REMEMBER_DAYS", 30, 0, 365),
        trusted_proxies=trusted_proxies,
        public_base_url=public_base_url,
        chat_emote_sources=chat_emote_sources,
        token_path=DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip(),
        tunables_path=DATA_DIR / os.getenv("TWITCH_TUNABLES_FILE", "tunables.json").strip(),
        specs_path=DATA_DIR / os.getenv("TWITCH_SPECS_FILE", "specs.json").strip(),
        toggles_path=DATA_DIR / os.getenv("TWITCH_TOGGLES_FILE", "toggles.json").strip(),
        db_path=DATA_DIR / os.getenv("TWITCH_DB_FILE", "community.db").strip(),
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )
