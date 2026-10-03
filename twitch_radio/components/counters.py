from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.counters import MAX_COUNTERS_LISTED
from twitch_radio.customcommands import clean_name

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

_USAGE = (
    "Usage: !counter add|del|set|public <name> [value] | list — "
    "view/adjust any counter with !<name> / !<name>++ / !<name>--"
)


class CountersComponent(commands.Component):
    """Management for named counters (!deaths, !wins, ...) — reading and
    incrementing them (!<name>, !<name>++/--) goes through the same
    CommandNotFound fallback path as custom commands; see chatbot.py's
    _try_fallback_command."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.is_moderator()
    @commands.command(name="counter")
    async def counter_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        parts = args.strip().split(maxsplit=2)
        if parts and parts[0].lower() == "list":
            names = [c.name for c in self.bot.counters.all()[:MAX_COUNTERS_LISTED]]
            total = len(self.bot.counters.all())
            more = f" (+{total - len(names)} more)" if total > len(names) else ""
            await self.bot.safe_reply(ctx, f"Counters: {', '.join(names)}{more}" if names else "No counters yet.")
            return
        if len(parts) < 2:
            await self.bot.safe_reply(ctx, _USAGE)
            return
        action, name = parts[0].lower(), clean_name(parts[1])
        value_text = parts[2] if len(parts) > 2 else ""
        if name is None:
            await self.bot.safe_reply(ctx, "Counter names can use letters, numbers and _ (max 25 characters).")
            return
        counters = self.bot.counters
        if action == "add":
            if name in self.bot.reserved_names or self.bot.custom.get(name) is not None:
                await self.bot.safe_reply(ctx, f"!{name} is already a command — pick a different name.")
                return
            created = await counters.create(name, str(ctx.chatter.id))
            await self.bot.safe_reply(ctx, f"Counter !{name} created, starting at 0." if created else f"!{name} already exists.")
        elif action in ("del", "delete", "remove"):
            removed = await counters.delete(name)
            await self.bot.safe_reply(ctx, f"Removed counter !{name}." if removed else f"No counter named {name}.")
        elif action == "set":
            if not value_text.lstrip("-").isdigit():
                await self.bot.safe_reply(ctx, "Usage: !counter set <name> <value>")
                return
            found = await counters.set_value(name, int(value_text))
            await self.bot.safe_reply(ctx, f"!{name} set to {value_text}." if found else f"No counter named {name}.")
        elif action == "public":
            on = value_text.strip().lower() != "off"
            found = await counters.set_public(name, on)
            state = "anyone" if on else "moderators only"
            await self.bot.safe_reply(ctx, f"!{name} can now be incremented by {state}." if found else f"No counter named {name}.")
        else:
            await self.bot.safe_reply(ctx, _USAGE)
