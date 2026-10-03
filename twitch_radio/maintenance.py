"""Background housekeeping: rolling SQLite backups of community.db."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from twitch_radio.db import Database

log = logging.getLogger(__name__)

BACKUP_INTERVAL_SECONDS = 24 * 3600
_PREFIX = "community-"
_SUFFIX = ".db"


def list_backups(directory: Path) -> list[Path]:
    """Existing backups, oldest first (names embed a sortable timestamp)."""
    return sorted(directory.glob(f"{_PREFIX}*{_SUFFIX}"))


def prune_backups(directory: Path, keep: int) -> list[Path]:
    """Deletes all but the newest `keep` backups; returns what was removed."""
    backups = list_backups(directory)
    doomed = backups[: max(0, len(backups) - max(keep, 0))]
    for path in doomed:
        with contextlib.suppress(OSError):
            path.unlink()
    return doomed


async def backup_once(db: Database, directory: Path, keep: int, *, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))
    dest = directory / f"{_PREFIX}{stamp}{_SUFFIX}"
    await db.backup_to(dest)
    prune_backups(directory, keep)
    return dest


async def backup_loop(db: Database, directory: Path, keep: int) -> None:
    """Backs up on startup unless a recent one exists, then once a day. A failed
    backup is logged and retried next cycle — it never takes the bot down."""
    while True:
        try:
            newest = list_backups(directory)[-1:]
            fresh = bool(newest) and time.time() - newest[0].stat().st_mtime < BACKUP_INTERVAL_SECONDS * 0.9
            if not fresh:
                path = await backup_once(db, directory, keep)
                log.info("Database backed up to %s (keeping %d).", path, keep)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Database backup failed (will retry).")
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
