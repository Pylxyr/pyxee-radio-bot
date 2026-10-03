"""Runtime-adjustable integer knobs.

Each knob is declared exactly once, as a dataclass field carrying its range,
label, help text and settings-page group in `metadata`. Everything else — the
/settings form, `!setlimit`, the clamping in `from_dict`, `to_dict` — is
derived from those declarations, so adding a tunable is one line here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any

log = logging.getLogger(__name__)


def _t(default: int, lo: int, hi: int, group: str, label: str, help_text: str) -> Any:
    return field(default=default, metadata={"lo": lo, "hi": hi, "group": group, "label": label, "help": help_text})


_POINTS = "Points & rewards"
_GAMBLE = "Gambling"
_FILTERS = "Chat filters"
_COMMANDS = "Custom commands"
_QUEUE = "Viewer queue"


@dataclass(slots=True)
class TwitchTunables:
    """Knobs adjustable from /settings or chat (`!setlimit`) without a restart."""

    points_per_active_minute: int = _t(
        1, 0, 100, _POINTS, "Points per active minute",
        "Awarded each minute to chatters active while live. 0 switches passive earning off.",
    )
    sub_multiplier_percent: int = _t(
        100, 100, 500, _POINTS, "Subscriber multiplier (%)",
        "Passive earning multiplier for subscribers. 100 = same as everyone, 200 = double.",
    )
    follow_bonus_points: int = _t(
        10, 0, 10000, _POINTS, "Follow bonus",
        "One-time points for a new follower (paid once per viewer, so unfollow/refollow can't farm it).",
    )
    sub_bonus_points: int = _t(
        100, 0, 10000, _POINTS, "Subscription bonus", "Points for each new subscription."
    )
    bits_points_per_100: int = _t(
        10, 0, 1000, _POINTS, "Points per 100 bits", "Points awarded for every 100 bits cheered. 0 disables."
    )
    daily_bonus_points: int = _t(
        50, 0, 10000, _POINTS, "!daily bonus", "Points from !daily (once per ~20 hours). 0 disables the command."
    )
    gamble_max_bet: int = _t(500, 1, 1_000_000, _GAMBLE, "Max bet", "Largest bet !gamble accepts.")
    gamble_win_chance_percent: int = _t(
        45, 1, 99, _GAMBLE, "Win chance (%)", "Chance a !gamble wins (pays 1:1). Below 50 gives the house an edge."
    )
    gamble_cooldown_seconds: int = _t(
        30, 0, 3600, _GAMBLE, "Gamble cooldown", "Seconds a chatter waits between !gamble uses."
    )
    duel_min_bet: int = _t(1, 1, 1_000_000, _GAMBLE, "Duel min bet", "Smallest amount !duel accepts.")
    duel_max_bet: int = _t(500, 1, 1_000_000, _GAMBLE, "Duel max bet", "Largest amount !duel accepts.")
    duel_timeout_seconds: int = _t(
        60, 10, 600, _GAMBLE, "Duel challenge window", "Seconds the challenged chatter has to !accept or !decline."
    )
    duel_challenger_win_chance_percent: int = _t(
        50, 1, 99, _GAMBLE, "Duel challenger win chance (%)",
        "Chance the challenger wins a !duel. 50 is a fair coin flip — this is PvP, not a wager against the house.",
    )
    queue_max_size: int = _t(0, 0, 10_000, _QUEUE, "Queue size cap", "Max viewers in !queue at once. 0 = unlimited.")
    caps_threshold_percent: int = _t(
        70, 50, 100, _FILTERS, "Caps threshold (%)", "Share of capital letters (ignoring emotes) that counts as shouting."
    )
    filter_warning_cooldown_seconds: int = _t(
        30, 0, 600, _FILTERS, "Warning cooldown",
        "Minimum seconds between public warnings to the same chatter (deletes/strikes still happen).",
    )
    filter_strikes_before_timeout: int = _t(
        3, 1, 10, _FILTERS, "Strikes before timeout", "Violations inside the window before a timeout (needs the timeout toggle)."
    )
    filter_strike_window_seconds: int = _t(
        600, 60, 86400, _FILTERS, "Strike window", "How long a violation counts toward a timeout."
    )
    filter_timeout_seconds: int = _t(60, 1, 86400, _FILTERS, "Timeout length", "Seconds a filter timeout lasts.")
    permit_default_seconds: int = _t(
        60, 10, 600, _FILTERS, "!permit length", "Seconds a chatter may post links after !permit."
    )
    custom_command_cooldown_seconds: int = _t(
        3, 0, 600, _COMMANDS, "Default cooldown", "Per-command cooldown unless !comopt overrides it."
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TwitchTunables:
        values: dict[str, int] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            try:
                value = int(data[f.name])
            except (TypeError, ValueError):
                log.warning("tunables.json: %r is not a valid number (%r) — using default.", f.name, data[f.name])
                continue
            lo, hi = f.metadata["lo"], f.metadata["hi"]
            if not lo <= value <= hi:
                clamped = max(lo, min(hi, value))
                log.warning("tunables.json: %r=%r outside %d-%d — clamping to %d.", f.name, value, lo, hi, clamped)
                value = clamped
            values[f.name] = value
        return cls(**values)

    def to_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# Derived views — the public names the settings page and !setlimit already use.
TUNABLE_BOUNDS: dict[str, tuple[int, int]] = {
    f.name: (f.metadata["lo"], f.metadata["hi"]) for f in fields(TwitchTunables)
}
TUNABLE_LABELS: dict[str, tuple[str, str]] = {
    f.name: (f.metadata["label"], f.metadata["help"]) for f in fields(TwitchTunables)
}
TUNABLE_GROUPS: dict[str, str] = {f.name: f.metadata["group"] for f in fields(TwitchTunables)}
