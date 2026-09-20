"""In-memory buffer of the most recent human chat messages, feeding the
public chat overlay (see admin_server.py's /chat-overlay, /chat.json,
/ws/chat) — the OBS-visible "show what chat's saying" widget.

Deliberately not persisted anywhere: this is a live "what's happening
right now" strip, not a chat log archive, so starting empty on every
restart is correct, not a bug. The bot's own messages never reach this
class at all — chatbot.py's _track_and_filter only calls append() after
its own chatter.id == self._bot_id check has already returned, the same
point every other per-chatter tracking in this file hooks in, so there's
one exclusion point, not two places that both need to agree on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

_DEFAULT_MAX_MESSAGES = 10
_DEFAULT_MAX_AGE_SECONDS = 600.0  # 10 minutes


@dataclass(slots=True)
class ChatEntry:
    author: str
    text: str
    at: float  # time.monotonic() — a reference for age pruning, never shown as a clock time


class ChatFeed:
    """Bounded to the last N messages (default 10) *and* separately, no
    message older than the configured age (default 10 minutes) — whichever
    limit a given message hits first. Both are enforced on every append()
    and every read, so a message doesn't linger past its 10 minutes just
    because fewer than 10 have arrived since to push it out.

    max_messages/max_age_seconds are constructor params rather than module
    constants purely so tests can use a short age window instead of
    waiting on a real 10-minute clock; every real caller uses the defaults.
    """

    def __init__(self, max_messages: int = _DEFAULT_MAX_MESSAGES, max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS) -> None:
        self._max_messages = max_messages
        self._max_age_seconds = max_age_seconds
        self._entries: list[ChatEntry] = []
        # Same wakeup-queue idea as RadioPlayer's subscribe_state()/
        # _notify_state_changed(): subscribers get an empty "something
        # changed" ping and re-fetch snapshot() themselves, rather than the
        # payload being pushed through the queue directly. Consistent with
        # how /ws/nowplaying already works, and it means this class has no
        # opinion at all about JSON shape — that's admin_server.py's job.
        self._state_subscribers: set[asyncio.Queue[None]] = set()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._max_age_seconds
        self._entries = [e for e in self._entries if e.at >= cutoff][-self._max_messages :]

    def append(self, author: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._entries.append(ChatEntry(author=author, text=text, at=time.monotonic()))
        self._prune()
        self._notify_state_changed()

    def snapshot(self) -> list[dict[str, object]]:
        """Each entry's age at the moment of the call, not a raw
        timestamp — time.monotonic() has no meaning outside this process,
        so shipping it to a browser would be useless (or actively
        misleading once serialized as if it were a real clock). The
        client ticks this forward itself between pushes the same way the
        now-playing overlay already does for elapsed track time, and ages
        a message out locally once it crosses the 10-minute mark, rather
        than waiting on the server to notice and push again — see
        _CHAT_OVERLAY_HTML's tick()."""
        self._prune()
        now = time.monotonic()
        return [{"author": e.author, "text": e.text, "age_seconds": now - e.at} for e in self._entries]

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
