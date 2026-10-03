from twitch_radio.db import Quote
from twitch_radio.quotes import format_quote, parse_quote_id


def test_parse_quote_id():
    assert parse_quote_id("3") == 3
    assert parse_quote_id("#3") == 3
    assert parse_quote_id("  7  ") == 7
    assert parse_quote_id("abc") is None
    assert parse_quote_id("") is None
    assert parse_quote_id("-1") is None  # lstrip("#") only; a bare minus isn't a digit string


def test_format_quote_includes_id_text_and_date():
    q = Quote(id=5, text="gg", added_by="1", added_at=1_700_000_000)
    out = format_quote(q)
    assert out.startswith("#5:") and '"gg"' in out and "2023-11" in out
