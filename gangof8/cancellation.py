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
_cancelers: dict[str, set] = {}  # session_id -> set of zero-arg abort callbacks
_progressers: dict[tuple[str, str], object] = {}  # (session, call) -> progress callback
_tls = threading.local()         # the worker thread's current session id


class SessionCancelled(Exception):
    """The human asked to cancel this session mid-run."""


# --- worker-thread context (so the adapter knows which session it's serving) --
def set_current_session(session_id: str | None) -> None:
    _tls.sid = session_id


def current_session() -> str | None:
    return getattr(_tls, "sid", None)


def set_current_call(call_id: str | None) -> None:
    """Bind an adapter invocation to its persisted call record.

    Adapters intentionally do not know about Session or LogStore.  This small
    thread-local bridge lets a streaming adapter report real output progress to
    the coordinator without coupling the adapter layer to persistence.
    """
    _tls.call_id = call_id


def current_call() -> str | None:
    return getattr(_tls, "call_id", None)


def set_call_kind(kind: str | None) -> None:
    _tls.call_kind = kind


def current_call_kind() -> str | None:
    return getattr(_tls, "call_kind", None)


def register_progress(session_id: str | None, call_id: str | None, fn) -> None:
    if not session_id or not call_id:
        return
    with _lock:
        _progressers[(session_id, call_id)] = fn


def unregister_progress(session_id: str | None, call_id: str | None) -> None:
    if not session_id or not call_id:
        return
    with _lock:
        _progressers.pop((session_id, call_id), None)


def report_progress(chars: int = 0, detail: str = "output") -> None:
    """Report meaningful adapter progress for the current call, if registered.

    Network keep-alives never call this function.  Only parsed model output (or
    an equivalent adapter-level milestone) refreshes the progress timestamp.
    """
    sid, call_id = current_session(), current_call()
    if not sid or not call_id:
        return
    with _lock:
        fn = _progressers.get((sid, call_id))
    if fn is not None:
        try:
            fn(max(0, int(chars)), str(detail or "output"))
        except Exception:  # noqa: BLE001 - progress reporting must never break a call
            pass


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


# --- abort-callback registry --------------------------------------------------
# For in-flight work with no killable subprocess (e.g. an OpenRouter HTTP call):
# the adapter registers a zero-arg callback that tears down its network client,
# so `request` can interrupt the blocking call the same way it kills a CLI proc.
def register_canceler(session_id: str | None, fn) -> None:
    if not session_id:
        return
    with _lock:
        _cancelers.setdefault(session_id, set()).add(fn)


def unregister_canceler(session_id: str | None, fn) -> None:
    if not session_id:
        return
    with _lock:
        fns = _cancelers.get(session_id)
        if fns:
            fns.discard(fn)
            if not fns:
                _cancelers.pop(session_id, None)


def _run_cancelers(session_id: str) -> None:
    with _lock:
        fns = list(_cancelers.get(session_id, ()))
    for fn in fns:
        try:
            fn()
        except Exception:  # noqa: BLE001 — best-effort teardown: ignore
            pass


# --- cancellation flag --------------------------------------------------------
def request(session_id: str) -> None:
    with _lock:
        _requested.add(session_id)
    _kill_procs(session_id)      # terminate any in-flight subprocess immediately
    _run_cancelers(session_id)   # and tear down any in-flight network client


def is_requested(session_id: str) -> bool:
    with _lock:
        return session_id in _requested


def clear(session_id: str) -> None:
    with _lock:
        _requested.discard(session_id)
        _procs.pop(session_id, None)
        _cancelers.pop(session_id, None)
        for key in [key for key in _progressers if key[0] == session_id]:
            _progressers.pop(key, None)
