from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from twitch_radio.admin_server import run_admin_server
from twitch_radio.player import RadioPlayer
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.config import Settings, load_settings
from twitch_radio.extraction import Resolver
from twitch_radio.store import JsonStore
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


async def _async_run() -> None:
    settings = load_settings()
    configure_logging(settings)
    log = logging.getLogger(__name__)

    resolver = Resolver(settings)
    tunables_store = JsonStore(settings.tunables_path)

    player = RadioPlayer(resolver=resolver.resolve, audio_bitrate_kbps=settings.audio_bitrate_kbps)
    # player.start() spawns a persistent background task before anything
    # else here exists — nested try/finally per resource, not one big try
    # around just the chat bot, so a failure acquiring a *later* resource
    # still tears down everything already acquired.
    player.start()
    try:
        admin_runner = await run_admin_server(
            player=player,
            tunables_store=tunables_store,
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
                token_storage_path=settings.token_path,
            )
            player.set_track_failure_notifier(bot.announce)

            async def _duration_limit() -> int:
                tunables = TwitchTunables.from_dict(await tunables_store.read())
                return tunables.max_request_duration_seconds

            player.set_duration_limit_getter(_duration_limit)

            loop = asyncio.get_running_loop()

            def _handle_shutdown_signal(signum: int) -> None:
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
        await player.stop()
        resolver.close()


def run() -> None:
    asyncio.run(_async_run())
