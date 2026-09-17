from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from twitch_radio.admin_server import run_admin_server
from twitch_radio.player import RadioPlayer
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.config import Settings, load_settings
from twitch_radio.db import Database
from twitch_radio.extraction import Resolver
from twitch_radio.radio import RadioSuggester
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

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
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _async_run(settings: Settings) -> None:
    configure_logging(settings)
    log = logging.getLogger(__name__)

    resolver = Resolver(settings)
    tunables_store = JsonStore(settings.tunables_path)
    blocklist_store = JsonStore(settings.blocklist_path)
    specs_store = JsonStore(settings.specs_path)
    toggles_store = JsonStore(settings.toggles_path)
    db = Database(settings.db_path)
    await db.connect()

    # Fire-and-forget: warms up yt-dlp's worker threads (and, once cached,
    # persists across restarts too) before the first real !sr arrives. Never
    # awaited inline — must not delay the rest of startup — and warm_up()
    # itself is non-fatal on failure, so this is pure upside.
    warmup_task = asyncio.create_task(resolver.warm_up(), name="resolver-warmup")
    _bg_tasks.add(warmup_task)
    warmup_task.add_done_callback(_bg_tasks.discard)

    player = RadioPlayer(
        resolver=resolver.resolve,
        audio_bitrate_kbps=settings.audio_bitrate_kbps,
        pause_when_no_listeners=settings.pause_when_no_listeners,
        prefetch_enabled=settings.ytdlp_cache_ttl_seconds > 0,
    )
    radio_suggester = RadioSuggester(resolver, blocklist_store)
    player.set_radio_suggester(radio_suggester.suggest)

    async def _radio_enabled() -> bool:
        toggles = FeatureToggles.from_dict(await toggles_store.read())
        return toggles.radio_autoplay_enabled

    player.set_radio_enabled_getter(_radio_enabled)
    # Nested try/finally per resource (not one big try around just the chat
    # bot) so a failure acquiring a *later* resource still tears down
    # everything already acquired.
    player.start()
    try:
        admin_runner = await run_admin_server(
            player=player,
            tunables_store=tunables_store,
            blocklist_store=blocklist_store,
            specs_store=specs_store,
            toggles_store=toggles_store,
            db=db,
            settings_password=settings.settings_password,
            broadcast_info={
                "Audio stream": "/stream.mp3",
                "Overlay": "/overlay",
                "Audio bitrate": f"{settings.audio_bitrate_kbps} kbps",
                "Chat command prefix": settings.prefix,
            },
            host=settings.nowplaying_host,
            port=settings.nowplaying_port,
        )
        try:
            bot = TwitchChatBot(
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                bot_id=settings.bot_id,
                owner_id=settings.owner_id,
                prefix=settings.prefix,
                resolver=resolver,
                player=player,
                tunables_store=tunables_store,
                blocklist_store=blocklist_store,
                specs_store=specs_store,
                toggles_store=toggles_store,
                db=db,
                token_storage_path=settings.token_path,
            )
            player.set_track_failure_notifier(bot.announce)

            async def _duration_limit() -> int:
                tunables = TwitchTunables.from_dict(await tunables_store.read())
                return tunables.max_request_duration_seconds

            player.set_duration_limit_getter(_duration_limit)

            loop = asyncio.get_running_loop()

            shutting_down = False

            def _handle_shutdown_signal(signum: int) -> None:
                # systemd sends SIGTERM and an impatient operator adds
                # Ctrl-C; without this guard each one spawned its own
                # bot.close() task, and two concurrent closes race over the
                # same websocket/token teardown.
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
        # Order matters and is the reverse of setup: the HTTP surface is
        # already down (inner finally above), so nothing can still be
        # serving a request against the player or the database by now.
        # TwitchChatBot.close() deliberately no longer closes the database
        # for exactly this reason — see its docstring.
        if not warmup_task.done():
            warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warmup_task
        await player.stop()
        resolver.close()
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
        help="Validate .env and exit — doesn't start the bot, spawn ffmpeg, or touch Twitch/yt-dlp.",
    )
    args = parser.parse_args()

    if args.check_config:
        sys.exit(_check_config())
    asyncio.run(_async_run(_load_settings_or_exit()))


def _check_config() -> int:
    import shutil

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"Config check FAILED: {exc}")
        return 1

    if shutil.which("ffmpeg") is None:
        print("Config check FAILED: ffmpeg not found on PATH.")
        return 1

    # Deliberately never prints client_secret or settings_password.
    print("Config OK:")
    print(f"  Twitch: bot_id={settings.bot_id} owner_id={settings.owner_id} prefix={settings.prefix!r}")
    print(f"  Audio: {settings.audio_bitrate_kbps} kbps, pause_when_no_listeners={settings.pause_when_no_listeners}")
    print(
        f"  HTTP: http://{settings.nowplaying_host}:{settings.nowplaying_port} "
        f"(settings password {'set' if settings.settings_password else 'NOT set — /settings is open to anyone'})"
    )
    token_status = "found" if settings.token_path.exists() else "missing — run OAuth setup before starting"
    print(f"  Token file: {settings.token_path} ({token_status})")
    print(f"  Community DB: {settings.db_path}")
    print(
        f"  yt-dlp: concurrency={settings.ytdlp_concurrency} "
        f"timeout={settings.ytdlp_extract_timeout_seconds}s "
        f"cookies={'configured' if settings.ytdlp_cookies_file else 'none'}"
    )
    return 0
