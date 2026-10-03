"""Chat moderation: applies the pure rules in filters.py to a ChatEvent and
carries out the response — a rate-limited public warning, a delete, and (after
repeated strikes) a timeout. The Twitch calls are injected, so the whole
policy is testable with fakes."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from twitch_radio.chatevent import ChatEvent
from twitch_radio.cooldown import CooldownTracker
from twitch_radio.db import Database
from twitch_radio.filters import (
    REASON_CAPS,
    REASON_LINK,
    REASON_TERM,
    PermitBook,
    StrikeTracker,
    TermMatcher,
    Violation,
    evaluate,
    normalize_domain,
)
from twitch_radio.runtime import RuntimeStatus
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

log = logging.getLogger(__name__)

KIND_TERM = "term"
KIND_DOMAIN = "domain"

Announce = Callable[[str], Awaitable[None]]
DeleteMessage = Callable[[str], Awaitable[bool]]
TimeoutUser = Callable[[str, int, str], Awaitable[bool]]


class AutoMod:
    def __init__(
        self,
        db: Database,
        tunables_store: JsonStore,
        toggles_store: JsonStore,
        status: RuntimeStatus,
        *,
        announce: Announce,
        delete_message: DeleteMessage,
        timeout_user: TimeoutUser,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._tunables_store = tunables_store
        self._toggles_store = toggles_store
        self._status = status
        self._announce = announce
        self._delete = delete_message
        self._timeout = timeout_user
        self.matcher = TermMatcher()
        self.allowed_domains: list[str] = []
        self.strikes = StrikeTracker(clock)
        self.permits = PermitBook(clock)
        self._warned = CooldownTracker(clock)

    async def load(self) -> None:
        self.matcher.set_terms(await self._db.list_filter_values(KIND_TERM))
        self.allowed_domains = await self._db.list_filter_values(KIND_DOMAIN)

    # -- list management (chat commands) ----------------------------------

    async def add_term(self, term: str, by: str) -> bool:
        added = await self._db.add_filter_value(KIND_TERM, term.strip().lower(), by)
        await self.load()
        return added

    async def remove_term(self, term: str) -> bool:
        removed = await self._db.remove_filter_value(KIND_TERM, term.strip().lower())
        await self.load()
        return removed

    async def add_domain(self, domain: str, by: str) -> str | None:
        host = normalize_domain(domain)
        if "." not in host:
            return None
        await self._db.add_filter_value(KIND_DOMAIN, host, by)
        await self.load()
        return host

    async def remove_domain(self, domain: str) -> bool:
        removed = await self._db.remove_filter_value(KIND_DOMAIN, normalize_domain(domain))
        await self.load()
        return removed

    def permit(self, login: str, seconds: int) -> None:
        self.permits.grant(login, seconds)

    # -- the policy -------------------------------------------------------

    async def inspect(self, event: ChatEvent) -> Violation | None:
        if event.is_moderator or not event.text:  # mods and the broadcaster are never filtered
            return None
        toggles = FeatureToggles.from_dict(await self._toggles_store.read())
        if not (toggles.link_filter_enabled or toggles.caps_filter_enabled or toggles.term_filter_enabled):
            return None
        if (toggles.filter_exempt_vips and event.is_vip) or (toggles.filter_exempt_subs and event.is_subscriber):
            return None
        tunables = TwitchTunables.from_dict(await self._tunables_store.read())
        violation = evaluate(
            event.text,
            fragments=event.fragments,
            link_on=toggles.link_filter_enabled,
            caps_on=toggles.caps_filter_enabled,
            term_on=toggles.term_filter_enabled,
            caps_threshold_percent=tunables.caps_threshold_percent,
            allowed_domains=self.allowed_domains,
            matcher=self.matcher,
            may_post_links=self.permits.active(event.login),
        )
        if violation is None:
            return None

        detail = f" ({violation.detail})" if violation.detail else ""
        log.info("AutoMod: %s violation by %s (%s)%s.", violation.reason, event.name, event.user_id, detail)
        strikes = self.strikes.record(event.user_id, tunables.filter_strike_window_seconds)
        missing = self._status.scopes_missing
        if toggles.filter_delete_enabled and "delete" not in missing and event.message_id:
            await self._delete(event.message_id)
        can_timeout = toggles.filter_timeout_enabled and "timeout" not in self._status.scopes_missing
        if can_timeout and strikes >= tunables.filter_strikes_before_timeout:
            if await self._timeout(event.user_id, tunables.filter_timeout_seconds, f"Chat filter ({violation.reason})"):
                self.strikes.clear(event.user_id)
                await self._announce(f"@{event.name} timed out for {tunables.filter_timeout_seconds}s.")
                return violation
        if self._warned.remaining(event.user_id, tunables.filter_warning_cooldown_seconds) == 0:
            self._warned.mark(event.user_id)
            warning = self._warning(event.name, violation)
            if can_timeout and strikes == tunables.filter_strikes_before_timeout - 1:
                warning += " Next one is a timeout."
            await self._announce(warning)
        return violation

    @staticmethod
    def _warning(name: str, violation: Violation) -> str:
        if violation.reason == REASON_TERM:
            return f"@{name} that word isn't allowed here."
        if violation.reason == REASON_LINK:
            return f"@{name} links aren't allowed — ask a mod for a !permit."
        if violation.reason == REASON_CAPS:
            return f"@{name} easy on the caps!"
        return f"@{name} please follow the chat rules."
