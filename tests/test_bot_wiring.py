"""Builds the real TwitchChatBot (no network) and drives it with fake chat
objects — catches command-registration mistakes and end-to-end wiring bugs
that unit tests on the services alone cannot."""

import contextlib
from types import SimpleNamespace

from conftest import run
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.chatfeed import ChatFeed
from twitch_radio.runtime import RuntimeStatus
from twitch_radio.store import JsonStore


async def _bot(tmp_path, make_db, **kw):
    db = await make_db()
    bot = TwitchChatBot(
        client_id="x", client_secret="y", bot_id="100", owner_id="200", prefix="!", public_base_url=None,
        chat_feed=ChatFeed(), tunables_store=JsonStore(tmp_path / "t.json"), specs_store=JsonStore(tmp_path / "s.json"),
        toggles_store=JsonStore(tmp_path / "g.json"), db=db, token_storage_path=tmp_path / "tok.json",
        status=RuntimeStatus(), emote_sources=(), **kw,
    )

    async def _noop():
        return None

    bot._try_subscribe_chat = _noop  # type: ignore[method-assign]
    bot._try_subscribe_alerts = _noop  # type: ignore[method-assign]
    await bot.setup_hook()
    return bot


async def _shutdown(bot):
    for task in (bot._points_task,):
        if task:
            task.cancel()
    for component in list(bot._components.values()) if hasattr(bot, "_components") else []:
        with contextlib.suppress(Exception):
            await component.component_teardown()


def _chatter(uid="1", name="Alice", **roles):
    return SimpleNamespace(id=uid, name=name.lower(), display_name=name, moderator=roles.get("moderator", False),
                           vip=roles.get("vip", False), subscriber=roles.get("subscriber", False))


