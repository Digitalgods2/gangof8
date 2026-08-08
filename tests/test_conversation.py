"""Conversational follow-up: the human can respond to the council's conclusion
and it deliberates again with the full thread as context — no starting over.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from gangof8 import loop
from gangof8.logstore import LogStore
from gangof8.models import FinalAnswer, Goal, SessionStatus, TaskType
from gangof8.sessions import SessionManager
from gangof8.service import GangOf8Service


def test_first_run_records_one_conversation_turn(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    s = svc.run("Compare SQLite vs JSON for logs and recommend one", source="test")
    assert s.status == SessionStatus.done
    assert len(s.turns) == 2
    assert s.turns[0]["role"] == "user" and s.turns[1]["role"] == "council"
    assert s.turns[1]["text"] == s.final.answer


def test_continue_grows_the_conversation(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    s = svc.run("Compare SQLite vs JSON for logs and recommend one", source="test")
    original_turns = list(s.turns)
    original_final = s.final
    s2 = svc.continue_session(s.session_id, "I disagree — JSON is simpler for my case.", background=False)
    assert s2.status == SessionStatus.done
    assert s2.session_id != s.session_id
    assert s2.parent_session_id == s.session_id
    assert len(s2.turns) == 4
    assert s2.turns[2] == {"role": "user", "text": "I disagree — JSON is simpler for my case."}
    assert s2.turns[3]["role"] == "council" and s2.turns[3]["text"] == s2.final.answer
    parent = svc.manager.load(s.session_id)
    assert parent.status == SessionStatus.done
    assert parent.turns == original_turns
    assert parent.final == original_final
    assert s2.agent_calls > 0 and s2.final is not None


def test_accepting_completed_result_does_not_reopen_or_call_models(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    completed = svc.run("What is SQLite?", source="test")
    original_turns = list(completed.turns)
    original_contract = dict(completed.outcome_contract)
    original_calls = completed.agent_calls
    original_final = completed.final

    result = svc.continue_session(
        completed.session_id,
        "  Accept.  ",
        background=False,
    )

    assert result.status == SessionStatus.done
    assert result.turns == original_turns
    assert result.outcome_contract == original_contract
    assert result.agent_calls == original_calls
    assert result.final == original_final
    assert svc.is_terminal_acknowledgement("accept") is True
    assert svc.is_terminal_acknowledgement("accept, but fix the date") is False


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


def test_continue_accepts_attachments_multimodal(tmp_path):
    """Follow-up responses are multi-modal like the task box: document text is
    folded into the turn the council reads, and image attachments join
    session.attachments so vision-capable seats see them on later calls."""
    import base64

    svc = GangOf8Service(data_dir=tmp_path)
    s = svc.run("What is SQLite?", source="test")
    doc = svc.save_upload("notes.txt", base64.b64encode(b"prefer WAL mode").decode())
    img = svc.save_upload("shot.png", base64.b64encode(b"\x89PNG\r\n\x1a\n0000").decode())
    s2 = svc.continue_session(s.session_id, "consider this", background=False,
                              attachments=[doc["id"], img["id"]])
    assert s2.status == SessionStatus.done
    user_turn = s2.turns[2]["text"]
    assert user_turn.startswith("consider this")
    assert "prefer WAL mode" in user_turn, "doc text folded into the turn"
    assert any(a["name"] == "shot.png" and a["kind"] == "image"
               for a in s2.attachments), "image joins session attachments (vision)"
    # an attachment-only response is allowed; a truly empty one is still rejected
    s3 = svc.continue_session(s2.session_id, "", background=False,
                              attachments=[doc["id"]])
    assert s3.turns[-2]["text"].startswith("(see attached)")
    with pytest.raises(ValueError):
        svc.continue_session(s3.session_id, "   ")


def test_cannot_continue_unfinished_session(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    s = svc._open("x", "test", None)  # status received, not done
    with pytest.raises(ValueError):
        svc.continue_session(s.session_id, "hi")


def test_continue_rejects_empty_text(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    s = svc.run("What is SQLite?", source="test")
    with pytest.raises(ValueError):
        svc.continue_session(s.session_id, "   ")


def test_released_artifact_correction_opens_clean_targeted_child(
        tmp_path, monkeypatch):
    """A correction after a failed follow-up still targets the released app."""
    svc = GangOf8Service(data_dir=tmp_path / "data")
    staging = tmp_path / "staging"
    delivered = tmp_path / "delivered"
    staging.mkdir()
    delivered.mkdir()
    original = b"<html><script>weekday = 'wrong';</script></html>"
    readme = b"# Date calculator\n"
    (staging / "app.html").write_bytes(original)
    (staging / "README.md").write_bytes(readme)
    target = delivered / "app.html"
    target.write_bytes(original)
    readme_target = delivered / "README.md"
    readme_target.write_bytes(readme)
    digest = hashlib.sha256(original).hexdigest()
    readme_digest = hashlib.sha256(readme).hexdigest()

    parent = svc._open(
        "Create a self-contained browser-based HTML date calculator",
        "test",
        None,
    )
    parent.status = SessionStatus.done
    parent.final = FinalAnswer(answer="Released app.html", confidence="high")
    parent.turns = [
        {"role": "user", "text": parent.task.text},
        {"role": "council", "text": parent.final.answer},
    ]
    parent.goal_id = "g_released"
    parent.goal_release = True
    parent.delivery_mode = "final_batch"
    parent.workspace_root = str(staging)
    parent.established_root = str(delivered)
    parent.required_files = ["app.html", "README.md"]
    parent.files_changed = [str(target), str(readme_target)]
    parent.verified_output_hashes = {
        "app.html": digest,
        "README.md": readme_digest,
    }
    parent.release_verified_hashes = {
        "app.html": digest,
        "README.md": readme_digest,
    }
    parent.outcome = "succeeded"
    parent.quality_gate = {"status": "PASS"}
    svc.store.save_session(parent)
    svc.goals.save(Goal(
        goal_id=parent.goal_id,
        text=parent.task.text,
        status="completed",
        release_status="released",
        release_session_id=parent.session_id,
        release_files=["app.html", "README.md"],
        staging_root=str(staging),
        established_root=str(delivered),
    ))

    # Reproduce the live failure: an answer-only child incorrectly claimed
    # success and had no artifact records of its own.
    failed_followup = svc._open(
        "The displayed result is wrong; fix it.",
        "followup",
        None,
        outcome_contract=parent.outcome_contract,
        parent_session_id=parent.session_id,
    )
    failed_followup.status = SessionStatus.done
    failed_followup.final = FinalAnswer(
        answer="Suggestions for fixing the app.", confidence="high"
    )
    failed_followup.turns = [
        *parent.turns,
        {"role": "user", "text": failed_followup.task.text},
        {"role": "council", "text": failed_followup.final.answer},
    ]
    failed_followup.goal_id = parent.goal_id
    failed_followup.outcome = "succeeded"
    failed_followup.quality_gate = {"status": "PASS"}
    svc.store.save_session(failed_followup)
    monkeypatch.setattr(
        svc, "_run_owned",
        lambda session, fn, background: session,
    )

    child = svc.continue_session(
        failed_followup.session_id,
        "The text is all blurry on the game. Also, the dog cannot pull Kyle "
        "along; Kyle stays in one place. Kyle is supposed to walk with the dog.",
        background=False,
    )

    assert child.session_id != parent.session_id
    assert child.parent_session_id == failed_followup.session_id
    assert child.turns[-1]["role"] == "user"
    assert len(child.turns) == 5, "the correction is appended exactly once"
    assert child.goal_id == parent.goal_id
    assert child.goal_release is False
    assert child.goal_milestone is None
    assert child.delivery_mode == "immediate"
    assert child.revision_targets == ["app.html"]
    assert child.revision_base_hashes == {"app.html": digest}
    assert child.revision_source_spaces == {"app.html": "established"}
    assert child.required_files == ["app.html"]
    assert child.workspace_root == str(staging)
    assert child.established_root == str(delivered)
    assert child.outcome == "pending"
    assert child.quality_gate == {}
    assert child.release_verified_hashes == {}
    assert child.outcome_contract["task_type"] == TaskType.code.value

    unchanged = svc.manager.load(parent.session_id)
    assert unchanged is not None
    assert unchanged.goal_release is True
    assert unchanged.outcome == "succeeded"
    assert unchanged.quality_gate == {"status": "PASS"}
    assert unchanged.release_verified_hashes == {
        "app.html": digest,
        "README.md": readme_digest,
    }
    assert unchanged.turns == parent.turns

    goal_view = svc.get_goal(parent.goal_id)
    assert goal_view["display_status"] == "revising"
    assert goal_view["actionable_session_id"] == child.session_id
    assert goal_view["latest_revision_session_id"] == child.session_id
    assert "revising app.html" in goal_view["now"]


def test_targeted_followup_cannot_be_downgraded_to_question(
        tmp_path, monkeypatch):
    svc = GangOf8Service(data_dir=tmp_path / "data")
    child = svc._open(
        "The day-of-week result does not agree; fix it.",
        "followup",
        None,
    )
    child.turns = [
        {"role": "user", "text": "The result is wrong; fix it."},
    ]
    child.revision_targets = ["app.html"]
    child.required_files = ["app.html"]
    monkeypatch.setattr(loop, "_deliberate", lambda *args, **kwargs: args[0])

    result = loop.run_session(
        child, svc.manager, svc.registry, svc.governance, svc.store
    )

    assert result.classification.task_type == TaskType.code
    assert result.classification.produces_output is True
    assert loop._effective_agent_timeout(result, "codex", None) == 0


@pytest.fixture()
def client(tmp_path):
    from gangof8 import main as main_mod
    main_mod.service = GangOf8Service(data_dir=tmp_path)
    return TestClient(main_mod.app)


def test_followup_endpoint(client):
    sid = client.post("/tasks", json={"text": "What is SQLite?", "source": "test"}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/followup", json={"text": "Why not DuckDB?"})
    assert r.status_code == 200 and r.json()["session_id"] != sid
    assert client.post("/sessions/nope/followup", json={"text": "x"}).status_code == 404
    assert client.post(f"/sessions/{sid}/followup", json={"text": ""}).status_code == 400


def test_followup_endpoint_reports_terminal_acknowledgement(client):
    sid = client.post(
        "/tasks",
        json={"text": "What is SQLite?", "source": "test"},
    ).json()["session_id"]
    response = client.post(
        f"/sessions/{sid}/followup",
        json={"text": "accept"},
    )
    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
    assert response.json()["status"] == "done"
