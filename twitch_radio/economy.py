"""The points economy: passive earning, event bonuses, ranks, !daily, !give
and !gamble. Pure helpers up top (easy to test); `Economy` below owns the
in-memory activity/session state and talks to the Database.

Every method that answers a chatter returns a plain string — the components
just send it, so the wording and the rules live in one place."""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from twitch_radio.chatevent import ChatEvent
from twitch_radio.cooldown import CooldownTracker
from twitch_radio.db import Database
from twitch_radio.runtime import RuntimeStatus
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

AWARD_TICK_SECONDS = 60
ACTIVE_WINDOW_SECONDS = 300.0
LAST_SEEN_PRUNE_SECONDS = 3600.0
DAILY_COOLDOWN_SECONDS = 20 * 3600
GIVE_COOLDOWN_SECONDS = 10.0

# (watch seconds needed, title). Watch time only accrues while chatting live.
RANKS: tuple[tuple[int, str], ...] = (
    (0, "Newcomer"),
    (1 * 3600, "Regular"),
    (10 * 3600, "Fan"),
    (25 * 3600, "Veteran"),
    (60 * 3600, "Legend"),
)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@dataclass(frozen=True, slots=True)
class RankInfo:
    title: str
    next_title: str | None
    seconds_to_next: int | None


def rank_for(watch_seconds: int) -> RankInfo:
    current = RANKS[0]
    upcoming: tuple[int, str] | None = None
    for threshold, title in RANKS:
        if watch_seconds >= threshold:
            current = (threshold, title)
        else:
            upcoming = (threshold, title)
            break
    if upcoming is None:
        return RankInfo(current[1], None, None)
    return RankInfo(current[1], upcoming[1], upcoming[0] - watch_seconds)


