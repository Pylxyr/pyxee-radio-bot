from __future__ import annotations

from typing import TYPE_CHECKING

from twitchio.ext import commands

from twitch_radio.chatevent import display_name_of
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

_USAGE = "Usage: !queue [join|leave] — mods: open|close|next [n]|clear  (size cap: !setlimit queue_max_size <n>)"


class QueueComponent(commands.Component):
    """The viewer queue (!queue) — see twitch_radio.queue.ViewerQueue. Its
    size cap is the queue_max_size tunable (shared surface with every other
    runtime knob — !setlimit/`/settings`), not a command of its own."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        q = self.bot.viewer_queue
        chatter_id = str(ctx.chatter.id)

        if action == "join":
            tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
            result = q.join(chatter_id, display_name_of(ctx.chatter), max_size=tunables.queue_max_size)
            messages = {
                "joined": f"You're #{q.position(chatter_id)} in the queue.",
                "already": f"You're already #{q.position(chatter_id)} in the queue.",
                "closed": "The queue isn't open right now.",
                "full": "The queue is full.",
            }
            await self.bot.safe_reply(ctx, messages[result])
            return
        if action == "leave":
            left = q.leave(chatter_id)
            await self.bot.safe_reply(ctx, "You left the queue." if left else "You're not in the queue.")
            return
        if action == "list":
            names = q.names(limit=5)
            more = f" (+{len(q) - 5} more)" if len(q) > 5 else ""
            await self.bot.safe_reply(ctx, f"Queue ({len(q)}): {', '.join(names)}{more}" if names else "The queue is empty.")
            return
        if not action:
            position = q.position(chatter_id)
            state = "open" if q.open else "closed"
            if position is None:
                await self.bot.safe_reply(ctx, f"Queue is {state}, {len(q)} waiting. !queue join to get in line.")
            else:
                await self.bot.safe_reply(ctx, f"You're #{position} of {len(q)}.")
            return

        if not getattr(ctx.chatter, "moderator", False):
            await self.bot.safe_reply(ctx, _USAGE)
            return
        if action == "open":
            q.open = True
            await self.bot.safe_reply(ctx, "Queue is open — !queue join to get in line.")
        elif action == "close":
            q.open = False
            await self.bot.safe_reply(ctx, "Queue is closed to new joins.")
        elif action == "clear":
            q.clear()
            await self.bot.safe_reply(ctx, "Queue cleared.")
        elif action == "next":
            count = int(rest.strip()) if rest.strip().isdigit() else 1
            names = q.pop_next(count)
            await self.bot.safe_reply(ctx, f"Next up: {', '.join(names)}!" if names else "The queue is empty.")
        else:
            await self.bot.safe_reply(ctx, _USAGE)
