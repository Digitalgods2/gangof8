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

import os
import signal
import subprocess
import threading

_lock = threading.Lock()
_requested: set[str] = set()
_procs: dict[str, set] = {}      # session_id -> set of live subprocess.Popen
_call_procs: dict[tuple[str, str], set] = {}
_cancelers: dict[str, set] = {}  # session_id -> set of zero-arg abort callbacks
_call_cancelers: dict[tuple[str, str], set] = {}
_call_requested: set[tuple[str, str]] = set()
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


def report_progress(chars: int = 0, detail: str = "output", tail: str = "") -> None:
    """Report meaningful adapter progress for the current call, if registered.

    Network keep-alives never call this function.  Only parsed model output (or
    an equivalent adapter-level milestone) refreshes the progress timestamp.
    ``tail`` optionally carries the last few hundred characters the model has
    produced so the dashboard can show what is being written, live.
    """
    sid, call_id = current_session(), current_call()
    if not sid or not call_id:
        return
    with _lock:
        fn = _progressers.get((sid, call_id))
    if fn is not None:
        try:
            fn(max(0, int(chars)), str(detail or "output"), str(tail or ""))
        except Exception:  # noqa: BLE001 - progress reporting must never break a call
            pass


def kill_tree(proc) -> None:
    """Kill a CLI seat's WHOLE process tree, not just the process we spawned.

    Every local CLI seat shells out further: the seat's launcher spawns a
    runtime which spawns the actual agent binary, four levels deep in one
    observed run. Popen.kill() terminates only the direct child, so a
    cancelled or timed-out call left the real agent alive — still running, and
    still able to write files with the user's privileges long after the
    coordinator considered the call over. Applies to whichever seats are
    enabled; nothing here is vendor-specific.

    Best-effort and never raises: the tree kill is attempted first, and the
    direct kill always runs as the fallback."""
    pid = getattr(proc, "pid", None)
    if pid and os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    elif pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, AttributeError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — already gone is success
        pass


# --- subprocess registry ------------------------------------------------------
def register_proc(session_id: str | None, proc) -> None:
    if not session_id:
        return
    call_id = current_call()
    with _lock:
        _procs.setdefault(session_id, set()).add(proc)
        requested = False
        if call_id:
            key = (session_id, call_id)
            _call_procs.setdefault(key, set()).add(proc)
            requested = key in _call_requested
    if requested:
        kill_tree(proc)


def unregister_proc(session_id: str | None, proc) -> None:
    if not session_id:
        return
    call_id = current_call()
    with _lock:
        procs = _procs.get(session_id)
        if procs:
            procs.discard(proc)
            if not procs:
                _procs.pop(session_id, None)
        if call_id:
            key = (session_id, call_id)
            call_procs = _call_procs.get(key)
            if call_procs:
                call_procs.discard(proc)
                if not call_procs:
                    _call_procs.pop(key, None)


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
    call_id = current_call()
    with _lock:
        _cancelers.setdefault(session_id, set()).add(fn)
        requested = False
        if call_id:
            key = (session_id, call_id)
            _call_cancelers.setdefault(key, set()).add(fn)
            requested = key in _call_requested
    # Close immediately when the operator's request landed in the small window
    # between dispatch and adapter registration.
    if requested:
        try:
            fn()
        except Exception:
            pass


def unregister_canceler(session_id: str | None, fn) -> None:
    if not session_id:
        return
    call_id = current_call()
    with _lock:
        fns = _cancelers.get(session_id)
        if fns:
            fns.discard(fn)
            if not fns:
                _cancelers.pop(session_id, None)
        if call_id:
            key = (session_id, call_id)
            call_fns = _call_cancelers.get(key)
            if call_fns:
                call_fns.discard(fn)
                if not call_fns:
                    _call_cancelers.pop(key, None)


def request_call(session_id: str, call_id: str) -> None:
    """Abort one in-flight API or CLI call without cancelling sibling seats."""
    key = (str(session_id), str(call_id))
    with _lock:
        _call_requested.add(key)
        fns = list(_call_cancelers.get(key, ()))
        procs = list(_call_procs.get(key, ()))
    for proc in procs:
        kill_tree(proc)
    for fn in fns:
        try:
            fn()
        except Exception:
            pass


def is_call_requested(
    session_id: str | None = None, call_id: str | None = None
) -> bool:
    sid = session_id or current_session()
    cid = call_id or current_call()
    if not sid or not cid:
        return False
    with _lock:
        return (sid, cid) in _call_requested


def clear_call(session_id: str | None, call_id: str | None) -> None:
    if not session_id or not call_id:
        return
    key = (session_id, call_id)
    with _lock:
        _call_requested.discard(key)
        _call_procs.pop(key, None)
        _call_cancelers.pop(key, None)
        _progressers.pop(key, None)


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
        for key in [key for key in _call_procs if key[0] == session_id]:
            _call_procs.pop(key, None)
        for key in [key for key in _call_cancelers if key[0] == session_id]:
            _call_cancelers.pop(key, None)
        for key in [key for key in _call_requested if key[0] == session_id]:
            _call_requested.discard(key)
        for key in [key for key in _progressers if key[0] == session_id]:
            _progressers.pop(key, None)
