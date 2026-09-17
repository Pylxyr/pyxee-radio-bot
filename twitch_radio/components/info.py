from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.specs import PCSpecs, Peripherals

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot


class InfoComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="specs")
    async def specs_cmd(self, ctx: commands.Context) -> None:
        """Shows the streamer's PC specs — set from the /settings page,
        not from chat (there's nothing to type here beyond !specs itself)."""
        specs = PCSpecs.from_dict(await self.bot.specs_store.read())
        lines = specs.display_lines()
        if not lines:
            await ctx.reply("Specs haven't been set up yet.")
            return
        await ctx.reply(" | ".join(lines))

    @commands.command(name="peripherals", aliases=["periphs"])
    async def peripherals_cmd(self, ctx: commands.Context) -> None:
        """Shows the streamer's peripherals — same deal as !specs above."""
        peripherals = Peripherals.from_dict(await self.bot.specs_store.read())
        lines = peripherals.display_lines()
        if not lines:
            await ctx.reply("Peripherals haven't been set up yet.")
            return
        await ctx.reply(" | ".join(lines))

    @commands.command(name="commands", aliases=["help"])
    async def commands_list(self, ctx: commands.Context) -> None:
        p = self.bot.prefix
        await ctx.reply(
            f"{p}sr <song/URL>  |  {p}skip/{p}voteskip  |  {p}remove  |  {p}position  |  {p}queue  |  "
            f"{p}nowplaying  |  {p}radio  |  {p}points  |  {p}leaderboard  |  {p}watchtime  |  {p}quote  |  "
            f"{p}specs  |  {p}peripherals  |  mods: {p}setlimit, {p}toggle, {p}block/{p}unblock, "
            f"{p}blocklist, {p}clearqueue, {p}addcom/{p}editcom/{p}delcom, {p}addquote/{p}delquote"
        )
