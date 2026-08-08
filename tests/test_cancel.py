"""Cancelling a session + workspace idempotency.

Cancel is cooperative: a flag (gangof8.cancellation) is checked at every
agent call, so a running session aborts at the next checkpoint and finalizes to
`cancelled`. A paused (awaiting approval/input) session is cancelled immediately.
"""

import tempfile

import pytest

from gangof8 import cancellation
from gangof8.models import Role, SessionStatus
from gangof8.registry import AdapterResult
from gangof8.adapters.mock import MockAdapter
from gangof8.service import GangOf8Service


@pytest.fixture()
def service(tmp_path):
    return GangOf8Service(data_dir=tmp_path)


# --- cooperative cancel of a running session ----------------------------------


def test_prerequested_cancel_aborts_run(service):
    s = service._open("compare SQLite vs JSON for session logs", "test", None)
    cancellation.request(s.session_id)
    done = service._safely(s, service._run_full)
    assert done.status == SessionStatus.cancelled
    assert done.stop_reason == "cancelled by user"
    assert not cancellation.is_requested(s.session_id)  # flag cleared after handling


def test_individual_call_stop_does_not_cancel_sibling_or_session():
    session_id = "s_call_stop"
    stopped = []
    killed = []

    class Proc:
        def __init__(self, name):
            self.name = name

        def kill(self):
            killed.append(self.name)

    cancellation.set_current_session(session_id)
    cancellation.set_current_call("call_a")
    cancellation.register_canceler(session_id, lambda: stopped.append("a"))
    proc_a = Proc("a")
    cancellation.register_proc(session_id, proc_a)
    cancellation.set_current_call("call_b")
    cancellation.register_canceler(session_id, lambda: stopped.append("b"))
    proc_b = Proc("b")
    cancellation.register_proc(session_id, proc_b)
    try:
        cancellation.request_call(session_id, "call_a")

        assert stopped == ["a"]
        assert killed == ["a"]
        assert cancellation.is_call_requested(session_id, "call_a")
        assert not cancellation.is_call_requested(session_id, "call_b")
        assert not cancellation.is_requested(session_id)
    finally:
        cancellation.set_current_call(None)
        cancellation.set_current_session(None)
        cancellation.clear(session_id)


def test_precancel_skips_preround_web_research(service, monkeypatch):
    """A cancel requested before deliberation must abort BEFORE the pre-round
    context build — which can include a slow, non-interruptible web search — not
    pay for it and then cancel."""
    from gangof8 import cancellation, loop

    called = {"overview": False}

    def _boom(session):
        called["overview"] = True
        return "SHOULD-NOT-RUN"

    monkeypatch.setattr(loop, "_web_overview", _boom)
    s = service._open("research the latest developments and summarize", "test", None)
    cancellation.request(s.session_id)
    done = service._safely(s, service._run_full)
    assert done.status == SessionStatus.cancelled
    assert called["overview"] is False, "pre-round web research ran despite a pending cancel"


def test_cancel_during_preround_overview_is_prompt(service, monkeypatch):
    """A cancel that lands WHILE the pre-round web research is running must abort
    promptly (abandon the un-killable search) rather than block until it returns."""
    import threading

    from gangof8 import cancellation, loop

    started = threading.Event()
    release = threading.Event()  # never set during the run: the search stays slow

    def _slow(session):
        started.set()
        release.wait(timeout=10)  # simulate a slow, un-abortable web search
        return "late overview"

    monkeypatch.setattr(loop, "_web_overview", _slow)
    s = service._open("research something current and summarize", "test", None)
    out: dict = {}
    worker = threading.Thread(target=lambda: out.update(session=service._safely(s, service._run_full)))
    worker.start()
    try:
        assert started.wait(5), "the pre-round overview should have started"
        cancellation.request(s.session_id)
        worker.join(timeout=5)
        assert not worker.is_alive(), "cancel must not block on the slow overview"
        assert out["session"].status == SessionStatus.cancelled
    finally:
        release.set()
        worker.join(timeout=10)


def test_startup_reconciles_orphaned_live_session(tmp_path):
    """A session left in a live state by a dead process (e.g. a restart) is
    finalized to cancelled when a fresh service boots on the same data dir, so it
    can't linger as an un-cancellable 'deliberating' ghost."""
    svc1 = GangOf8Service(data_dir=tmp_path)
    s = svc1._open("build a thing", "test", None)
    s.status = SessionStatus.deliberating          # simulate a crash mid-run
    svc1.store.save_session(s)

    svc2 = GangOf8Service(data_dir=tmp_path)       # a fresh process boots
    reloaded = svc2.manager.load(s.session_id)
    assert reloaded.status == SessionStatus.cancelled
    assert "restart" in (reloaded.stop_reason or "")


