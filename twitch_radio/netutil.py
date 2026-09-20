from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """True if binding to `host` keeps a listener reachable only from this
    machine: "localhost", or any loopback IP literal (127.0.0.0/8, ::1).
    "0.0.0.0", "::", "" and real hostnames/addresses are not loopback.

    Shared by config.py (startup warnings) and the admin server (deciding
    whether /settings may run without a password), so both always agree on
    what "reachable off this machine" means.
    """
    candidate = host.strip().strip("[]").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False
