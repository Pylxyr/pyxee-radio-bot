from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.customcommands import clean_name
from twitch_radio.timers import MAX_MIN_MESSAGES, TimerScheduler, parse_add
from twitch_radio.toggles import FeatureToggles

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

_TICK_SECONDS = 30
_USAGE = "Usage: !timer add <name> <minutes> <message> | remove <name> | on|off <name> | min <name> <messages> | list"


class TimersComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot
        self._scheduler = TimerScheduler()
        self._task: asyncio.Task[None] | None = None

    async def component_load(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="timers")

    async def component_teardown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            try:
                await self.tick()
            except Exception:
                log.debug("Timer tick failed (non-fatal).", exc_info=True)

    async def tick(self) -> int:
        """One scheduler pass; returns how many timers were posted."""
        toggles = FeatureToggles.from_dict(await self.bot.toggles_store.read())
        if not toggles.timers_enabled or not await self.bot.channel_is_live():
            self._scheduler.reset()
            return 0
        due = self._scheduler.due(
            await self.bot.db.list_timers(), now=time.monotonic(), total_messages=self.bot.chat_messages
        )
        for timer in due:
            await self.bot.announce(timer.message)
        return len(due)

    @commands.is_moderator()
    @commands.command(name="timer")
    async def timer_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        sub, _, rest = args.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        db = self.bot.db
        if sub == "add":
            parsed = parse_add(rest)
            if isinstance(parsed, str):
                await self.bot.safe_reply(ctx, parsed)
                return
            name, minutes, message = parsed
            await db.set_timer(name, message, minutes, str(ctx.chatter.id))
            await self.bot.safe_reply(ctx, f"Timer {name} saved: every {minutes} min while live (needs some chat activity).")
        elif sub in ("remove", "delete", "del"):
            target = clean_name(rest)
            removed = target is not None and await db.delete_timer(target)
            await self.bot.safe_reply(ctx, f"Removed timer {target}." if removed else f"No timer named {rest or '?'}.")
        elif sub in ("on", "off"):
            target = clean_name(rest)
            found = target is not None and await db.set_timer_enabled(target, sub == "on")
            await self.bot.safe_reply(ctx, f"Timer {target} is {sub}." if found else f"No timer named {rest or '?'}.")
        elif sub == "min":
            name_text, _, count_text = rest.partition(" ")
            target = clean_name(name_text)
            if target is None or not count_text.strip().isdigit() or int(count_text) > MAX_MIN_MESSAGES:
                await self.bot.safe_reply(ctx, f"Usage: !timer min <name> <0-{MAX_MIN_MESSAGES}>")
                return
            found = await db.set_timer_min_messages(target, int(count_text))
            await self.bot.safe_reply(ctx, f"Timer {target} needs {int(count_text)} chat messages." if found else f"No timer named {target}.")
        elif sub == "list":
            timers = await db.list_timers()
            if not timers:
                await self.bot.safe_reply(ctx, "No timers yet.")
                return
            listing = ", ".join(f"{t.name} ({t.interval_minutes}m{'' if t.enabled else ', off'})" for t in timers)
            await self.bot.safe_reply(ctx, f"Timers: {listing}")
        else:
            await self.bot.safe_reply(ctx, _USAGE)
