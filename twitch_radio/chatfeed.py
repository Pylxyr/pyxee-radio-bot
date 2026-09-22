"""In-memory buffer of the most recent human chat messages, feeding the
public chat overlay (see the admin server's /chat-overlay, /chat.json,
/ws/chat) — the OBS-visible "show what chat's saying" widget.

Deliberately not persisted: this is a live "what's happening right now"
strip, not a chat log archive, so starting empty on every restart is
correct, not a bug. The bot's own messages never reach this class —
chatbot.py's _track_and_filter only calls append() after its own
chatter.id == self._bot_id check returns, the same point every other
per-chatter tracking hooks in, so there's one exclusion point, not two
that could disagree.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_DEFAULT_MAX_MESSAGES = 10
_DEFAULT_MAX_AGE_SECONDS = 600.0  # 10 minutes


# Twitch emote IDs are numeric ("25") or "emotesv2_<hex>" for newer ones. Only
# IDs matching this are ever sent to the overlay, which splices them into an
# image URL — anything else is shown as plain text instead.
_EMOTE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CHEER_PREFIX = re.compile(r"[A-Za-z0-9]{1,40}")
_MAX_FRAGMENTS = 500  # Twitch caps a message at 500 characters, so this never truncates a real one


def fragments_to_dicts(fragments: Iterable[Any]) -> list[dict[str, object]]:
    """Turns a chat message's structured fragments (TwitchIO's
    ChatMessageFragment list, read by attribute so this needs no TwitchIO
    import) into plain JSON-ready dicts the overlay can render:

        {"type": "text", "text": "hello "}
        {"type": "emote", "id": "25", "name": "Kappa", "animated": False}
        {"type": "cheermote", "name": "Cheer100", "prefix": "Cheer", "bits": 100, "tier": 100}

    Emotes reach the bot as fragments with an ID; the message's plain
    `text` only has their names, which is why the overlay used to show
    letters instead of pictures. Cheermotes carry prefix/bits/tier so
    emotes.EmoteService can attach artwork. Everything else becomes text,
    with adjacent text runs merged.
    """
    out: list[dict[str, object]] = []
    for frag in list(fragments)[:_MAX_FRAGMENTS]:
        text = getattr(frag, "text", "") or ""
        emote = getattr(frag, "emote", None)
        if getattr(frag, "type", "") == "emote" and emote is not None:
            emote_id = str(getattr(emote, "id", ""))
            if _EMOTE_ID.fullmatch(emote_id):
                formats = getattr(emote, "format", None) or []
                out.append({"type": "emote", "id": emote_id, "name": text, "animated": "animated" in formats})
                continue
        cheer = getattr(frag, "cheermote", None)
        if getattr(frag, "type", "") == "cheermote" and cheer is not None:
            prefix = str(getattr(cheer, "prefix", ""))
            try:
                bits, tier = int(getattr(cheer, "bits", 0)), int(getattr(cheer, "tier", 0))
            except (TypeError, ValueError):
                bits = tier = 0
            if _CHEER_PREFIX.fullmatch(prefix) and bits > 0:
                out.append({"type": "cheermote", "name": text, "prefix": prefix, "bits": bits, "tier": tier})
                continue
        if out and out[-1]["type"] == "text":
            out[-1]["text"] = str(out[-1]["text"]) + text
        else:
            out.append({"type": "text", "text": text})
    return out


@dataclass(slots=True)
class ChatEntry:
    id: int  # unique per message, so two identical messages in a row are still two messages
    author: str
    text: str
    at: float  # time.monotonic() — a reference for age pruning, never shown as a clock time
    fragments: list[dict[str, object]] = field(default_factory=list)


class ChatFeed:
    """Bounded to the last N messages (default 10) and separately to no
    message older than the configured age (default 10 minutes) —
    whichever limit hits first, enforced on every append() and read.

    max_messages/max_age_seconds are constructor parameters rather than
    module constants so limits can change without editing this file;
    every real caller uses the defaults.
    """

    def __init__(self, max_messages: int = _DEFAULT_MAX_MESSAGES, max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS) -> None:
        self._max_messages = max_messages
        self._max_age_seconds = max_age_seconds
        self._entries: list[ChatEntry] = []
        self._next_id = 1
        # Same wakeup-queue idea as RadioPlayer's subscribe_state()/
        # _notify_state_changed(): subscribers get an empty "something
        # changed" ping and re-fetch snapshot() themselves, rather than the
        # payload being pushed through the queue directly. Consistent with
        # how /ws/nowplaying already works, and it means this class has no
        # opinion at all about JSON shape — that's admin/handlers/live.py's job.
        self._state_subscribers: set[asyncio.Queue[None]] = set()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._max_age_seconds
        self._entries = [e for e in self._entries if e.at >= cutoff][-self._max_messages :]

    def append(self, author: str, text: str, fragments: list[dict[str, object]] | None = None) -> None:
        """`fragments` (see fragments_to_dicts) carries emotes as images-to-be;
        without it the message is shown as plain text."""
        text = text.strip()
        if not text:
            return
        entry = ChatEntry(
            id=self._next_id,
            author=author,
            text=text,
            at=time.monotonic(),
            fragments=fragments or [{"type": "text", "text": text}],
        )
        self._next_id += 1
        self._entries.append(entry)
        self._prune()
        self._notify_state_changed()

    def snapshot(self) -> list[dict[str, object]]:
        """Each entry's age at the moment of the call, not a raw
        timestamp — time.monotonic() has no meaning outside this process,
        so shipping it to a browser would be useless (or actively
        misleading once serialized as if it were a real clock). The
        client ticks this forward itself between pushes and ages a
        message out locally once it crosses the 10-minute mark, rather
        than waiting on the server to notice and push again — see
        chat_overlay.html's effectiveMessages()."""
        self._prune()
        now = time.monotonic()
        return [
            {"id": e.id, "author": e.author, "text": e.text, "fragments": e.fragments, "age_seconds": now - e.at}
            for e in self._entries
        ]

    def subscribe_state(self) -> asyncio.Queue[None]:
        q: asyncio.Queue[None] = asyncio.Queue(maxsize=4)
        self._state_subscribers.add(q)
        return q

    def unsubscribe_state(self, q: asyncio.Queue[None]) -> None:
        self._state_subscribers.discard(q)

    def _notify_state_changed(self) -> None:
        for q in list(self._state_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)
