"""Per-seat health: classify adapter failures and remember seat state.

A seat outage is not a session bug. A real build watched claude's CLI hit
its monthly spend limit mid-authoring: the run recovered (a helper seat
authored the file), but the operator saw a failed session and a goal paused
with "no file was delivered" — the truth lived one level down in a single
session's unresolved list, and later scheduling kept burning attempts
against a seat that could not possibly answer. This module gives the
service one shared, thread-safe answer to "can this seat answer right
now?", fed by every adapter success/failure that passes through the
registry, and consulted by scheduling (owner assignment, escalation
targets, release verifier pool) and by the dashboard's seat badges.

States:
- healthy       — last call completed (or nothing known against the seat)
- degraded      — transient trouble (capacity, timeout, generic error);
                  the seat stays schedulable, retries remain sensible
- quota_exhausted / auth_expired / offline — hard-unavailable: no retry
  can succeed until a human (or the provider's clock) intervenes; the
  scheduler must route around these instead of burning attempts
"""

from __future__ import annotations

import re
import threading

from .models import utcnow

# Hard-unavailable states: retrying cannot help until something external
# changes (a human raises the limit, re-authenticates, or installs the CLI).
UNAVAILABLE_STATES = ("quota_exhausted", "auth_expired", "offline")

_CLASSIFIERS: tuple[tuple[str, re.Pattern], ...] = (
    ("quota_exhausted", re.compile(
        r"spend limit|usage limit|quota exceeded|out of credits|"
        r"insufficient[_ ]quota|billing", re.IGNORECASE)),
    ("auth_expired", re.compile(
        r"not logged in|login required|unauthorized|authentication|"
        r"invalid api key|expired.*(?:token|credential)|\b401\b", re.IGNORECASE)),
    ("offline", re.compile(
        r"not found on PATH|not runnable|No such file or directory.*CLI|"
        r"command not found", re.IGNORECASE)),
    ("capacity", re.compile(
        r"at capacity|overloaded|rate limit|too many requests|"
        r"\b429\b|\b503\b|\b529\b", re.IGNORECASE)),
    ("timeout", re.compile(r"timed out|timeout", re.IGNORECASE)),
)


def classify_failure(error_text: str) -> str:
    """Map an adapter error message to a seat state. Unknown -> 'degraded'."""
    text = str(error_text or "")
    for state, pattern in _CLASSIFIERS:
        if pattern.search(text):
            return state
    return "degraded"


class SeatHealth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seats: dict[str, dict] = {}

    def record_failure(self, seat: str, error_text: str) -> str:
        """Classify and remember a failure; returns the classified state."""
        state = classify_failure(error_text)
        with self._lock:
            entry = self._seats.setdefault(seat, {})
            if entry.get("state") != state:
                entry["since"] = utcnow()
            entry["state"] = state
            entry["reason"] = str(error_text or "")[:300]
            entry["failures"] = entry.get("failures", 0) + 1
        return state

    def record_success(self, seat: str) -> None:
        with self._lock:
            entry = self._seats.setdefault(seat, {})
            if entry.get("state") != "healthy":
                entry["since"] = utcnow()
            entry["state"] = "healthy"
            entry["reason"] = ""
            entry["last_ok"] = utcnow()

    def state(self, seat: str) -> str:
        with self._lock:
            return (self._seats.get(seat) or {}).get("state", "healthy")

    def reason(self, seat: str) -> str:
        with self._lock:
            return (self._seats.get(seat) or {}).get("reason", "")

    def is_unavailable(self, seat: str) -> bool:
        """True only for hard-unavailable states — the ones where another
        attempt is guaranteed wasted. Transient trouble stays schedulable."""
        return self.state(seat) in UNAVAILABLE_STATES

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                seat: dict(entry) for seat, entry in sorted(self._seats.items())
            }
