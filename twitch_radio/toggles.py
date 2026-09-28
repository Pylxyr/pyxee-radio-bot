from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Same "single source of truth" idea as tunables.TUNABLE_BOUNDS — shared by
# /settings and !toggle so both stay in sync on valid keys.
TOGGLE_KEYS: dict[str, str] = {
    "link_filter_enabled": "Warn on links from chatters (mods/broadcaster exempt)",
    "caps_filter_enabled": "Warn on excessive-caps messages (mods/broadcaster exempt)",
    "filter_delete_enabled": (
        "Also delete the offending message (needs moderator:manage:chat_messages "
        "on the bot's token — see README; silently stays warn-only without it)"
    ),
    "alerts_enabled": (
        "Post chat announcements for follows/subs/cheers/raids, and auto-shoutout "
        "a raider (needs extra OAuth scopes for some of these — see README; each "
        "degrades independently, so this is safe to turn on regardless of which "
        "scopes are actually granted)"
    ),
}


@dataclass(slots=True)
class FeatureToggles:
    # All default off — new chat-visible behavior (a filter warning, a
    # follow announcement) is something the streamer should opt into, not
    # discover.
    link_filter_enabled: bool = False
    caps_filter_enabled: bool = False
    filter_delete_enabled: bool = False
    alerts_enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeatureToggles":
        defaults = cls()

        def _field(name: str, default: bool) -> bool:
            if name not in data:
                return default
            value = data[name]
            return value if isinstance(value, bool) else default

        return cls(
            link_filter_enabled=_field("link_filter_enabled", defaults.link_filter_enabled),
            caps_filter_enabled=_field("caps_filter_enabled", defaults.caps_filter_enabled),
            filter_delete_enabled=_field("filter_delete_enabled", defaults.filter_delete_enabled),
            alerts_enabled=_field("alerts_enabled", defaults.alerts_enabled),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "link_filter_enabled": self.link_filter_enabled,
            "caps_filter_enabled": self.caps_filter_enabled,
            "filter_delete_enabled": self.filter_delete_enabled,
            "alerts_enabled": self.alerts_enabled,
        }