def test_cancel_running_session_sets_flag(service):
    # A running session is made durably terminal immediately; the cancellation
    # flag still tears down any adapter work owned by the revoked worker.
    s = service._open("examine and report", "test", None)
    s.status = SessionStatus.deliberating
    service.store.save_session(s)
    out = service.cancel_session(s.session_id)
    assert out["status"] == "cancelled"
    assert cancellation.is_requested(s.session_id)
    persisted = service.manager.load(s.session_id)
    assert persisted.status == SessionStatus.cancelled
    assert persisted.active_agent_calls == []
    cancellation.clear(s.session_id)


# --- immediate cancel of a paused session -------------------------------------


class _ApprovalAdapter:
    """Implementer promotes into an established folder → pauses for approval."""

    name = "mock"

    def __init__(self, est):
        self._inner = MockAdapter()
        self._est = est

    def call(self, role, prompt, timeout_s):
        if role == Role.lead:
            return AdapterResult(content="ARTIFACT: r.md\nhi\nPROMOTE: r.md\n", duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_cancel_paused_session_immediately(tmp_path):
    est = tmp_path / "established"
    est.mkdir()

    class _Svc(GangOf8Service):
        def _open(self, *a, **k):
            sess = super()._open(*a, **k)
            sess.established_root = str(est)
            self.store.save_session(sess)
            return sess

    svc = _Svc(data_dir=tmp_path / "data")
    svc.registry.register(_ApprovalAdapter(str(est)))
    s = svc.run("add a thing to the app", source="test")
    assert s.status == SessionStatus.awaiting_approval  # paused on the promote gate

    out = svc.cancel_session(s.session_id)
    assert out["status"] == "cancelled"
    reloaded = svc.manager.load(s.session_id)
    assert reloaded.status == SessionStatus.cancelled
    assert all(a.status != "pending" for a in reloaded.approvals)  # pending gate cleared
    assert not (est / "r.md").exists()  # nothing promoted


def test_cancel_finished_session_is_noop(service):
    s = service.run("what is 2+2?", source="test")  # completes immediately
    assert s.status == SessionStatus.done
    out = service.cancel_session(s.session_id)
    assert out["status"] == "done" and "already finished" in out["note"]


def test_cancel_unknown_session_raises(service):
    with pytest.raises(KeyError):
        service.cancel_session("s_nope")


# --- workspace idempotency ----------------------------------------------------


def test_adding_same_workspace_root_is_idempotent(service):
    d = tempfile.mkdtemp()
    a = service.create_workspace("proj", d)
    b = service.create_workspace("proj-renamed", d)  # same folder
    assert a.id == b.id
    assert len(service.list_workspaces()["workspaces"]) == 1


# --- cancel is prompt even when a panel seat can't be hard-killed -----------------


def test_cancel_aborts_while_a_panel_seat_is_stuck(tmp_path):
    """Regression: an API-based panel seat is an HTTP call cancel can't kill like
    a CLI subprocess. The fan-out must stop WAITING for it on cancel instead of
    blocking until it returns — otherwise 'Cancel run' hangs the whole round."""
    import threading

    started = threading.Event()
    release = threading.Event()  # never set during the run: the seat stays "in flight"

    class StuckSeat:
        name = "stuck"

        def call(self, role, prompt, timeout_s, images=None):
            started.set()
            release.wait(timeout=30)  # simulate an un-killable, slow API call
            return AdapterResult(content="late take", duration_ms=1)

    # panel = default 'mock' seat (fast) + the stuck seat; lead resolves to 'mock'
    svc = GangOf8Service(data_dir=tmp_path, panel=["mock", "stuck"])
    svc.registry.register(StuckSeat())
    s = svc._open("compare SQLite vs JSON for session logs", "test", None)

    out: dict = {}
    worker = threading.Thread(target=lambda: out.update(session=svc._safely(s, svc._run_full)))
    worker.start()
    try:
        assert started.wait(5), "the stuck panel seat should have started"
        cancellation.request(s.session_id)          # human hits 'Cancel run'
        worker.join(timeout=10)
        assert not worker.is_alive(), "cancel must not block on the stuck seat"
        assert out["session"].status == SessionStatus.cancelled
        assert out["session"].stop_reason == "cancelled by user"
    finally:
        release.set()  # let the abandoned seat thread finish and exit
        worker.join(timeout=10)
