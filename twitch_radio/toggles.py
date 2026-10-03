"""On/off feature switches, declared once as dataclass fields (see tunables.py
for the same pattern). `TOGGLE_KEYS` (key -> description) is derived."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any


def _b(default: bool, description: str) -> Any:
    return field(default=default, metadata={"description": description})


@dataclass(slots=True)
class FeatureToggles:
    # Anything that makes the bot say something new defaults off — the
    # streamer opts in rather than discovering it in chat.
    link_filter_enabled: bool = _b(False, "Warn on links from chatters (mods/broadcaster exempt; see !permit and !allowdomain)")
    caps_filter_enabled: bool = _b(False, "Warn on excessive-caps messages (emotes are ignored)")
    term_filter_enabled: bool = _b(False, "Warn on blocked terms (manage with !blockterm)")
    filter_delete_enabled: bool = _b(
        False,
        "Also delete the offending message (needs moderator:manage:chat_messages on the bot's token — "
        "stays warn-only without it)",
    )
    filter_timeout_enabled: bool = _b(
        False,
        "Time a chatter out after repeated violations (needs moderator:manage:banned_users on the bot's token)",
    )
    filter_exempt_vips: bool = _b(True, "VIPs are exempt from the chat filters")
    filter_exempt_subs: bool = _b(False, "Subscribers are exempt from the chat filters")
    alerts_enabled: bool = _b(
        False,
        "Announce follows/subs/cheers/raids/Hype Trains in chat and auto-shoutout raiders (some need extra "
        "OAuth scopes; each degrades independently)",
    )
    gamble_enabled: bool = _b(False, "Let chatters spend points on !gamble")
    duels_enabled: bool = _b(False, "Let chatters wager points against each other with !duel")
    timers_enabled: bool = _b(True, "Post scheduled timer messages while the channel is live (manage with !timer)")
    stream_summary_enabled: bool = _b(False, "Post the top point earners when the stream ends")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureToggles:
        values = {f.name: data[f.name] for f in fields(cls) if isinstance(data.get(f.name), bool)}
        return cls(**values)

    def to_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


TOGGLE_KEYS: dict[str, str] = {f.name: f.metadata["description"] for f in fields(FeatureToggles)}
