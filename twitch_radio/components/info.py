from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.commands_reference import COMMANDS
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
            await self.bot.safe_reply(ctx, "Specs haven't been set up yet.")
            return
        await self.bot.safe_reply(ctx, " | ".join(lines))

    @commands.command(name="peripherals", aliases=["periphs"])
    async def peripherals_cmd(self, ctx: commands.Context) -> None:
        """Shows the streamer's peripherals — same deal as !specs above."""
        peripherals = Peripherals.from_dict(await self.bot.specs_store.read())
        lines = peripherals.display_lines()
        if not lines:
            await self.bot.safe_reply(ctx, "Peripherals haven't been set up yet.")
            return
        await self.bot.safe_reply(ctx, " | ".join(lines))

    @commands.command(name="commands", aliases=["help"])
    async def commands_list(self, ctx: commands.Context) -> None:
        """Built from commands_reference.COMMANDS rather than hand-typed —
        a command that isn't meant to show up here at all (public=False,
        e.g. !block/!unblock/!blocklist) just needs that one flag set in
        one place, instead of also needing someone to remember to leave it
        out of this string by hand. Full docs for everything, hidden
        commands included, live on /settings."""
        p = self.bot.prefix
        anyone = [c for c in COMMANDS if c.public and c.group == "anyone"]
        mods = [c for c in COMMANDS if c.public and c.group == "moderators"]
        # !sr is the one command worth a visible argument hint here — it's
        # the single most-used command and the one people are most likely
        # to type bare and wonder what goes after it.
        main = " | ".join(
            f"{p}{c.name} <song/URL>" if c.name == "sr" else f"{p}{c.name}" for c in anyone
        )
        mod_list = ", ".join(f"{p}{c.name}" for c in mods)
        await self.bot.safe_reply(
            ctx, f"{main}  |  mods: {mod_list}  |  full reference (incl. a couple of mod-only "
                 f"extras not shown here): /settings"
        )
