from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from twitchio.ext import commands

from twitch_radio.blocklist import (
    add_track_block,
    add_uploader_block,
    counts as blocklist_counts,
    looks_like_a_single_track,
    normalize_track_key,
    remove_track_block,
    remove_uploader_block,
)
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

USAGE = {
    "setlimit": "Usage: !setlimit <key> <value> — keys: " + ", ".join(TUNABLE_BOUNDS),
    "block": "Usage: !block <YouTube/SoundCloud URL, or an uploader name>",
    "unblock": "Usage: !unblock <YouTube/SoundCloud URL, or an uploader name>",
    "toggle": "Usage: !toggle <key> [on|off] — keys: " + ", ".join(TOGGLE_KEYS),
}

# Every @commands.is_moderator() below also admits the broadcaster —
# repeated here per-component since each mod-only command relies on it.


class ModerationComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.is_moderator()
    @commands.command(name="setlimit")
    async def set_limit(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: adjust one request-limit tunable live, without needing
        the /settings page — e.g. !setlimit queue_cap 100. Same keys and
        ranges as /settings; takes effect on the very next command."""
        args = args.strip()
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            keys = ", ".join(TUNABLE_BOUNDS)
            await self.bot.safe_reply(ctx, f"Usage: !setlimit <key> <value> — keys: {keys}")
            return
        key, raw_value = parts[0].strip(), parts[1].strip()
        bounds = TUNABLE_BOUNDS.get(key)
        if bounds is None:
            keys = ", ".join(TUNABLE_BOUNDS)
            await self.bot.safe_reply(ctx, f"Unknown key {key!r} — keys: {keys}")
            return
        lo, hi = bounds
        try:
            value = int(raw_value)
        except ValueError:
            await self.bot.safe_reply(ctx, f"{key}: not a number.")
            return
        if not (lo <= value <= hi):
            await self.bot.safe_reply(ctx, f"{key}: must be between {lo} and {hi}.")
            return

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            updated: dict[str, object] = dict(TwitchTunables.from_dict(current).to_dict())
            updated[key] = value
            return updated

        await self.bot.tunables_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, value, ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"{key} = {value}")

    @commands.is_moderator()
    @commands.command(name="toggle")
    async def toggle(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: flips a feature on/off — e.g. !toggle radio_autoplay_enabled off.
        (!radio on/off is a shortcut for the radio_autoplay_enabled key specifically.)"""
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            await self.bot.safe_reply(ctx, USAGE["toggle"])
            return
        key, value = parts[0].strip(), parts[1].lower() == "on"
        if key not in TOGGLE_KEYS:
            await self.bot.safe_reply(ctx, f"Unknown key {key!r} — keys: {', '.join(TOGGLE_KEYS)}")
            return

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            setattr(toggles, key, value)
            return toggles.to_dict()

        await self.bot.toggles_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, value, ctx.chatter.display_name, ctx.chatter.id)
        await self.bot.safe_reply(ctx, f"{key} = {'on' if value else 'off'}")

    @commands.is_moderator()
    @commands.command(name="block")
    async def block(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: blocks a track (URL) or uploader (name) from being
        requested again, and pulls any already-queued copy out too —
        QueuedRequest already carries the uploader, so no re-resolution
        is needed to match on it."""
        target = args.strip()
        if not target:
            await self.bot.safe_reply(ctx, USAGE["block"])
            return
        key = normalize_track_key(target)

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            return add_track_block(current, key) if key else add_uploader_block(current, target)

        result = await self.bot.blocklist_store.update(_mutate)
        log.info("Blocked %r via chat by %s (%s)", target, ctx.chatter.display_name, ctx.chatter.id)
        tracks, uploaders = blocklist_counts(result)
        kind = "track" if key else f"uploader {target!r}"
        reply = f"Blocked that {kind}. ({tracks} tracks, {uploaders} uploaders blocked)"
        if target.lower().startswith(("http://", "https://")) and not looks_like_a_single_track(target, key):
            reply += " (Doesn't look like a single track link, so this won't match anything by URL.)"

        if key:
            purged = self.bot.player.purge_pending(lambda r: normalize_track_key(r.webpage_url) == key)
        else:
            # Uploader block. Matches the same way blocklist_reason() does
            # (case-folded, stripped) so a request that would now be
            # rejected at !sr time doesn't sit in the queue and play anyway.
            wanted = target.strip().lower()
            purged = self.bot.player.purge_pending(lambda r: r.uploader.strip().lower() == wanted)
        if purged:
            noun = "request" if len(purged) == 1 else "requests"
            reply += f" Also removed {len(purged)} already-queued {noun}."

        await self.bot.safe_reply(ctx, reply)

    @commands.is_moderator()
    @commands.command(name="unblock")
    async def unblock(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: reverses !block for a track (by URL) or uploader (by
        name)."""
        target = args.strip()
        if not target:
            await self.bot.safe_reply(ctx, USAGE["unblock"])
            return
        key = normalize_track_key(target)

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            return remove_track_block(current, key) if key else remove_uploader_block(current, target)

        result = await self.bot.blocklist_store.update(_mutate)
        log.info("Unblocked %r via chat by %s (%s)", target, ctx.chatter.display_name, ctx.chatter.id)
        tracks, uploaders = blocklist_counts(result)
        await self.bot.safe_reply(ctx, f"Unblocked. ({tracks} tracks, {uploaders} uploaders still blocked)")

    @commands.is_moderator()
    @commands.command(name="blocklist")
    async def blocklist_cmd(self, ctx: commands.Context) -> None:
        """Mod-only: shows how many tracks/uploaders are currently blocked
        (not the full list — that can get long for chat)."""
        tracks, uploaders = blocklist_counts(await self.bot.blocklist_store.read())
        await self.bot.safe_reply(ctx, f"{tracks} track(s) and {uploaders} uploader(s) currently blocked.")

    @commands.is_moderator()
    @commands.command(name="clearqueue")
    async def clear_queue(self, ctx: commands.Context) -> None:
        """Mod-only: empties the queue. Doesn't touch whatever's currently
        playing — use !skip for that."""
        removed = self.bot.player.purge_pending(lambda r: True)
        if not removed:
            await self.bot.safe_reply(ctx, "Queue's already empty.")
            return
        await self.bot.safe_reply(ctx, f"Cleared {len(removed)} queued request(s).")
