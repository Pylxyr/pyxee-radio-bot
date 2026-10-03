from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.quotes import MAX_QUOTE_LENGTH, format_quote, parse_quote_id

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot


class QuotesComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="quote")
    async def quote_cmd(self, ctx: commands.Context, arg: str = "") -> None:
        """!quote shows a random quote; !quote <id> shows a specific one."""
        if arg:
            quote_id = parse_quote_id(arg)
            if quote_id is None:
                await self.bot.safe_reply(ctx, "Usage: !quote [id]")
                return
            quote = await self.bot.db.get_quote(quote_id)
            if quote is None:
                await self.bot.safe_reply(ctx, f"No quote #{quote_id}.")
                return
        else:
            quote = await self.bot.db.random_quote()
            if quote is None:
                await self.bot.safe_reply(ctx, "No quotes yet — !addquote <text> to add one.")
                return
        await self.bot.safe_reply(ctx, format_quote(quote))

    @commands.is_moderator()
    @commands.command(name="addquote")
    async def add_quote_cmd(self, ctx: commands.Context, *, text: str = "") -> None:
        text = text.strip()
        if not text:
            await self.bot.safe_reply(ctx, "Usage: !addquote <text>")
            return
        if len(text) > MAX_QUOTE_LENGTH:
            await self.bot.safe_reply(ctx, f"Keep quotes under {MAX_QUOTE_LENGTH} characters.")
            return
        quote_id = await self.bot.db.add_quote(text, str(ctx.chatter.id))
        await self.bot.safe_reply(ctx, f"Saved as #{quote_id}.")

    @commands.is_moderator()
    @commands.command(name="delquote")
    async def del_quote_cmd(self, ctx: commands.Context, arg: str = "") -> None:
        quote_id = parse_quote_id(arg)
        removed = quote_id is not None and await self.bot.db.delete_quote(quote_id)
        await self.bot.safe_reply(ctx, f"Deleted #{quote_id}." if removed else "Usage: !delquote <id>")