def test_all_components_register_without_collisions(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            names = set(bot.commands)
            assert {"points", "gamble", "timer", "permit", "addcom", "setlimit", "toggle", "uptime"} <= names
            assert bot.reserved_names >= names  # reserved list covers every real command
        finally:
            await _shutdown(bot)

    run(go())


def test_chat_message_flows_to_feed_economy_and_automod(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            await bot.toggles_store.update(lambda d: {**d, "link_filter_enabled": True, "filter_delete_enabled": True})
            sent, deleted = [], []

            async def fake_announce(text):
                sent.append(text)

            async def fake_delete(message_id):
                deleted.append(message_id)
                return True

            bot.announce = fake_announce  # type: ignore[method-assign]
            bot.delete_message = fake_delete  # type: ignore[method-assign]
            bot.automod._announce = fake_announce
            bot.automod._delete = fake_delete

            def message(text, chatter):
                return SimpleNamespace(chatter=chatter, text=text, fragments=[], id="msg-1")

            await bot._process_chat(message("hello there", _chatter()))
            await bot._process_chat(message("free stuff at spam.com", _chatter("2", "Spammy")))
            await bot._process_chat(message("ignored: it's the bot", _chatter("100", "TheBot")))
            assert bot.chat_messages == 2 and len(bot.chat_feed.snapshot()) == 2
            assert deleted == ["msg-1"] and "links aren't allowed" in sent[0]
            assert set(bot.economy._seen) == {"1", "2"}
            assert bot.status.last_chat_message_at is not None
        finally:
            await _shutdown(bot)

    run(go())


def test_custom_command_runs_through_the_not_found_path(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db, reserved_commands=frozenset({"sr"}))
        try:
            await bot.custom.save("hello", "Hi {user}, {touser}!", "1")
            replies = []

            async def reply(text):
                replies.append(text)

            ctx = SimpleNamespace(content="!hello @Bob", chatter=_chatter(), broadcaster=SimpleNamespace(display_name="Chan"),
                                  reply=reply)
            assert await bot._try_custom_command(ctx, "hello")
            assert replies and replies[0].startswith("Hi Alice, Bob!")
            assert not await bot._try_custom_command(ctx, "nothere")
            assert "sr" in bot.reserved_names  # another bot's command can't be shadowed
        finally:
            await _shutdown(bot)

    run(go())


def test_live_state_events_drive_session_summary(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            said = []

            async def fake_announce(text):
                said.append(text)

            bot.announce = fake_announce  # type: ignore[method-assign]
            await bot.toggles_store.update(lambda d: {**d, "stream_summary_enabled": True})
            await bot.event_stream_online(None)  # type: ignore[arg-type]
            assert bot.status.live.is_live is True
            await bot.economy.on_follow("5", "Newbie")
            await bot.event_stream_offline(None)  # type: ignore[arg-type]
            assert bot.status.live.is_live is False
            assert said and "Newbie" in said[0]
            await bot.event_stream_offline(None)  # duplicate offline event: no second summary
            assert len(said) == 1
        finally:
            await _shutdown(bot)

    run(go())


def test_reply_suffixes_are_configurable(tmp_path, make_db):
    async def go():
        plain = await _bot(tmp_path, make_db, reply_suffixes=())
        try:
            assert plain._decorate("hi") == "hi"
            assert len(plain._decorate("x" * 900)) == 500
        finally:
            await _shutdown(plain)

    run(go())


def test_counter_view_and_increment_through_the_fallback_path(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            await bot.counters.create("deaths", "1")
            replies = []

            async def reply(text):
                replies.append(text)

            ctx = SimpleNamespace(content="!deaths", chatter=_chatter(), reply=reply)
            assert await bot._try_counter_command(ctx, "deaths") and replies[-1].startswith("deaths: 0")

            mod_ctx = SimpleNamespace(content="!deaths++", chatter=_chatter("9", "Mod", moderator=True), reply=reply)
            assert await bot._try_counter_command(mod_ctx, "deaths++") and replies[-1].startswith("deaths: 1")

            viewer_ctx = SimpleNamespace(content="!deaths++", chatter=_chatter(), reply=reply)
            assert not await bot._try_counter_command(viewer_ctx, "deaths++")  # not public, not a mod

            assert not await bot._try_counter_command(ctx, "nosuchcounter")
        finally:
            await _shutdown(bot)

    run(go())


def test_commands_listing_includes_custom_commands_and_counters(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            from twitch_radio.components.info import InfoComponent

            component = InfoComponent(bot)
            replies = []

            async def reply(text):
                replies.append(text)

            ctx = SimpleNamespace(chatter=_chatter(), reply=reply)
            await component.commands_list.callback(component, ctx)
            assert "custom:" not in replies[-1] and "counters:" not in replies[-1]

            await bot.custom.save("hi", "hello", "1")
            await bot.counters.create("deaths", "1")
            await component.commands_list.callback(component, ctx)
            assert "custom: !hi" in replies[-1] and "counters: !deaths" in replies[-1]
        finally:
            await _shutdown(bot)

    run(go())


def test_counter_list_subcommand(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            from twitch_radio.components.counters import CountersComponent

            component = CountersComponent(bot)
            replies = []

            async def reply(text):
                replies.append(text)

            ctx = SimpleNamespace(chatter=_chatter("9", "Mod", moderator=True), reply=reply)
            await component.counter_cmd.callback(component, ctx, args="list")
            assert "No counters yet" in replies[-1]

            await bot.counters.create("wins", "9")
            await bot.counters.create("deaths", "9")
            await component.counter_cmd.callback(component, ctx, args="list")
            assert "deaths" in replies[-1] and "wins" in replies[-1]
        finally:
            await _shutdown(bot)

    run(go())


def test_addcom_and_counter_add_cannot_shadow_each_other(tmp_path, make_db):
    async def go():
        bot = await _bot(tmp_path, make_db)
        try:
            await bot.custom.save("hi", "hello", "1")
            from twitch_radio.components.counters import CountersComponent

            component = CountersComponent(bot)
            replies = []

            async def reply(text):
                replies.append(text)

            ctx = SimpleNamespace(chatter=_chatter("9", "Mod", moderator=True), reply=reply)
            await component.counter_cmd.callback(component, ctx, args="add hi")
            assert "already a command" in replies[-1]
            assert bot.counters.get("hi") is None
        finally:
            await _shutdown(bot)

    run(go())
