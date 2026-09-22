from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.commands_reference import BY_NAME

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

# Custom commands can't shadow a built-in — checked in add_command_cmd below.
# Sourced from commands_reference.BY_NAME (every command name + alias) rather
# than a hand-maintained list, so a new built-in command is reserved
# automatically instead of silently staying spoofable until this set is
# remembered and updated too.
_RESERVED_NAMES = frozenset(BY_NAME)


def _format_duration(seconds: int) -> str:
    hours, rem = divmod(max(0, seconds), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class EngagementComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    # -- points / watch-time --------------------------------------------

    @commands.command(name="points", aliases=["balance"])
    async def points_cmd(self, ctx: commands.Context) -> None:
        """Passive points/watch-time, earned for chatting while the stream's
        live (see chatbot.py's award loop) — not (yet) spendable on
        anything; that's a follow-up once there's a points economy to
        spend them in."""
        stats = await self.bot.db.get_stats(str(ctx.chatter.id))
        points = stats["points"] if stats else 0
        watch = _format_duration(stats["watch_seconds"] if stats else 0)
        await self.bot.safe_reply(ctx, f"{ctx.chatter.display_name}: {points} points, {watch} watched.")

    @commands.command(name="watchtime")
    async def watchtime_cmd(self, ctx: commands.Context) -> None:
        stats = await self.bot.db.get_stats(str(ctx.chatter.id))
        watch = _format_duration(stats["watch_seconds"] if stats else 0)
        await self.bot.safe_reply(ctx, f"{ctx.chatter.display_name} has {watch} of chat activity tracked.")

    @commands.command(name="leaderboard", aliases=["top"])
    async def leaderboard_cmd(self, ctx: commands.Context) -> None:
        top = await self.bot.db.top_points(limit=5)
        if not top:
            await self.bot.safe_reply(ctx, "No points earned yet.")
            return
        ranked = ", ".join(f"{i}. {name} ({pts})" for i, (name, pts) in enumerate(top, start=1))
        await self.bot.safe_reply(ctx, f"Top points: {ranked}")

    # -- custom commands --------------------------------------------------

    @commands.is_moderator()
    @commands.command(name="addcom", aliases=["editcom"])
    async def add_command_cmd(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: !addcom <name> <response text>. Also used to edit one —
        it's an upsert, so re-running it with the same name just replaces
        the response. {user} in the response is replaced with the calling
        chatter's display name."""
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2:
            await self.bot.safe_reply(ctx, "Usage: !addcom <name> <response text>")
            return
        name, response = parts[0].strip().lstrip("!").lower(), parts[1].strip()
        if name in _RESERVED_NAMES:
            await self.bot.safe_reply(ctx, f"!{name} is a built-in command — pick a different name.")
            return
        await self.bot.db.set_command(name, response, str(ctx.chatter.id))
        log.info("Custom command !%s set by %s (%s)", name, ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"Saved !{name}.")

    @commands.is_moderator()
    @commands.command(name="delcom")
    async def del_command_cmd(self, ctx: commands.Context, *, name: str) -> None:
        name = name.strip().lstrip("!").lower()
        removed = await self.bot.db.delete_command(name)
        await self.bot.safe_reply(ctx, f"Removed !{name}." if removed else f"No custom command !{name}.")
