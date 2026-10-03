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
        """Links to the public /commands page when TWITCH_PUBLIC_BASE_URL is
        set — a categorized, searchable reference any chatter can open, not
        just mods with the /settings password. Otherwise falls back to a
        terse in-chat listing built from the same commands_reference.COMMANDS,
        so a command that shouldn't show here (public=False) just needs
        that one flag set in one place."""
        if self.bot.public_commands_url:
            await self.bot.safe_reply(
                ctx, f"Full list of commands, what they do, and how to use them: {self.bot.public_commands_url}"
            )
            return
        p = self.bot.prefix
        anyone = [c for c in COMMANDS if c.public and c.group == "anyone"]
        mods = [c for c in COMMANDS if c.public and c.group == "moderators"]
        parts = [" | ".join(f"{p}{c.name}" for c in anyone), "mods: " + ", ".join(f"{p}{c.name}" for c in mods)]
        custom = self.bot.custom.all()
        if custom:
            parts.append("custom: " + ", ".join(f"{p}{c.name}" for c in custom))
        counters = self.bot.counters.all()
        if counters:
            parts.append("counters: " + ", ".join(f"{p}{c.name}" for c in counters))
        await self.bot.safe_reply(ctx, "  |  ".join(parts))
