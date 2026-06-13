"""Phase 2: approval resolution + session resume.

Approving the gate on a paused session resumes deliberation to completion;
denying it cancels the session. Resume state survives the SQLite round-trip
(the service reloads the session from storage before resuming).
"""

import json

import pytest

from conclave_os.models import SessionStatus
from conclave_os.service import ConclaveService

RISKY = "Delete all temp files in C:\\temp and email me the report"


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


def _pause(service):
    session = service.run(RISKY, source="test")
    assert session.status == SessionStatus.awaiting_approval
    assert len(session.approvals) == 1
    return session


def test_approve_resumes_to_done(service):
    paused = _pause(service)
    resumed = service.approve(paused.session_id, paused.approvals[0].approval_id, approved=True)
    assert resumed.status == SessionStatus.done
    assert resumed.final is not None and resumed.final.answer
    assert resumed.approvals[0].status == "approved"
    assert len(resumed.rounds) >= 1, "deliberation must actually run after approval"
    assert resumed.agent_calls > 0
    # the resume is visible in the reasoning trail
    path = service.store.session_log_path(paused.session_id)
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "session_resumed" in events
    assert events.index("approval_resolved") < events.index("session_resumed")
    # and the resumed result is persisted
    assert service.get(paused.session_id)["status"] == "done"


def test_deny_cancels_without_running_agents(service):
    paused = _pause(service)
    cancelled = service.approve(paused.session_id, paused.approvals[0].approval_id, approved=False)
    assert cancelled.status == SessionStatus.cancelled
    assert cancelled.stop_reason == "approval denied"
    assert cancelled.final is None
    assert cancelled.agent_calls == 0
    assert cancelled.rounds == []
    assert cancelled.approvals[0].status == "denied"


def test_pending_approvals_listing(service):
    paused = _pause(service)
    pending = service.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["session_id"] == paused.session_id
    assert pending[0]["task_text"] == RISKY
    service.approve(paused.session_id, paused.approvals[0].approval_id, approved=True)
    assert service.pending_approvals() == []


def test_unknown_ids_rejected(service):
    paused = _pause(service)
    with pytest.raises(KeyError):
        service.approve("s_nope", "a_nope", approved=True)
    with pytest.raises(KeyError):
        service.approve(paused.session_id, "a_nope", approved=True)


def test_resume_only_from_awaiting_approval(service):
    done = service.run("What is SQLite?", source="test")
    assert done.status == SessionStatus.done
    from conclave_os.loop import resume_session

    with pytest.raises(ValueError):
        resume_session(done, service.manager, service.registry, service.governance, service.store)


def test_api_approval_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path)
    client = TestClient(main_mod.app)

    created = client.post("/tasks", json={"text": RISKY}).json()
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
    assert client.get("/approvals").json() == []

    r404 = client.post(f"/sessions/{sid}/approvals/a_nope", json={"approved": True})
    assert r404.status_code == 404
