from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.customcommands import MAX_RESPONSE_LENGTH, ROLES, clean_name, normalize_role
from twitch_radio.tunables import TUNABLE_BOUNDS

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

_MAX_COOLDOWN = TUNABLE_BOUNDS["custom_command_cooldown_seconds"][1]


class CustomCommandsComponent(commands.Component):
    """Mod-managed chat commands. Running them is handled by the bot's
    CommandNotFound hook (see chatbot.py) via `CustomCommandStore.execute`."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.is_moderator()
    @commands.command(name="addcom", aliases=["editcom"])
    async def add_command_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2:
            await self.bot.safe_reply(ctx, "Usage: !addcom <name> <response>  (variables: {user} {touser} {args} {count} {channel})")
            return
        name, response = clean_name(parts[0]), parts[1].strip()
        if name is None:
            await self.bot.safe_reply(ctx, "Command names can use letters, numbers and _ (max 25 characters).")
            return
        if name in self.bot.reserved_names or self.bot.counters.get(name) is not None:
            await self.bot.safe_reply(ctx, f"!{name} is already a command — pick a different name.")
            return
        if len(response) > MAX_RESPONSE_LENGTH:
            await self.bot.safe_reply(ctx, f"Keep responses under {MAX_RESPONSE_LENGTH} characters.")
            return
        await self.bot.custom.save(name, response, str(ctx.chatter.id))
        log.info("Custom command !%s set by %s (%s)", name, ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"Saved !{name}.")

    @commands.is_moderator()
    @commands.command(name="delcom")
    async def del_command_cmd(self, ctx: commands.Context, *, name: str = "") -> None:
        cleaned = clean_name(name)
        removed = cleaned is not None and await self.bot.custom.delete(cleaned)
        await self.bot.safe_reply(ctx, f"Removed !{cleaned}." if removed else f"No custom command !{cleaned or name.strip()}.")

    @commands.is_moderator()
    @commands.command(name="comopt")
    async def command_options_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        """!comopt <name> cd <seconds|default>  |  !comopt <name> role <everyone|sub|vip|mod>"""
        usage = "Usage: !comopt <name> cd <seconds|default>  or  !comopt <name> role <everyone|sub|vip|mod>"
        parts = args.split()
        name = clean_name(parts[0]) if parts else None
        if name is None or len(parts) != 3:
            await self.bot.safe_reply(ctx, usage)
            return
        option, value = parts[1].lower(), parts[2].lower()
        if option in ("cd", "cooldown"):
            if value == "default":
                seconds = -1
            elif value.isdigit() and int(value) <= _MAX_COOLDOWN:
                seconds = int(value)
            else:
                await self.bot.safe_reply(ctx, f"Cooldown must be 0-{_MAX_COOLDOWN} seconds, or 'default'.")
                return
            found = await self.bot.custom.set_option(name, cooldown_seconds=seconds)
            label = "the default cooldown" if seconds < 0 else f"a {seconds}s cooldown"
        elif option == "role":
            role = normalize_role(value)
            if role is None:
                await self.bot.safe_reply(ctx, f"Role must be one of: {', '.join(ROLES)}.")
                return
            found = await self.bot.custom.set_option(name, min_role=role)
            label = f"role {role}"
        else:
            await self.bot.safe_reply(ctx, usage)
            return
        await self.bot.safe_reply(ctx, f"!{name} now uses {label}." if found else f"No custom command !{name}.")
