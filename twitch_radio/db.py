from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)

_T = TypeVar("_T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS viewer_stats (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    watch_seconds INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_commands (
    name TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at REAL NOT NULL
);
"""


class Database:
    """Async wrapper over sqlite3 for per-viewer/community data — the stuff
    JsonStore's whole-file-rewrite model is the wrong shape for (it scales
    with viewer count, not with a handful of global settings). One
    `check_same_thread=False` connection, WAL mode (safe concurrent
    read/write with far less fsync overhead than the default rollback
    journal — matches this project's resource-conscious deploy targets),
    and an asyncio.Lock serializing access, same philosophy as JsonStore.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            self._conn = await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    async def _run(self, fn: Callable[..., _T], *args: Any) -> _T:
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Database.connect() was never called")
            return await asyncio.to_thread(fn, self._conn, *args)

    # -- viewer stats -----------------------------------------------------

    def _bulk_award_sync(
        self, conn: sqlite3.Connection, entries: list[tuple[str, str, int, int]]
    ) -> None:
        now = time.time()
        conn.executemany(
            """
            INSERT INTO viewer_stats (user_id, display_name, points, watch_seconds, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                points = points + excluded.points,
                watch_seconds = watch_seconds + excluded.watch_seconds,
                updated_at = excluded.updated_at
            """,
            [(uid, name, pts, secs, now) for uid, name, pts, secs in entries],
        )
        conn.commit()

    async def bulk_award(self, entries: list[tuple[str, str, int, int]]) -> None:
        """entries: (user_id, display_name, points_delta, watch_seconds_delta).
        One transaction for the whole batch — called once per award tick
        (see chatbot.py's passive-points loop), not once per viewer."""
        if not entries:
            return
        await self._run(self._bulk_award_sync, entries)

    def _get_stats_sync(self, conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT display_name, points, watch_seconds FROM viewer_stats WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return {"display_name": row[0], "points": row[1], "watch_seconds": row[2]}

    async def get_stats(self, user_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_stats_sync, user_id)

    def _top_sync(self, conn: sqlite3.Connection, column: str, limit: int) -> list[tuple[str, int]]:
        # column is never user input — only "points" or "watch_seconds" from the methods below.
        rows = conn.execute(
            f"SELECT display_name, {column} FROM viewer_stats WHERE {column} > 0 "
            f"ORDER BY {column} DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    async def top_points(self, limit: int = 5) -> list[tuple[str, int]]:
        return await self._run(self._top_sync, "points", limit)

    async def top_watch_seconds(self, limit: int = 5) -> list[tuple[str, int]]:
        return await self._run(self._top_sync, "watch_seconds", limit)

    # -- custom commands ----------------------------------------------------

    def _get_command_sync(self, conn: sqlite3.Connection, name: str) -> str | None:
        row = conn.execute("SELECT response FROM custom_commands WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE custom_commands SET uses = uses + 1 WHERE name = ?", (name,))
        conn.commit()
        return str(row[0])

    async def get_command(self, name: str) -> str | None:
        """Returns the response text and increments its use counter, or
        None if no such command exists."""
        return await self._run(self._get_command_sync, name)

    def _set_command_sync(self, conn: sqlite3.Connection, name: str, response: str, created_by: str) -> None:
        conn.execute(
            """
            INSERT INTO custom_commands (name, response, created_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET response = excluded.response
            """,
            (name, response, created_by, time.time()),
        )
        conn.commit()

    async def set_command(self, name: str, response: str, created_by: str) -> None:
        await self._run(self._set_command_sync, name, response, created_by)

    def _delete_command_sync(self, conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute("DELETE FROM custom_commands WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    async def delete_command(self, name: str) -> bool:
        return await self._run(self._delete_command_sync, name)

    def _list_commands_sync(self, conn: sqlite3.Connection) -> list[str]:
        return [r[0] for r in conn.execute("SELECT name FROM custom_commands ORDER BY name").fetchall()]

    async def list_commands(self) -> list[str]:
        return await self._run(self._list_commands_sync)

    # -- quotes -------------------------------------------------------------

    def _add_quote_sync(self, conn: sqlite3.Connection, text: str, added_by: str) -> int:
        cur = conn.execute(
            "INSERT INTO quotes (text, added_by, added_at) VALUES (?, ?, ?)", (text, added_by, time.time())
        )
        conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("INSERT into quotes did not produce a rowid")
        return cur.lastrowid

    async def add_quote(self, text: str, added_by: str) -> int:
        return await self._run(self._add_quote_sync, text, added_by)

    def _get_quote_sync(self, conn: sqlite3.Connection, quote_id: int | None) -> tuple[int, str] | None:
        if quote_id is None:
            row = conn.execute("SELECT id, text FROM quotes ORDER BY RANDOM() LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT id, text FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return (row[0], row[1]) if row is not None else None

    async def get_quote(self, quote_id: int | None = None) -> tuple[int, str] | None:
        return await self._run(self._get_quote_sync, quote_id)

    def _delete_quote_sync(self, conn: sqlite3.Connection, quote_id: int) -> bool:
        cur = conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
        conn.commit()
        return cur.rowcount > 0

    async def delete_quote(self, quote_id: int) -> bool:
        return await self._run(self._delete_quote_sync, quote_id)

    def _count_quotes_sync(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()
        return int(row[0]) if row else 0

    async def count_quotes(self) -> int:
        return await self._run(self._count_quotes_sync)
