from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Same "single source of truth" idea as tunables.TUNABLE_BOUNDS — shared by
# /settings and !toggle so both stay in sync on valid keys.
TOGGLE_KEYS: dict[str, str] = {
    "radio_autoplay_enabled": "Auto-queue similar tracks when the queue runs dry",
    "link_filter_enabled": "Warn on links from chatters (mods/broadcaster exempt)",
    "caps_filter_enabled": "Warn on excessive-caps messages (mods/broadcaster exempt)",
    "filter_delete_enabled": (
        "Also delete the offending message (needs moderator:manage:chat_messages "
        "on the bot's token — see README; silently stays warn-only without it)"
    ),
}


@dataclass(slots=True)
class FeatureToggles:
    # Defaults to on: this is the specific gap ("radio silence") this
    # feature exists to close. Moderation filters default off — a false
    # positive is a chat-visible annoyance (or, for filter_delete_enabled,
    # a removed message), so all of them are opt-in.
    radio_autoplay_enabled: bool = True
    link_filter_enabled: bool = False
    caps_filter_enabled: bool = False
    filter_delete_enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeatureToggles":
        defaults = cls()

        def _field(name: str, default: bool) -> bool:
            if name not in data:
                return default
            value = data[name]
            return value if isinstance(value, bool) else default

        return cls(
            radio_autoplay_enabled=_field("radio_autoplay_enabled", defaults.radio_autoplay_enabled),
            link_filter_enabled=_field("link_filter_enabled", defaults.link_filter_enabled),
            caps_filter_enabled=_field("caps_filter_enabled", defaults.caps_filter_enabled),
            filter_delete_enabled=_field("filter_delete_enabled", defaults.filter_delete_enabled),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "radio_autoplay_enabled": self.radio_autoplay_enabled,
            "link_filter_enabled": self.link_filter_enabled,
            "caps_filter_enabled": self.caps_filter_enabled,
            "filter_delete_enabled": self.filter_delete_enabled,
        }
