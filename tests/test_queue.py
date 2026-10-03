from twitch_radio.queue import ViewerQueue


def test_join_leave_and_position():
    q = ViewerQueue(open=True)
    assert q.join("1", "Alice") == "joined"
    assert q.join("1", "Alice") == "already"
    assert q.join("2", "Bob") == "joined"
    assert q.position("1") == 1 and q.position("2") == 2 and q.position("3") is None
    assert len(q) == 2
    assert q.leave("1") and not q.leave("1")
    assert q.position("2") == 1


def test_closed_queue_refuses_joins():
    q = ViewerQueue(open=False)
    assert q.join("1", "Alice") == "closed"


def test_max_size():
    q = ViewerQueue(open=True)
    assert q.join("1", "Alice", max_size=1) == "joined"
    assert q.join("2", "Bob", max_size=1) == "full"
    assert q.join("2", "Bob", max_size=0) == "joined"  # 0 = unlimited, re-checked fresh each call


def test_pop_next_and_clear():
    q = ViewerQueue(open=True)
    for uid, name in [("1", "A"), ("2", "B"), ("3", "C")]:
        q.join(uid, name)
    assert q.names() == ["A", "B", "C"]
    assert q.pop_next(2) == ["A", "B"]
    assert len(q) == 1 and q.position("3") == 1
    q.clear()
    assert len(q) == 0 and q.names() == []
