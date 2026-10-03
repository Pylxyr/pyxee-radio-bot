from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar

from twitchio.exceptions import HTTPException
from twitchio.ext import commands

from twitch_radio.predictions import CreatedPrediction, PredictionManager, parse_start_args

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from twitchio import ChannelPredictionEnd, PartialUser

    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

_T = TypeVar("_T")


def build_manager(broadcaster: PartialUser) -> PredictionManager:
    async def create(title: str, outcomes: list[str], seconds: int) -> CreatedPrediction:
        prediction = await broadcaster.create_prediction(title=title, outcomes=outcomes, prediction_window=seconds)
        return CreatedPrediction(id=prediction.id, outcome_ids=[o.id for o in prediction.outcomes])

    async def end(prediction_id: str, status: str, winning_outcome_id: str | None) -> None:
        await broadcaster.end_prediction(id=prediction_id, status=status, winning_outcome_id=winning_outcome_id)  # type: ignore[arg-type]

    return PredictionManager(create, end)


class PredictionsComponent(commands.Component):
    """!predict — needs channel:manage:predictions on the BROADCASTER's
    token, same pattern as !poll in components/stream_info.py."""

    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.Component.listener()
    async def event_prediction_end(self, payload: ChannelPredictionEnd) -> None:
        """Only acts when the prediction resolved some other way than our
        own !predict command (the streamer used the Twitch dashboard
        directly) — see PredictionManager.consume_external_resolution."""
        manager = self.bot.predictions
        if payload.status == "canceled":
            if manager.consume_external_cancel():
                await self.bot.announce("Prediction cancelled.")
            return
        if payload.winning_outcome is None:
            return
        title = manager.consume_external_resolution(payload.winning_outcome.id)
        if title is not None:
            await self.bot.announce(f"Prediction resolved: {title} wins!")

    @commands.is_moderator()
    @commands.command(name="predict")
    async def predict_cmd(self, ctx: commands.Context, *, args: str = "") -> None:
        sub, _, rest = args.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        manager = self.bot.predictions

        if sub == "start":
            parsed = parse_start_args(rest)
            if isinstance(parsed, str):
                await self.bot.safe_reply(ctx, parsed)
                return
            seconds, title, outcomes = parsed
            try:
                await manager.start(title, outcomes, seconds)
            except HTTPException as e:
                msg = "Predictions aren't set up for this channel yet." if e.status in (401, 403) else (
                    "Couldn't start that prediction — is one already running?"
                )
                await self.bot.safe_reply(ctx, msg)
                log.debug("!predict start failed: %s", e, exc_info=True)
                return
            except Exception:
                log.debug("!predict start failed (non-fatal).", exc_info=True)
                await self.bot.safe_reply(ctx, "Couldn't start that prediction right now.")
                return
            numbered = ", ".join(f"{i}) {o}" for i, o in enumerate(outcomes, start=1))
            await self.bot.safe_reply(ctx, f"Prediction started: {title} — {numbered} ({seconds}s)")
        elif sub == "lock":
            if manager.current is None:
                await self.bot.safe_reply(ctx, "No prediction running.")
                return
            if await self._call(ctx, manager.lock()):
                await self.bot.safe_reply(ctx, "Prediction locked.")
        elif sub == "resolve":
            if manager.current is None:
                await self.bot.safe_reply(ctx, "No prediction running.")
                return
            if not rest.isdigit() or not 1 <= int(rest) <= len(manager.current.outcome_titles):
                await self.bot.safe_reply(ctx, "Usage: !predict resolve <outcome number>")
                return
            winning_title = await self._call(ctx, manager.resolve(int(rest)))
            if winning_title:
                await self.bot.safe_reply(ctx, f"Prediction resolved: {winning_title} wins!")
        elif sub == "cancel":
            if manager.current is None:
                await self.bot.safe_reply(ctx, "No prediction running.")
                return
            if await self._call(ctx, manager.cancel()):
                await self.bot.safe_reply(ctx, "Prediction cancelled — points refunded by Twitch.")
        else:
            await self.bot.safe_reply(
                ctx, "Usage: !predict start <seconds> <title> ; <outcome> ; <outcome> [...] | lock | resolve <n> | cancel"
            )

    async def _call(self, ctx: commands.Context, coro: Coroutine[object, object, _T]) -> _T | None:
        """Runs a PredictionManager call that talks to Twitch, turning a
        failure into a chat reply instead of a crash. `manager.current is
        None` is checked by the caller beforehand — every remaining failure
        here is a real Twitch API problem, most commonly a missing scope."""
        try:
            return await coro
        except HTTPException as e:
            await self.bot.safe_reply(ctx, "Predictions aren't set up for this channel yet.")
            log.debug("!predict failed: %s", e, exc_info=True)
        except Exception:
            log.debug("!predict failed (non-fatal).", exc_info=True)
            await self.bot.safe_reply(ctx, "Couldn't reach Twitch just now.")
        return None
