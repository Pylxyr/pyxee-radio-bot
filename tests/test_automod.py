from conftest import run
from twitch_radio.automod import AutoMod
from twitch_radio.chatevent import ChatEvent
from twitch_radio.runtime import RuntimeStatus


class Harness:
    def __init__(self, db, stores):
        self.said, self.deleted, self.timeouts = [], [], []
        self.now = [0.0]
        self.status = RuntimeStatus()

        async def announce(text):
            self.said.append(text)

        async def delete(message_id):
            self.deleted.append(message_id)
            return True

        async def timeout(user_id, seconds, reason):
            self.timeouts.append((user_id, seconds))
            return True

        self.stores = stores
        self.mod = AutoMod(
            db, stores[0], stores[1], self.status,
            announce=announce, delete_message=delete, timeout_user=timeout, clock=lambda: self.now[0],
        )

    async def toggles(self, **flags):
        await self.stores[1].update(lambda d: {**d, **flags})

    async def tunables(self, **values):
        await self.stores[0].update(lambda d: {**d, **values})


def _msg(text, uid="7", name="Spammer", **kw):
    return ChatEvent(user_id=uid, login=name.lower(), name=name, text=text, message_id="m1", **kw)


def test_nothing_happens_when_all_filters_are_off(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        assert await h.mod.inspect(_msg("visit spam.com")) is None
        assert h.said == []

    run(go())


def test_link_warning_delete_and_exemptions(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        await h.toggles(link_filter_enabled=True, filter_delete_enabled=True)
        assert (await h.mod.inspect(_msg("visit spam.com"))).reason == "link"
        assert h.deleted == ["m1"] and "links aren't allowed" in h.said[0]
        assert await h.mod.inspect(_msg("visit spam.com", is_moderator=True)) is None
        assert await h.mod.inspect(_msg("visit spam.com", is_vip=True)) is None  # VIPs exempt by default
        await h.toggles(filter_exempt_vips=False, filter_exempt_subs=True)
        assert await h.mod.inspect(_msg("visit spam.com", is_subscriber=True)) is None
        assert await h.mod.inspect(_msg("visit spam.com", uid="8", is_vip=True)) is not None

    run(go())


def test_permit_and_allowlist(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        await h.toggles(link_filter_enabled=True)
        h.mod.permit("spammer", 60)
        assert await h.mod.inspect(_msg("visit spam.com")) is None
        h.now[0] = 61
        assert await h.mod.inspect(_msg("visit spam.com")) is not None
        assert await h.mod.add_domain("https://www.friendly.gg/x", "1") == "friendly.gg"
        assert await h.mod.add_domain("nodots", "1") is None
        assert await h.mod.inspect(_msg("see friendly.gg/a", uid="9")) is None

    run(go())


def test_warning_cooldown_limits_chat_spam(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        await h.toggles(caps_filter_enabled=True)
        for _ in range(5):
            assert await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW")) is not None
        assert len(h.said) == 1  # one warning, not five
        h.now[0] = 31
        await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW"))
        assert len(h.said) == 2

    run(go())


def test_repeat_offender_is_timed_out(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        await h.toggles(caps_filter_enabled=True, filter_timeout_enabled=True)
        await h.tunables(filter_strikes_before_timeout=3, filter_warning_cooldown_seconds=0)
        await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW"))
        await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW"))
        assert "Next one is a timeout" in h.said[-1] and h.timeouts == []
        await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW"))
        assert h.timeouts == [("7", 60)] and "timed out" in h.said[-1]
        await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW"))  # strikes were reset
        assert len(h.timeouts) == 1

    run(go())


def test_missing_scopes_disable_actions(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        await h.toggles(caps_filter_enabled=True, filter_delete_enabled=True, filter_timeout_enabled=True)
        await h.tunables(filter_strikes_before_timeout=1)
        h.status.scopes_missing.update({"delete", "timeout"})
        await h.mod.inspect(_msg("STOP SHOUTING RIGHT NOW"))
        assert h.deleted == [] and h.timeouts == [] and len(h.said) == 1

    run(go())


def test_blocked_terms_via_commands_api(make_db, stores):
    async def go():
        h = Harness(await make_db(), stores)
        await h.toggles(term_filter_enabled=True)
        assert await h.mod.add_term("Heck", "1") and not await h.mod.add_term("heck", "1")
        assert (await h.mod.inspect(_msg("oh HECK no"))).reason == "term"
        assert await h.mod.remove_term("heck")
        assert await h.mod.inspect(_msg("oh heck no", uid="9")) is None

    run(go())
