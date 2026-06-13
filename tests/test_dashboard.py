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
RISKY = "Delete all temp files in C:\\temp and email me the report"


@pytest.fixture()
def client(tmp_path):
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path)
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


def test_background_submit_completes(client):
    created = client.post("/tasks", json={"text": TASK, "background": True}).json()
    assert created["status"] == "received", "background submit must return immediately"
    data = _poll_until(client, created["session_id"], {"done"})
    assert data["final"]["answer"]


def test_background_approval_resumes(client):
    created = client.post("/tasks", json={"text": RISKY, "background": True}).json()
    data = _poll_until(client, created["session_id"], {"awaiting_approval"})
    aid = next(a["approval_id"] for a in data["approvals"] if a["status"] == "pending")
    resolved = client.post(
        f"/sessions/{created['session_id']}/approvals/{aid}",
        json={"approved": True, "background": True},
    ).json()
    assert resolved["status"] in ("awaiting_approval", "deliberating", "composing", "done")
    data = _poll_until(client, created["session_id"], {"done"})
    assert data["final"]["answer"]


def test_sessions_list_is_enriched(client):
    created = client.post("/tasks", json={"text": RISKY, "background": True}).json()
    _poll_until(client, created["session_id"], {"awaiting_approval"})
    listed = client.get("/sessions").json()
    entry = next(s for s in listed if s["session_id"] == created["session_id"])
    assert entry["task_text"].startswith("Delete all temp files")
    assert entry["pending_approvals"] == 1
    assert entry["pending_inputs"] == 0


def test_sync_submit_still_works(client):
    created = client.post("/tasks", json={"text": TASK}).json()
    assert created["status"] == "done", "default (sync) behavior is unchanged"
