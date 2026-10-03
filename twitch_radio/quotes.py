"""!quote formatting — pure functions; twitch_radio.db.Database owns storage."""

from __future__ import annotations

import datetime

from twitch_radio.db import Quote

MAX_QUOTE_LENGTH = 400


def format_quote(quote: Quote) -> str:
    when = datetime.datetime.fromtimestamp(quote.added_at, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    return f'#{quote.id}: "{quote.text}" ({when})'


def parse_quote_id(raw: str) -> int | None:
    raw = raw.strip().lstrip("#")
    return int(raw) if raw.isdigit() else None
