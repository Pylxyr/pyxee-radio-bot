import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twitch_radio.db import Database  # noqa: E402
from twitch_radio.store import JsonStore  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def make_db(tmp_path):
    """Factory returning a connected Database; closed automatically."""
    opened = []

    async def _make(name="test.db"):
        db = Database(tmp_path / name)
        await db.connect()
        opened.append(db)
        return db

    yield _make
    for db in opened:
        run(db.close())


@pytest.fixture
def stores(tmp_path):
    return JsonStore(tmp_path / "tunables.json"), JsonStore(tmp_path / "toggles.json")
