from twitch_radio.cooldown import CooldownTracker
from twitch_radio.runtime import LiveState, RuntimeStatus


def test_cooldown_uses_injected_clock():
    now = [0.0]
    cd = CooldownTracker(clock=lambda: now[0])
    assert cd.remaining("k", 10) == 0
    cd.mark("k")
    assert cd.remaining("k", 10) == 10
    now[0] = 4
    assert cd.remaining("k", 10) == 6
    now[0] = 11
    assert cd.remaining("k", 10) == 0
    assert cd.remaining("k", 0) == 0


def test_live_state_reports_flips():
    live = LiveState()
    assert live.update(True, now=1.0) is False  # first reading is not a flip
    assert live.update(True, now=2.0) is False
    assert live.update(False, now=3.0) is True and live.changed_at == 3.0


def test_status_snapshot_is_json_friendly():
    status = RuntimeStatus(chat_subscribed=True, last_chat_message_at=10.0)
    status.scopes_missing.add("timeout")
    snap = status.snapshot(now=15.5)
    assert snap["seconds_since_last_chat"] == 5.5 and snap["seconds_since_last_award_tick"] is None
    assert snap["scopes_missing"] == ["timeout"] and snap["live"] is None