def points_for_tick(base: int, subscriber: bool, multiplier_percent: int) -> int:
    if base <= 0 or not subscriber:
        return max(0, base)
    return max(base, (base * multiplier_percent + 50) // 100)


def bits_points(bits: int, per_100: int) -> int:
    return max(0, bits // 100) * max(0, per_100)


def parse_bet(raw: str, balance: int, max_bet: int) -> tuple[int | None, str | None]:
    """(bet, None) on success, (None, error message) otherwise. Accepts a
    number, `all`, `half`, or a percentage like `25%`; always capped at max_bet."""
    text = raw.strip().lower()
    if not text:
        return None, "Usage: !gamble <amount|all|half|25%>"
    if balance <= 0:
        return None, "You don't have any points to bet."
    if text == "all":
        bet = min(balance, max_bet)
    elif text == "half":
        bet = min(max(1, balance // 2), max_bet)
    elif text.endswith("%") and text[:-1].isdigit():
        pct = int(text[:-1])
        if not 1 <= pct <= 100:
            return None, "Percentages must be between 1% and 100%."
        bet = min(max(1, balance * pct // 100), max_bet)
    elif text.isdigit():
        bet = int(text)
        if bet < 1:
            return None, "Bet at least 1 point."
        if bet > max_bet:
            return None, f"The max bet is {max_bet}."
        if bet > balance:
            return None, f"You only have {balance} points."
    else:
        return None, "Usage: !gamble <amount|all|half|25%>"
    return bet, None


@dataclass(slots=True)
class _Seen:
    at: float
    name: str
    subscriber: bool


class Economy:
    def __init__(
        self,
        db: Database,
        tunables_store: JsonStore,
        toggles_store: JsonStore,
        status: RuntimeStatus,
        *,
        is_live: Callable[[], Awaitable[bool]],
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._tunables_store = tunables_store
        self._toggles_store = toggles_store
        self._status = status
        self._is_live = is_live
        self._rng = rng or random.Random()
        self._clock = clock
        self._seen: dict[str, _Seen] = {}
        self._cooldowns = CooldownTracker(clock)
        self._session_points: Counter[str] = Counter()
        self._session_names: dict[str, str] = {}

    async def _tunables(self) -> TwitchTunables:
        return TwitchTunables.from_dict(await self._tunables_store.read())

    # -- passive earning --------------------------------------------------

    def note_activity(self, event: ChatEvent) -> None:
        self._seen[event.user_id] = _Seen(self._clock(), event.name or event.user_id, event.is_subscriber)

    def _note_session(self, user_id: str, name: str, points: int) -> None:
        if points > 0:
            self._session_points[user_id] += points
            self._session_names[user_id] = name

    async def award_tick(self) -> int:
        """One passive-earning tick; returns how many chatters were credited.
        Watch time accrues whenever the channel is live, even if points are
        switched off."""
        now = self._clock()
        for uid in [u for u, s in self._seen.items() if now - s.at > LAST_SEEN_PRUNE_SECONDS]:
            del self._seen[uid]
        active = {uid: s for uid, s in self._seen.items() if now - s.at <= ACTIVE_WINDOW_SECONDS}
        if not active or not await self._is_live():
            return 0
        tunables = await self._tunables()
        entries = []
        for uid, seen in active.items():
            pts = points_for_tick(tunables.points_per_active_minute, seen.subscriber, tunables.sub_multiplier_percent)
            entries.append((uid, seen.name, pts, AWARD_TICK_SECONDS))
            self._note_session(uid, seen.name, pts)
        await self._db.bulk_award(entries)
        self._status.last_award_tick_at = now
        return len(entries)

    # -- one-off bonuses --------------------------------------------------

    async def on_follow(self, user_id: str, name: str) -> int | None:
        points = (await self._tunables()).follow_bonus_points
        if points > 0 and await self._db.claim_follow_bonus(user_id, name, points):
            self._note_session(user_id, name, points)
            return points
        return None

    async def on_subscribe(self, user_id: str, name: str) -> int | None:
        points = (await self._tunables()).sub_bonus_points
        if points <= 0:
            return None
        await self._db.credit(user_id, name, points)
        self._note_session(user_id, name, points)
        return points

    async def on_cheer(self, user_id: str, name: str, bits: int) -> int | None:
        points = bits_points(bits, (await self._tunables()).bits_points_per_100)
        if points <= 0:
            return None
        await self._db.credit(user_id, name, points)
        self._note_session(user_id, name, points)
        return points

    # -- chatter-facing answers ------------------------------------------

    async def points_line(self, user_id: str, name: str) -> str:
        stats = await self._db.get_stats(user_id)
        points = stats["points"] if stats else 0
        watched = stats["watch_seconds"] if stats else 0
        return f"{name}: {points} points, {format_duration(watched)} watched ({rank_for(watched).title})."

    async def rank_line(self, user_id: str, name: str) -> str:
        stats = await self._db.get_stats(user_id)
        watched = stats["watch_seconds"] if stats else 0
        info = rank_for(watched)
        text = f"{name} is a {info.title} with {format_duration(watched)} of chat time"
        if info.next_title and info.seconds_to_next is not None:
            return f"{text} — {format_duration(info.seconds_to_next)} to {info.next_title}."
        return f"{text} — top rank reached!"

    async def daily(self, user_id: str, name: str) -> str:
        points = (await self._tunables()).daily_bonus_points
        if points <= 0:
            return "!daily is turned off."
        claimed, wait, balance = await self._db.claim_daily(user_id, name, points, DAILY_COOLDOWN_SECONDS)
        if not claimed:
            return f"{name}, you already claimed today — back in {format_duration(wait)}."
        self._note_session(user_id, name, points)
        return f"{name} claimed {points} points! Balance: {balance}."

    async def gamble(self, user_id: str, name: str, raw_bet: str) -> str:
        if not FeatureToggles.from_dict(await self._toggles_store.read()).gamble_enabled:
            return "Gambling is turned off."
        tunables = await self._tunables()
        wait = self._cooldowns.remaining(f"gamble:{user_id}", tunables.gamble_cooldown_seconds)
        if wait > 0:
            return f"{name}, cool down for {wait:.0f}s before gambling again."
        stats = await self._db.get_stats(user_id)
        bet, error = parse_bet(raw_bet, stats["points"] if stats else 0, tunables.gamble_max_bet)
        if bet is None:
            return error or "Usage: !gamble <amount>"
        won = self._rng.random() * 100 < tunables.gamble_win_chance_percent
        balance = await self._db.settle_gamble(user_id, bet, won)
        if balance is None:
            return f"{name}, you don't have {bet} points."
        self._cooldowns.mark(f"gamble:{user_id}")
        verdict = f"WON {bet}" if won else f"lost {bet}"
        return f"{name} bet {bet} and {verdict}. Balance: {balance}."

    async def give(self, from_id: str, from_name: str, target: str, raw_amount: str) -> str:
        target = target.strip().lstrip("@")
        if not target or not raw_amount.strip().isdigit() or int(raw_amount) < 1:
            return "Usage: !give <user> <amount>"
        amount = int(raw_amount)
        wait = self._cooldowns.remaining(f"give:{from_id}", GIVE_COOLDOWN_SECONDS)
        if wait > 0:
            return f"Slow down — try again in {wait:.0f}s."
        found = await self._db.find_by_name(target)
        if found is None:
            return f"I haven't seen {target} in chat yet."
        to_id, to_name = found
        if to_id == from_id:
            return "You can't give points to yourself."
        result = await self._db.transfer(from_id, to_id, to_name, amount)
        if result is None:
            stats = await self._db.get_stats(from_id)
            return f"You only have {stats['points'] if stats else 0} points."
        self._cooldowns.mark(f"give:{from_id}")
        return f"{from_name} gave {amount} points to {to_name}. (Balance: {result[0]})"

    # -- stream sessions --------------------------------------------------

    def session_summary(self, limit: int = 3) -> str | None:
        top = self._session_points.most_common(limit)
        if not top:
            return None
        ranked = ", ".join(f"{self._session_names.get(uid, uid)} (+{pts})" for uid, pts in top)
        return f"Top earners this stream: {ranked}"

    def reset_session(self) -> None:
        self._session_points.clear()
        self._session_names.clear()
