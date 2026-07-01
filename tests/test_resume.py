"""Phase 2: approval resolution + session resume.

The one approval gate left is the promote (workspace → established folder).
Approving it on a paused session resumes deliberation to completion; denying
it skips the delivery but still completes the session. Resume state survives
the SQLite round-trip (the service reloads the session before resuming).
"""

import json

import pytest

from conclave_os.adapters.mock import MockAdapter
from conclave_os.models import Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService

TASK = "Write a short report recommending SQLite or plain JSON for session logs."

PROMOTE_DRAFT = (
    "ARTIFACT: report.md\n"
    "# Storage Recommendation\n\n"
    "Use SQLite for session logs.\n"
    "PROMOTE: report.md\n"
)


class PromotingLead:
    """Lead writes a sandbox file and asks to promote it into established."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            return AdapterResult(content=PROMOTE_DRAFT, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


class _EstablishedService(ConclaveService):
    """Stamps an established folder onto every session so PROMOTE lines become
    approval-gated promote actions — the pause under test."""

    established_root: str | None = None

    def _open(self, *a, **k):
        session = super()._open(*a, **k)
        session.established_root = self.established_root
        self.store.save_session(session)
        return session


@pytest.fixture()
def service(tmp_path):
    est = tmp_path / "established"
    est.mkdir()
    svc = _EstablishedService(data_dir=tmp_path / "data")
    svc.established_root = str(est)
    svc.registry.register(PromotingLead())
    return svc


def _pause(service):
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.awaiting_approval
    assert len(session.approvals) == 1
    assert session.approvals[0].category == "promote"
    return session


def test_approve_resumes_to_done(service):
    paused = _pause(service)
    resumed = service.approve(paused.session_id, paused.approvals[0].approval_id, approved=True)
    assert resumed.status == SessionStatus.done
    assert resumed.final is not None and resumed.final.answer
    assert resumed.approvals[0].status == "approved"
    assert len(resumed.rounds) >= 1, "deliberation ran before the promote gate"
    assert resumed.agent_calls > 0
    promote = next(a for a in resumed.proposed_actions if a.kind == "promote")
    assert promote.status == "executed"
    # the resume is visible in the reasoning trail
    path = service.store.session_log_path(paused.session_id)
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "session_resumed" in events
    assert events.index("approval_resolved") < events.index("session_resumed")
    # and the resumed result is persisted
    assert service.get(paused.session_id)["status"] == "done"


def test_deny_skips_promote_but_completes(service):
    paused = _pause(service)
    done = service.approve(paused.session_id, paused.approvals[0].approval_id, approved=False)
    assert done.status == SessionStatus.done, "denying a promote skips delivery, not the session"
    assert done.approvals[0].status == "denied"
    promote = next(a for a in done.proposed_actions if a.kind == "promote")
    assert promote.status == "denied"
    assert done.final is not None
    assert any("denied" in u for u in done.unresolved)


def test_pending_approvals_listing(service):
    paused = _pause(service)
    pending = service.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["session_id"] == paused.session_id
    assert pending[0]["task_text"] == TASK
    service.approve(paused.session_id, paused.approvals[0].approval_id, approved=True)
    assert service.pending_approvals() == []


def test_unknown_ids_rejected(service):
    paused = _pause(service)
    with pytest.raises(KeyError):
        service.approve("s_nope", "a_nope", approved=True)
    with pytest.raises(KeyError):
        service.approve(paused.session_id, "a_nope", approved=True)


def test_resume_only_from_awaiting_approval(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    done = service.run("What is SQLite?", source="test")
    assert done.status == SessionStatus.done
    from conclave_os.loop import resume_session

    with pytest.raises(ValueError):
        resume_session(done, service.manager, service.registry, service.governance, service.store)


def test_api_approval_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from conclave_os import main as main_mod

    est = tmp_path / "established"
    est.mkdir()
    svc = _EstablishedService(data_dir=tmp_path / "data")
    svc.established_root = str(est)
    svc.registry.register(PromotingLead())
    main_mod.service = svc
    client = TestClient(main_mod.app)

    created = client.post("/tasks", json={"text": TASK}).json()
    assert created["status"] == "awaiting_approval"
    approval_id = created["pending_approvals"][0]["approval_id"]
    sid = created["session_id"]

    listed = client.get("/approvals").json()
    assert [a["approval_id"] for a in listed] == [approval_id]

    resolved = client.post(
        f"/sessions/{sid}/approvals/{approval_id}", json={"approved": True}
    ).json()
    assert resolved["status"] == "done"
    assert resolved["final"]["answer"]
    assert (est / "report.md").exists()
    assert client.get("/approvals").json() == []

    r404 = client.post(f"/sessions/{sid}/approvals/a_nope", json={"approved": True})
    assert r404.status_code == 404
