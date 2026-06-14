"""Cooperative + hard cancellation for running sessions.

A background deliberation runs in a worker thread and reloads its session as a
fresh object, so a flag set by the API on a different copy wouldn't be seen.
This shared, thread-safe registry bridges them:

- `request(session_id)` flags the session AND kills any in-flight CLI subprocess
  registered for it — so cancel is near-instant, not just at the next checkpoint.
- The loop checks `is_requested` at every agent call (a clean abort point).
- The CLI adapter registers/unregisters its subprocess per session (via the
  worker thread's `current_session`) so `request` can terminate it.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_requested: set[str] = set()
_procs: dict[str, set] = {}      # session_id -> set of live subprocess.Popen
_tls = threading.local()         # the worker thread's current session id


class SessionCancelled(Exception):
    """The human asked to cancel this session mid-run."""


# --- worker-thread context (so the adapter knows which session it's serving) --
def set_current_session(session_id: str | None) -> None:
    _tls.sid = session_id


def current_session() -> str | None:
    return getattr(_tls, "sid", None)


# --- subprocess registry ------------------------------------------------------
def register_proc(session_id: str | None, proc) -> None:
    if not session_id:
        return
    with _lock:
        _procs.setdefault(session_id, set()).add(proc)


def unregister_proc(session_id: str | None, proc) -> None:
    if not session_id:
        return
    with _lock:
        procs = _procs.get(session_id)
        if procs:
            procs.discard(proc)
            if not procs:
                _procs.pop(session_id, None)


def _kill_procs(session_id: str) -> None:
    with _lock:
        procs = list(_procs.get(session_id, ()))
    for p in procs:
        try:
            p.kill()
        except Exception:  # noqa: BLE001 — already dead / not killable: ignore
            pass


# --- cancellation flag --------------------------------------------------------
def request(session_id: str) -> None:
    with _lock:
        _requested.add(session_id)
    _kill_procs(session_id)  # terminate any in-flight subprocess immediately


def is_requested(session_id: str) -> bool:
    with _lock:
        return session_id in _requested


def clear(session_id: str) -> None:
    with _lock:
        _requested.discard(session_id)
        _procs.pop(session_id, None)
