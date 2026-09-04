from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from twitch_radio.admin_server import run_admin_server
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.config import Settings, load_settings
from twitch_radio.extraction import Resolver
from twitch_radio.relay import TwitchRadioRelay
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
    # twitchio and yt-dlp are both fairly chatty at INFO; this service's own
    # logging carries the signal that actually matters day to day.
    logging.getLogger("twitchio").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _async_run() -> None:
    settings = load_settings()
    configure_logging(settings)
    log = logging.getLogger(__name__)

    resolver = Resolver(settings)
    tunables_store = JsonStore(settings.tunables_path)

    relay = TwitchRadioRelay(
        ingest_url=settings.ingest_url,
        stream_key=settings.stream_key,
        background_image=settings.background_image,
        resolver=resolver.resolve,
        video_bitrate_kbps=settings.video_bitrate_kbps,
        video_fps=settings.video_fps,
    )
    # relay.start() spawns a persistent background task (and, once running, a
    # live ffmpeg RTMP push) before anything else here exists. Everything
    # from here down is nested try/finally, one per resource — not one big
    # try around just the chat bot — specifically so a failure acquiring any
    # *later* resource (the admin server's port, the chat bot itself) still
    # tears down everything already acquired instead of leaking it.
    relay.start()
    try:
        admin_runner = await run_admin_server(
            relay=relay,
            tunables_store=tunables_store,
            settings_password=settings.settings_password,
            broadcast_info={
                "Ingest URL": settings.ingest_url,
                "Video bitrate": f"{settings.video_bitrate_kbps} kbps",
                "Video framerate": f"{settings.video_fps} fps",
                "Background image": str(settings.background_image),
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
                relay=relay,
                tunables_store=tunables_store,
                token_storage_path=settings.token_path,
            )
            relay.set_track_failure_notifier(bot.announce)

            loop = asyncio.get_running_loop()

            def _handle_sigterm() -> None:
                log.info("SIGTERM received — initiating graceful shutdown.")
                task = asyncio.create_task(bot.close())
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)

            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

            async with bot:
                await bot.start()
        finally:
            await admin_runner.cleanup()
    finally:
        await relay.stop()
        resolver.close()


def run() -> None:
    asyncio.run(_async_run())
