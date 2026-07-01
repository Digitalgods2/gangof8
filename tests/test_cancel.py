"""Cancelling a session + workspace idempotency.

Cancel is cooperative: a flag (conclave_os.cancellation) is checked at every
agent call, so a running session aborts at the next checkpoint and finalizes to
`cancelled`. A paused (awaiting approval/input) session is cancelled immediately.
"""

import tempfile

import pytest

from conclave_os import cancellation
from conclave_os.models import Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.adapters.mock import MockAdapter
from conclave_os.service import ConclaveService


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


# --- cooperative cancel of a running session ----------------------------------


def test_prerequested_cancel_aborts_run(service):
    s = service._open("compare SQLite vs JSON for session logs", "test", None)
    cancellation.request(s.session_id)
    done = service._safely(s, service._run_full)
    assert done.status == SessionStatus.cancelled
    assert done.stop_reason == "cancelled by user"
    assert not cancellation.is_requested(s.session_id)  # flag cleared after handling


def test_cancel_running_session_sets_flag(service):
    # a 'running' (not-yet-finished) session: cancel_session flags it for the worker
    s = service._open("examine and report", "test", None)
    s.status = SessionStatus.deliberating
    service.store.save_session(s)
    out = service.cancel_session(s.session_id)
    assert out["status"] == "cancelling"
    assert cancellation.is_requested(s.session_id)
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

    class _Svc(ConclaveService):
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
