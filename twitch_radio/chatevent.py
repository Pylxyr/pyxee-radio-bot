"""One normalized view of an incoming chat message.

The bot converts twitchio's ChatMessage into this once, so the filter, the
economy and the timers all work on plain data (and can be tested without a
live Twitch connection)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChatEvent:
    user_id: str
    login: str
    name: str
    text: str
    message_id: str = ""
    # Emote-decorated fragments (see chatfeed.fragments_to_dicts + emotes.decorate).
    fragments: list[dict[str, object]] = field(default_factory=list)
    is_moderator: bool = False  # includes the broadcaster
    is_vip: bool = False
    is_subscriber: bool = False


def display_name_of(user: object) -> str:
    """A chatter's display name with sensible fallbacks (twitchio types it Optional)."""
    return str(getattr(user, "display_name", None) or getattr(user, "name", None) or getattr(user, "id", "") or "")
