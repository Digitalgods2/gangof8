"""Phase 5: service mode + dashboard.

Background submission returns immediately and the session completes on a
worker thread; the dashboard page is served at /; approvals and answers can
resume sessions in the background.
"""

import time

import pytest
from fastapi.testclient import TestClient

from conclave_os.service import ConclaveService

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)
# a task that pauses: the lead promotes into the established folder the task
# references, hitting the ONE approval gate (the pre-run risk gate is gone)
PROMOTING = "Write a short report recommending SQLite, delivered into {est}"

PROMOTE_DRAFT = (
    "ARTIFACT: report.md\n"
    "# Storage Recommendation\n\nUse SQLite.\n"
    "PROMOTE: report.md\n"
)


class _PromotingLead:
    name = "mock"

    def __init__(self):
        from conclave_os.adapters.mock import MockAdapter
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        from conclave_os.models import Role
        from conclave_os.registry import AdapterResult
        # promote only for the delivery-flavored task; plain tasks stay mock
        if role == Role.lead and "delivered into" in prompt:
            return AdapterResult(content=PROMOTE_DRAFT, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def est(tmp_path):
    d = tmp_path / "established"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path, est):
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path / "data")
    main_mod.service.registry.register(_PromotingLead())
    return TestClient(main_mod.app)


def _poll_until(client, session_id, statuses, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = client.get(f"/sessions/{session_id}").json()
        if data["status"] in statuses:
            return data
        time.sleep(0.1)
    raise AssertionError(f"session {session_id} never reached {statuses}")


def test_dashboard_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Conclave OS" in r.text
    assert "human authority preserved" in r.text


def test_health_reports_backend(client):
    h = client.get("/health").json()
    assert h["ok"] is True
    assert h["backend"] == "mock"


def test_delete_session_removes_it(client):
    sid = client.post("/tasks", json={"text": "What is 2+2?", "source": "test"}).json()["session_id"]
    assert any(s["session_id"] == sid for s in client.get("/sessions").json())
    r = client.delete(f"/sessions/{sid}")
    assert r.status_code == 200 and r.json()["deleted"] == sid
    assert not any(s["session_id"] == sid for s in client.get("/sessions").json())
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_delete_missing_session_404(client):
    assert client.delete("/sessions/s_nope").status_code == 404


def test_background_submit_completes(client):
    created = client.post("/tasks", json={"text": TASK, "background": True}).json()
    assert created["status"] == "received", "background submit must return immediately"
    data = _poll_until(client, created["session_id"], {"done"})
    assert data["final"]["answer"]


def test_background_approval_resumes(client, est):
    created = client.post(
        "/tasks", json={"text": PROMOTING.format(est=est), "background": True}
    ).json()
    data = _poll_until(client, created["session_id"], {"awaiting_approval"})
    aid = next(a["approval_id"] for a in data["approvals"] if a["status"] == "pending")
    resolved = client.post(
        f"/sessions/{created['session_id']}/approvals/{aid}",
        json={"approved": True, "background": True},
    ).json()
    assert resolved["status"] in ("awaiting_approval", "deliberating", "composing", "done")
    data = _poll_until(client, created["session_id"], {"done"})
    assert data["final"]["answer"]


def test_sessions_list_is_enriched(client, est):
    created = client.post(
        "/tasks", json={"text": PROMOTING.format(est=est), "background": True}
    ).json()
    _poll_until(client, created["session_id"], {"awaiting_approval"})
    listed = client.get("/sessions").json()
    entry = next(s for s in listed if s["session_id"] == created["session_id"])
    assert entry["task_text"].startswith("Write a short report")
    assert entry["pending_approvals"] == 1
    assert entry["pending_inputs"] == 0


def test_sync_submit_still_works(client):
    created = client.post("/tasks", json={"text": TASK}).json()
    assert created["status"] == "done", "default (sync) behavior is unchanged"
