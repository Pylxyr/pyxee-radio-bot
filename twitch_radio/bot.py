from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from twitch_radio.admin.app import run_admin_server
from twitch_radio.admin.passwords import is_password_hash
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.chatfeed import ChatFeed
from twitch_radio.config import Settings, load_settings
from twitch_radio.db import Database
from twitch_radio.maintenance import backup_loop
from twitch_radio.runtime import RuntimeStatus
from twitch_radio.store import JsonStore

_bg_tasks: set[asyncio.Task[object]] = set()


def configure_logging(settings: Settings) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_to_file:
        handlers.append(logging.FileHandler(settings.log_dir / "twitch-radio.log", encoding="utf-8"))
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(settings.log_level)
    logging.getLogger("twitchio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _async_run(settings: Settings) -> None:
    configure_logging(settings)
    log = logging.getLogger(__name__)

    tunables_store = JsonStore(settings.tunables_path)
    specs_store = JsonStore(settings.specs_path)
    toggles_store = JsonStore(settings.toggles_path)
    db = Database(settings.db_path)
    await db.connect()
    # Shared between the chat bot (appends every non-bot message — see
    # chatbot.py's _process_chat) and the admin server (/chat-overlay,
    # /chat.json, /ws/chat).
    chat_feed = ChatFeed()
    status = RuntimeStatus()

    # Nested try/finally per resource (not one big try around just the chat
    # bot) so a failure acquiring a *later* resource still tears down
    # everything already acquired.
    try:
        admin_runner = await run_admin_server(
            chat_feed=chat_feed,
            status=status,
            tunables_store=tunables_store,
            specs_store=specs_store,
            toggles_store=toggles_store,
            db=db,
            settings_password=settings.settings_password,
            broadcast_info={
                "Chat overlay": "/chat-overlay",
                "Chat command prefix": settings.prefix,
            },
            host=settings.http_host,
            port=settings.http_port,
            trusted_proxies=settings.trusted_proxies,
            session_hours=settings.session_hours,
            session_remember_days=settings.session_remember_days,
            allow_open_settings=settings.settings_allow_open,
        )
        try:
            bot = TwitchChatBot(
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                bot_id=settings.bot_id,
                owner_id=settings.owner_id,
                prefix=settings.prefix,
                chat_feed=chat_feed,
                tunables_store=tunables_store,
                specs_store=specs_store,
                toggles_store=toggles_store,
                db=db,
                token_storage_path=settings.token_path,
                public_base_url=settings.public_base_url,
                emote_sources=settings.chat_emote_sources,
                status=status,
                reply_suffixes=settings.reply_suffixes,
                reserved_commands=settings.reserved_commands,
            )
            if settings.db_backup_keep > 0:
                backup_task = asyncio.create_task(
                    backup_loop(db, settings.backup_dir, settings.db_backup_keep), name="db-backup"
                )
                _bg_tasks.add(backup_task)
                backup_task.add_done_callback(_bg_tasks.discard)

            loop = asyncio.get_running_loop()

            shutting_down = False

            def _handle_shutdown_signal(signum: int) -> None:
                # systemd sends SIGTERM and an impatient operator adds
                # Ctrl-C — without this guard, both would spawn their own
                # bot.close() and race over the same teardown.
                nonlocal shutting_down
                if shutting_down:
                    log.info("%s received — shutdown already in progress.", signal.Signals(signum).name)
                    return
                shutting_down = True
                log.info("%s received — initiating graceful shutdown.", signal.Signals(signum).name)
                task = asyncio.create_task(bot.close())
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)

            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, _handle_shutdown_signal, sig)

            async with bot:
                await bot.start()
        finally:
            await admin_runner.cleanup()
    finally:
        # Reverse of setup order: the HTTP surface is already down (inner
        # finally above), so nothing can still be serving a request against
        # the database — see TwitchChatBot.close()'s docstring for why it
        # doesn't close the database itself.
        await db.close()


def _load_settings_or_exit() -> Settings:
    import sys

    try:
        return load_settings()
    except RuntimeError as exc:
        sys.exit(f"Config check FAILED: {exc}")


def run() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="twitch-radio-bot")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate .env and exit — doesn't start the bot or touch Twitch.",
    )
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="Prompt for a /settings password and print the hash to put in TWITCH_SETTINGS_PASSWORD.",
    )
    args = parser.parse_args()

    if args.check_config:
        sys.exit(_check_config())
    if args.hash_password:
        sys.exit(_hash_password())
    asyncio.run(_async_run(_load_settings_or_exit()))


def _hash_password() -> int:
    import getpass

    from twitch_radio.admin.passwords import MAX_PASSWORD_LENGTH, hash_password

    password = getpass.getpass("New /settings password: ")
    if not password:
        print("Nothing entered — aborting.")
        return 1
    if len(password) > MAX_PASSWORD_LENGTH:
        print(f"Too long — the login form accepts at most {MAX_PASSWORD_LENGTH} characters.")
        return 1
    if getpass.getpass("Repeat it: ") != password:
        print("Those didn't match — aborting.")
        return 1
    print("\nPut this line in .env (replacing any existing TWITCH_SETTINGS_PASSWORD), then restart:\n")
    print(f"TWITCH_SETTINGS_PASSWORD={hash_password(password)}")
    return 0


def _check_config() -> int:
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"Config check FAILED: {exc}")
        return 1

    # Deliberately never prints client_secret or settings_password.
    print("Config OK:")
    print(f"  Twitch: bot_id={settings.bot_id} owner_id={settings.owner_id} prefix={settings.prefix!r}")
    if settings.settings_password is None:
        login = "no password — /settings is reachable only from this machine, never through a proxy"
    else:
        login = f"password {'hashed' if is_password_hash(settings.settings_password) else 'set (plain text)'}"
    print(f"  HTTP: http://{settings.http_host}:{settings.http_port} ({login})")
    print(
        f"  Sessions: {settings.session_hours}h"
        + (f", or {settings.session_remember_days}d with 'keep me signed in'" if settings.session_remember_days else "")
    )
    print(f"  Trusted proxies: {', '.join(str(net) for net in settings.trusted_proxies) or 'none'}")
    if settings.public_base_url:
        print(f"  Public commands page: {settings.public_base_url}/commands (linked from !commands in chat)")
    else:
        print("  Public commands page: not configured (TWITCH_PUBLIC_BASE_URL unset) — !commands uses the in-chat listing")
    token_status = "found" if settings.token_path.exists() else "missing — run OAuth setup before starting"
    print(f"  Token file: {settings.token_path} ({token_status})")
    print(f"  Community DB: {settings.db_path}")
    return 0
