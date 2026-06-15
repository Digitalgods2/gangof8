"""Conversational follow-up: the human can respond to the council's conclusion
and it deliberates again with the full thread as context — no starting over.
"""

import pytest
from fastapi.testclient import TestClient

from conclave_os import loop
from conclave_os.logstore import LogStore
from conclave_os.models import SessionStatus
from conclave_os.sessions import SessionManager
from conclave_os.service import ConclaveService


def test_first_run_records_one_conversation_turn(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    s = svc.run("Compare SQLite vs JSON for logs and recommend one", source="test")
    assert s.status == SessionStatus.done
    assert len(s.turns) == 2
    assert s.turns[0]["role"] == "user" and s.turns[1]["role"] == "council"
    assert s.turns[1]["text"] == s.final.answer


def test_continue_grows_the_conversation(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    s = svc.run("Compare SQLite vs JSON for logs and recommend one", source="test")
    s2 = svc.continue_session(s.session_id, "I disagree — JSON is simpler for my case.", background=False)
    assert s2.status == SessionStatus.done
    assert len(s2.turns) == 4
    assert s2.turns[2] == {"role": "user", "text": "I disagree — JSON is simpler for my case."}
    assert s2.turns[3]["role"] == "council" and s2.turns[3]["text"] == s2.final.answer
    # per-turn deliberation state was reset (fresh rounds for the new turn)
    assert s2.agent_calls > 0 and s2.final is not None


def test_conversation_overview_focuses_on_latest_response(tmp_path):
    s = SessionManager(LogStore(tmp_path)).create("x", source="test")
    s.turns = [
        {"role": "user", "text": "original question"},
        {"role": "council", "text": "my earlier conclusion"},
        {"role": "user", "text": "but what about edge case X?"},
    ]
    ov = loop._conversation_overview(s)
    assert "CONVERSATION SO FAR" in ov and "my earlier conclusion" in ov
    assert "LATEST RESPONSE" in ov and "but what about edge case X?" in ov
    # turn one (no prior conversation) → empty overview
    s.turns = []
    assert loop._conversation_overview(s) == ""


def test_cannot_continue_unfinished_session(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    s = svc._open("x", "test", None)  # status received, not done
    with pytest.raises(ValueError):
        svc.continue_session(s.session_id, "hi")


def test_continue_rejects_empty_text(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    s = svc.run("What is SQLite?", source="test")
    with pytest.raises(ValueError):
        svc.continue_session(s.session_id, "   ")


@pytest.fixture()
def client(tmp_path):
    from conclave_os import main as main_mod
    main_mod.service = ConclaveService(data_dir=tmp_path)
    return TestClient(main_mod.app)


def test_followup_endpoint(client):
    sid = client.post("/tasks", json={"text": "What is SQLite?", "source": "test"}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/followup", json={"text": "Why not DuckDB?"})
    assert r.status_code == 200 and r.json()["session_id"] == sid
    assert client.post("/sessions/nope/followup", json={"text": "x"}).status_code == 404
    assert client.post(f"/sessions/{sid}/followup", json={"text": ""}).status_code == 400
