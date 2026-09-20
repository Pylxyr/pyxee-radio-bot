"""HTTP surface of the bot: audio stream, OBS overlays, the public commands
page and the password-gated /settings page.

Layout:
    app.py        builds the aiohttp app (routes, middleware, TLS) — the only
                  entry point, `run_admin_server`, lives here
    context.py    the shared dependencies every handler reads
    handlers/     request handlers, grouped by concern (live data, media, settings)
    render/       pure functions that build the /settings and /commands HTML
    security.py   credential, CSRF, rate-limit and URL-allowlist rules (stdlib only)
    assets.py     loads static/ (overlay pages, CSS, JS, HTML templates) and the logo
    static/       the front-end files themselves

Nothing is imported here on purpose: `from twitch_radio.admin.app import
run_admin_server` pulls in aiohttp, while security.py and render/ stay
importable without it.
"""
