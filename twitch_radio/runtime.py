"""Small shared runtime state: what the bot currently believes about itself
and the channel. Plain data + a couple of pure helpers, so /healthz, the
timers and the points loop can all read it without importing the bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Cosmetic suffixes rotated onto bot messages so Twitch's duplicate-message
# rule never drops one (see chatbot.py). Overridable with TWITCH_REPLY_SUFFIXES.
DEFAULT_REPLY_SUFFIXES: tuple[str, ...] = (" \u2728", " \U0001f4ab", " \u2b50", " \U0001f31f")


@dataclass(slots=True)
class LiveState:
    """Whether the channel is live. Fed by EventSub stream.online/offline when
    available and by the periodic Helix poll otherwise — whichever reported
    most recently wins."""

    is_live: bool | None = None  # None = not known yet
    changed_at: float | None = None  # monotonic

    def update(self, live: bool, *, now: float | None = None) -> bool:
        """Records a reading; returns True when it flipped the known state."""
        flipped = self.is_live is not None and self.is_live != live
        if self.is_live != live:
            self.changed_at = time.monotonic() if now is None else now
        self.is_live = live
        return flipped


@dataclass(slots=True)
class RuntimeStatus:
    chat_subscribed: bool = False
    last_chat_message_at: float | None = None  # monotonic
    last_award_tick_at: float | None = None  # monotonic
    alert_subscriptions: dict[str, bool] = field(default_factory=dict)
    # Optional scopes discovered missing at runtime ("delete", "timeout", "shoutout", "followage").
    scopes_missing: set[str] = field(default_factory=set)
    live: LiveState = field(default_factory=LiveState)

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now

        def age(ts: float | None) -> float | None:
            return None if ts is None else round(now - ts, 1)

        return {
            "chat_subscribed": self.chat_subscribed,
            "seconds_since_last_chat": age(self.last_chat_message_at),
            "seconds_since_last_award_tick": age(self.last_award_tick_at),
            "live": self.live.is_live,
            "seconds_since_live_state_changed": age(self.live.changed_at),
            "alert_subscriptions": dict(self.alert_subscriptions),
            "scopes_missing": sorted(self.scopes_missing),
        }
