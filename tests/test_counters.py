from conftest import run
from twitch_radio.counters import CounterStore, split_suffix


def test_split_suffix():
    assert split_suffix("deaths") == ("deaths", None)
    assert split_suffix("deaths++") == ("deaths", "++")
    assert split_suffix("Deaths--") == ("deaths", "--")
    assert split_suffix("deaths+5") is None
    assert split_suffix("two words") is None
    assert split_suffix("") is None


def test_counter_lifecycle(make_db):
    async def go():
        db = await make_db()
        store = CounterStore(db)
        await store.load()
        assert store.get("deaths") is None
        assert await store.create("deaths", "1") and not await store.create("deaths", "1")
        assert store.get("deaths").value == 0
        assert store.view("deaths") == "deaths: 0" and store.view("nope") is None
        assert await store.apply_suffix("deaths", "++", is_moderator=True) == "deaths: 1"
        assert await store.apply_suffix("deaths", "--", is_moderator=True) == "deaths: 0"
        assert await store.apply_suffix("deaths", "++", is_moderator=False) is None  # not public, not a mod
        assert await store.set_public("deaths", True)
        assert await store.apply_suffix("deaths", "++", is_moderator=False) == "deaths: 1"
        assert await store.set_value("deaths", 50) and store.get("deaths").value == 50
        assert not await store.set_value("nope", 1)
        assert await store.delete("deaths") and store.get("deaths") is None
        assert await store.apply_suffix("deaths", "++", is_moderator=True) is None

    run(go())


def test_all_sorted_and_reflects_db(make_db):
    async def go():
        db = await make_db()
        store = CounterStore(db)
        await store.create("wins", "1")
        await store.create("deaths", "1")
        await store.load()
        assert [c.name for c in store.all()] == ["deaths", "wins"]

    run(go())
