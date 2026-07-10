"""Network-surface guardrails for the local dashboard.

Gang of 8 is a single-user desktop service. Its API intentionally includes
local-machine capabilities, so exposing it on a LAN by accident is unsafe.
Remote serving is an explicit operator decision, never a side effect of
passing a different bind host.
"""

from __future__ import annotations

import ipaddress
import os

from . import config


def remote_access_allowed() -> bool:
    """Whether an operator explicitly opted in to serving non-local clients."""
    return os.environ.get(config.ALLOW_REMOTE_ENV, "").strip().lower() in {"1", "true", "yes"}


def is_loopback_host(host: str | None) -> bool:
    """Return True only for loopback IPs and their conventional hostnames."""
    value = (host or "").strip().strip("[]").lower()
    if value in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str) -> None:
    """Reject an unsafe bind unless remote access was deliberately enabled."""
    if remote_access_allowed() or is_loopback_host(host):
        return
    raise ValueError(
        f"refusing to bind Gang of 8 to non-local host {host!r}; "
        f"set {config.ALLOW_REMOTE_ENV}=1 only behind authenticated access"
    )


def local_request_allowed(client_host: str | None) -> bool:
    """Allow only loopback requests by default.

    Starlette's TestClient identifies itself as ``testclient`` rather than an
    IP address; treating that synthetic host as local keeps the guard testable
    without weakening any real network path.
    """
    if remote_access_allowed():
        return True
    return client_host in {None, "testclient"} or is_loopback_host(client_host)


def sensitive_local_action_allowed(client_host: str | None) -> bool:
    """Key reveal and OS file opening remain local even in remote-enabled mode."""
    return client_host in {None, "testclient"} or is_loopback_host(client_host)
