from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.eightball import answer

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot


class EightBallComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="8ball")
    async def eightball_cmd(self, ctx: commands.Context, *, question: str = "") -> None:
        if not question.strip():
            await self.bot.safe_reply(ctx, "Usage: !8ball <question>")
            return
        await self.bot.safe_reply(ctx, f"\U0001f3b1 {answer()}")
