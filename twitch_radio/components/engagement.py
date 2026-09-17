from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twitchio.ext import commands

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

# Custom commands can't shadow a built-in — checked in add_command_cmd below.
# Kept as a flat, hand-maintained set (not introspected from the other
# components) to avoid a wiring-time import cycle between this module and
# the ones that define these names.
_RESERVED_NAMES = frozenset(
    {
        "sr", "songrequest", "skip", "voteskip", "vs", "remove", "cancel", "unqueue",
        "position", "pos", "queue", "nowplaying", "np", "radio", "specs", "peripherals",
        "commands", "help", "setlimit", "toggle", "block", "unblock", "blocklist", "clearqueue",
        "points", "balance", "leaderboard", "top", "watchtime", "addcom", "editcom", "delcom",
        "quote", "addquote", "delquote",
    }
)


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
        await ctx.reply(f"{ctx.chatter.display_name}: {points} points, {watch} watched.")

    @commands.command(name="watchtime")
    async def watchtime_cmd(self, ctx: commands.Context) -> None:
        stats = await self.bot.db.get_stats(str(ctx.chatter.id))
        watch = _format_duration(stats["watch_seconds"] if stats else 0)
        await ctx.reply(f"{ctx.chatter.display_name} has {watch} of chat activity tracked.")

    @commands.command(name="leaderboard", aliases=["top"])
    async def leaderboard_cmd(self, ctx: commands.Context) -> None:
        top = await self.bot.db.top_points(limit=5)
        if not top:
            await ctx.reply("No points earned yet.")
            return
        ranked = ", ".join(f"{i}. {name} ({pts})" for i, (name, pts) in enumerate(top, start=1))
        await ctx.reply(f"Top points: {ranked}")

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
            await ctx.reply("Usage: !addcom <name> <response text>")
            return
        name, response = parts[0].strip().lstrip("!").lower(), parts[1].strip()
        if name in _RESERVED_NAMES:
            await ctx.reply(f"!{name} is a built-in command — pick a different name.")
            return
        await self.bot.db.set_command(name, response, str(ctx.chatter.id))
        log.info("Custom command !%s set by %s (%s)", name, ctx.chatter.display_name, ctx.chatter.id)
        await ctx.reply(f"Saved !{name}.")

    @commands.is_moderator()
    @commands.command(name="delcom")
    async def del_command_cmd(self, ctx: commands.Context, *, name: str) -> None:
        name = name.strip().lstrip("!").lower()
        removed = await self.bot.db.delete_command(name)
        await ctx.reply(f"Removed !{name}." if removed else f"No custom command !{name}.")

    # -- quotes -------------------------------------------------------------

    @commands.command(name="quote")
    async def quote_cmd(self, ctx: commands.Context, *, arg: str = "") -> None:
        """!quote — random saved quote. !quote <id> — a specific one."""
        arg = arg.strip()
        quote_id: int | None = None
        if arg:
            try:
                quote_id = int(arg)
            except ValueError:
                await ctx.reply("Usage: !quote [id]")
                return
        row = await self.bot.db.get_quote(quote_id)
        if row is None:
            await ctx.reply("No quotes saved yet." if quote_id is None else f"No quote #{arg}.")
            return
        qid, text = row
        await ctx.reply(f"#{qid}: {text}")

    @commands.is_moderator()
    @commands.command(name="addquote")
    async def add_quote_cmd(self, ctx: commands.Context, *, text: str) -> None:
        text = text.strip()
        if not text:
            await ctx.reply("Usage: !addquote <text>")
            return
        quote_id = await self.bot.db.add_quote(text, str(ctx.chatter.id))
        await ctx.reply(f"Saved as #{quote_id}.")

    @commands.is_moderator()
    @commands.command(name="delquote")
    async def del_quote_cmd(self, ctx: commands.Context, *, arg: str) -> None:
        try:
            quote_id = int(arg.strip())
        except ValueError:
            await ctx.reply("Usage: !delquote <id>")
            return
        removed = await self.bot.db.delete_quote(quote_id)
        await ctx.reply(f"Removed #{quote_id}." if removed else f"No quote #{quote_id}.")
