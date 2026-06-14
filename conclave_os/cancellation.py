"""Cooperative cancellation registry.

A background deliberation runs in a worker thread and reloads its session as a
fresh object, so a flag set by the API on a different copy wouldn't be seen.
This shared, thread-safe set bridges them: the API requests cancellation by
session id; the loop checks it at each agent call and aborts between steps.

Cancellation is cooperative — an in-flight CLI subprocess finishes first, so a
cancel takes effect at the next checkpoint, not instantly.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_requested: set[str] = set()


def request(session_id: str) -> None:
    with _lock:
        _requested.add(session_id)


def is_requested(session_id: str) -> bool:
    with _lock:
        return session_id in _requested


def clear(session_id: str) -> None:
    with _lock:
        _requested.discard(session_id)
