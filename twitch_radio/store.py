from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class JsonStore:
    """Tiny atomic JSON key-value file, guarded by an in-process asyncio.Lock.

    Reads/writes are serialized by the lock (this data is only ever touched
    from the chat bot's commands and the /settings HTTP handler, both on the
    same event loop), and writes are write-temp-then-rename so a crash
    mid-write can never leave a corrupt or half-written file behind.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def read(self) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._read_sync)

    async def write(self, data: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)

    async def update(
        self, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any]:
        """Read-modify-write while holding the lock across all three steps,
        so two concurrent callers (e.g. two /settings submissions) can't
        silently clobber each other. `mutator` returns the dict to persist,
        or None to leave the file untouched."""
        async with self._lock:
            current = await asyncio.to_thread(self._read_sync)
            updated = mutator(current)
            if updated is None:
                return current
            await asyncio.to_thread(self._write_sync, updated)
            return updated

    def _read_sync(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            # Falls back to defaults either way, but this should show up in
            # the logs rather than silently vanishing someone's saved settings.
            log.warning("Couldn't read %s — falling back to defaults.", self._path, exc_info=True)
            return {}
        if not isinstance(loaded, dict):
            log.warning("%s did not contain a JSON object — falling back to defaults.", self._path)
            return {}
        return loaded

    def _write_sync(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise
