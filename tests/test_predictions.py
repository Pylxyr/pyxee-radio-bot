
from conftest import run
from twitch_radio.predictions import CreatedPrediction, PredictionManager, parse_start_args


def test_parse_start_args():
    assert parse_start_args("60 Will it rain? ; Yes ; No") == (60, "Will it rain?", ["Yes", "No"])
    assert isinstance(parse_start_args("5 Too short ; Yes ; No"), str)  # below MIN_WINDOW_SECONDS
    assert isinstance(parse_start_args("abc Title ; Yes ; No"), str)
    assert isinstance(parse_start_args("60 Only one outcome ; Yes"), str)
    assert isinstance(parse_start_args("60 No outcomes at all"), str)
    many = " ; ".join(f"Opt{i}" for i in range(15))
    seconds, title, outcomes = parse_start_args(f"60 Title ; {many}")
    assert len(outcomes) == 10  # capped at MAX_OUTCOMES


def _manager():
    calls = []

    async def create(title, outcomes, seconds):
        calls.append(("create", title, outcomes, seconds))
        return CreatedPrediction(id="p1", outcome_ids=[f"o{i}" for i in range(len(outcomes))])

    async def end(pid, status, winning):
        calls.append(("end", pid, status, winning))

    return PredictionManager(create, end), calls


def test_full_lifecycle_through_resolve():
    async def go():
        pm, calls = _manager()
        await pm.start("Win?", ["Yes", "No"], 60)
        assert pm.current is not None and pm.current.outcome_titles == {"o0": "Yes", "o1": "No"}
        assert await pm.lock()
        assert await pm.resolve(2) == "No"
        assert pm.current is None
        assert calls == [
            ("create", "Win?", ["Yes", "No"], 60),
            ("end", "p1", "LOCKED", None),
            ("end", "p1", "RESOLVED", "o1"),
        ]

    run(go())


def test_resolve_rejects_out_of_range_and_missing():
    async def go():
        pm, _ = _manager()
        assert await pm.resolve(1) is None  # nothing started
        await pm.start("Win?", ["Yes", "No"], 60)
        assert await pm.resolve(0) is None
        assert await pm.resolve(3) is None
        assert pm.current is not None  # untouched by the failed resolves

    run(go())


def test_cancel():
    async def go():
        pm, calls = _manager()
        assert not await pm.cancel()
        await pm.start("Win?", ["Yes", "No"], 60)
        assert await pm.cancel()
        assert pm.current is None
        assert calls[-1] == ("end", "p1", "CANCELED", None)

    run(go())


def test_lock_with_nothing_running():
    async def go():
        pm, _ = _manager()
        assert not await pm.lock()

    run(go())


def test_consume_external_resolution_only_acts_when_still_tracked():
    async def go():
        pm, _ = _manager()
        assert pm.consume_external_resolution("o0") is None  # nothing running
        await pm.start("Win?", ["Yes", "No"], 60)
        assert pm.consume_external_resolution("not-an-id") is None
        assert pm.current is not None
        assert pm.consume_external_resolution("o0") == "Yes"
        assert pm.current is None
        assert pm.consume_external_resolution("o0") is None  # already consumed

    run(go())


def test_our_own_resolve_suppresses_the_external_path():
    async def go():
        pm, _ = _manager()
        await pm.start("Win?", ["Yes", "No"], 60)
        assert await pm.resolve(1) == "Yes"
        # The EventSub listener fires for every resolution, including ours —
        # consume_external_resolution must no-op since current is already clear.
        assert pm.consume_external_resolution("o0") is None

    run(go())


def test_consume_external_cancel():
    async def go():
        pm, _ = _manager()
        assert not pm.consume_external_cancel()
        await pm.start("Win?", ["Yes", "No"], 60)
        assert pm.consume_external_cancel()
        assert pm.current is None
        assert not pm.consume_external_cancel()

    run(go())
