from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.chatevent import display_name_of
from twitch_radio.economy import format_duration

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot


class EconomyComponent(commands.Component):
    """Thin chat layer over `Economy` — every rule lives in twitch_radio/economy.py."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="points", aliases=["balance"])
    async def points_cmd(self, ctx: commands.Context, target: str = "") -> None:
        target = target.strip().lstrip("@")
        if target:
            found = await self.bot.db.find_by_name(target)
            if found is None:
                await self.bot.safe_reply(ctx, f"I haven't seen {target} in chat yet.")
                return
            await self.bot.safe_reply(ctx, await self.bot.economy.points_line(found[0], found[1]))
            return
        await self.bot.safe_reply(ctx, await self.bot.economy.points_line(str(ctx.chatter.id), display_name_of(ctx.chatter)))

    @commands.command(name="watchtime")
    async def watchtime_cmd(self, ctx: commands.Context) -> None:
        stats = await self.bot.db.get_stats(str(ctx.chatter.id))
        watched = format_duration(stats["watch_seconds"] if stats else 0)
        await self.bot.safe_reply(ctx, f"{display_name_of(ctx.chatter)} has {watched} of chat activity tracked.")

    @commands.command(name="rank")
    async def rank_cmd(self, ctx: commands.Context) -> None:
        await self.bot.safe_reply(ctx, await self.bot.economy.rank_line(str(ctx.chatter.id), display_name_of(ctx.chatter)))

    @commands.command(name="leaderboard", aliases=["top"])
    async def leaderboard_cmd(self, ctx: commands.Context, kind: str = "") -> None:
        by_watch = kind.strip().lower() in ("watch", "watchtime", "time")
        if by_watch:
            rows = [(n, format_duration(v)) for n, v in await self.bot.db.top_watch_seconds(limit=5)]
            label = "Most chat time"
        else:
            rows = [(n, str(v)) for n, v in await self.bot.db.top_points(limit=5)]
            label = "Top points"
        if not rows:
            await self.bot.safe_reply(ctx, "Nobody's on the board yet.")
            return
        ranked = ", ".join(f"{i}. {name} ({value})" for i, (name, value) in enumerate(rows, start=1))
        await self.bot.safe_reply(ctx, f"{label}: {ranked}")

    @commands.command(name="daily")
    async def daily_cmd(self, ctx: commands.Context) -> None:
        await self.bot.safe_reply(ctx, await self.bot.economy.daily(str(ctx.chatter.id), display_name_of(ctx.chatter)))

    @commands.command(name="give", aliases=["pay"])
    async def give_cmd(self, ctx: commands.Context, target: str = "", amount: str = "") -> None:
        reply = await self.bot.economy.give(str(ctx.chatter.id), display_name_of(ctx.chatter), target, amount)
        await self.bot.safe_reply(ctx, reply)

    @commands.command(name="gamble", aliases=["bet"])
    async def gamble_cmd(self, ctx: commands.Context, bet: str = "") -> None:
        await self.bot.safe_reply(ctx, await self.bot.economy.gamble(str(ctx.chatter.id), display_name_of(ctx.chatter), bet))
