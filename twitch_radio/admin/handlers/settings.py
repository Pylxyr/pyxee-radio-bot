"""The password-gated routes: /settings (view and save) and /blocklist.json."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from twitch_radio.admin.context import AdminContext, get_ctx
from twitch_radio.admin.handlers.auth import authorize, origin_ok, protect
from twitch_radio.admin.render.settings_page import FORM_MARKER, LiveStatus, render_settings_page
from twitch_radio.blocklist import clean_list
from twitch_radio.blocklist import counts as blocklist_counts
from twitch_radio.specs import PC_SPEC_FIELDS, PERIPHERAL_FIELDS, PCSpecs, Peripherals
from twitch_radio.toggles import TOGGLE_KEYS, FeatureToggles
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value: str) -> bool:
    """For a toggle named explicitly in a partial (non-browser) POST. A bare
    HTML checkbox submits the literal string "on", so that has to count as
    true, but an explicit `alerts_enabled=false` from a script should mean
    what it says rather than "present, therefore on"."""
    return value.strip().lower() in _TRUTHY


def _parse_tunables(form: Mapping[str, Any]) -> tuple[dict[str, int], list[str]]:
    """Validate every submitted tunable against its bounds. Fields absent from
    the form are simply not part of the update."""
    submitted: dict[str, int] = {}
    errors: list[str] = []
    for name, (lo, hi) in TUNABLE_BOUNDS.items():
        raw = form.get(name)
        if raw is None:
            continue
        try:
            value = int(str(raw))
        except ValueError:
            errors.append(f"{name}: not a number")
            continue
        if value < lo or value > hi:
            errors.append(f"{name}: must be between {lo} and {hi}")
            continue
        submitted[name] = value
    return submitted, errors


def _live_status(ctx: AdminContext) -> LiveStatus:
    np = ctx.player.now_playing
    return LiveStatus(
        state=ctx.player.state.value,
        queue_size=ctx.player.queue_size(),
        uptime_seconds=int(time.monotonic() - ctx.started_at),
        now_playing_title=np.title if np is not None else None,
    )


async def _community_snapshot(ctx: AdminContext) -> dict[str, Any]:
    """Read-only dashboard data — management itself stays in chat (!addcom,
    etc.); this is just visibility so a mod doesn't need a second tool."""
    return {
        "top_points": await ctx.db.top_points(limit=5),
        "custom_commands": sorted(await ctx.db.list_commands()),
    }


async def _page_response(
    ctx: AdminContext,
    *,
    tunables: TwitchTunables,
    specs_data: dict[str, Any],
    toggles: FeatureToggles,
    message: str | None = None,
    error: bool = False,
    status: int = 200,
) -> web.Response:
    html = render_settings_page(
        tunables=tunables,
        pc_specs=PCSpecs.from_dict(specs_data),
        peripherals=Peripherals.from_dict(specs_data),
        toggles=toggles,
        community=await _community_snapshot(ctx),
        broadcast_info=ctx.broadcast_info,
        status=_live_status(ctx),
        has_logo=ctx.logo is not None,
        message=message,
        error=error,
    )
    return protect(web.Response(text=html, content_type="text/html", status=status))


async def handle_settings_get(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    denied = authorize(ctx, request)
    if denied is not None:
        return denied
    return await _page_response(
        ctx,
        tunables=TwitchTunables.from_dict(await ctx.tunables_store.read()),
        specs_data=await ctx.specs_store.read(),
        toggles=FeatureToggles.from_dict(await ctx.toggles_store.read()),
    )


async def handle_settings_post(request: web.Request) -> web.Response:
    ctx = get_ctx(request)
    denied = authorize(ctx, request)
    if denied is not None:
        return denied
    if not origin_ok(request):
        return protect(web.Response(status=403, text="Origin check failed — refusing to save."))
    form = await request.post()

    # Validate every tunable BEFORE writing anything anywhere, so one
    # out-of-range number can't leave the three stores disagreeing about what
    # the operator just submitted (or answer 400 for a request that changed things).
    submitted, errors = _parse_tunables(form)
    if errors:
        current = TwitchTunables.from_dict(await ctx.tunables_store.read())
        return await _page_response(
            ctx,
            tunables=TwitchTunables.from_dict({**current.to_dict(), **submitted}),
            specs_data=await ctx.specs_store.read(),
            toggles=FeatureToggles.from_dict(await ctx.toggles_store.read()),
            message="Nothing was saved — " + "; ".join(errors),
            error=True,
            status=400,
        )

    def _mutate_tunables(current: dict[str, Any]) -> dict[str, Any]:
        return {**TwitchTunables.from_dict(current).to_dict(), **submitted}

    def _mutate_specs(current: dict[str, Any]) -> dict[str, Any]:
        updated = dict(current)
        for field, _label in (*PC_SPEC_FIELDS, *PERIPHERAL_FIELDS):
            raw = form.get(field)
            if raw is not None:
                updated[field] = str(raw)
        # Routed through from_dict()/to_dict() so the strip + length clamp
        # lives in one place (specs.py). Free-text fields have no failure mode
        # the way tunable bounds do, so nothing here can fail.
        return {**PCSpecs.from_dict(updated).to_dict(), **Peripherals.from_dict(updated).to_dict()}

    # Absent-means-unchecked is right for a browser submitting this page's own
    # form and catastrophic for anything else: origin_ok deliberately lets
    # non-browser callers (curl, a Stream Deck script) through, and one of
    # those POSTing just `queue_cap=100` would silently switch off every
    # feature whose checkbox wasn't in its body. FORM_MARKER is a hidden field
    # only this page's form carries, so checkbox semantics apply exactly where
    # they're meant to and a partial POST updates only the toggles it names.
    full_form = form.get(FORM_MARKER) is not None

    def _mutate_toggles(current: dict[str, Any]) -> dict[str, Any]:
        toggles = FeatureToggles.from_dict(current)
        for key in TOGGLE_KEYS:
            present = form.get(key) is not None
            if full_form:
                setattr(toggles, key, present)
            elif present:
                setattr(toggles, key, _is_truthy(str(form.get(key))))
        return toggles.to_dict()

    tunables_result = await ctx.tunables_store.update(_mutate_tunables)
    specs_result = await ctx.specs_store.update(_mutate_specs)
    toggles_result = await ctx.toggles_store.update(_mutate_toggles)
    log.info(
        "Settings updated via /settings from %s: tunables=%s specs=%s toggles=%s",
        request.remote,
        tunables_result,
        specs_result,
        toggles_result,
    )
    return await _page_response(
        ctx,
        tunables=TwitchTunables.from_dict(tunables_result),
        specs_data=specs_result,
        toggles=FeatureToggles.from_dict(toggles_result),
        message="Saved.",
    )


async def handle_blocklist(request: web.Request) -> web.Response:
    """Full blocklist contents, gated like /settings — !blocklist in chat only
    gives counts, so this is where a mod actually audits what's blocked."""
    ctx = get_ctx(request)
    denied = authorize(ctx, request)
    if denied is not None:
        return denied
    data = await ctx.blocklist_store.read()
    tracks, uploaders = blocklist_counts(data)
    return protect(
        web.json_response(
            {
                "tracks": clean_list(data.get("tracks")),
                "uploaders": clean_list(data.get("uploaders")),
                "track_count": tracks,
                "uploader_count": uploaders,
            }
        )
    )
