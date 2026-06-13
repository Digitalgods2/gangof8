"""Safest-first test case (DESIGN.md section 6): full pipeline through the
MockAdapter — no network, no tools, no cost."""

import json

import pytest

from conclave_os.models import Complexity, Risk, Role, SessionStatus, TaskType
from conclave_os.service import ConclaveService

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


@pytest.fixture()
def session(service):
    return service.run(TASK, source="test")


def test_session_completes_state_machine(session):
    assert session.session_id.startswith("s_")
    assert session.status == SessionStatus.done
    assert session.stop_reason == "max rounds reached"


def test_classification(session):
    cls = session.classification
    assert cls.task_type == TaskType.question
    assert cls.complexity == Complexity.standard
    assert cls.risk == Risk.none
    assert cls.human_approval_required is False
    assert cls.tools_allowed is False


def test_council_roles_explicit(session):
    active = session.council.active_roles()
    assert {Role.coordinator, Role.researcher, Role.critic, Role.summarizer} <= active
    assert Role.architect not in active
    assert Role.implementer not in active
    # inactive roles are still listed, so the log shows the choice
    assert len(session.council.members) == 7


def test_exactly_three_bounded_rounds(session):
    assert len(session.rounds) == 3
    goals = [r.goal for r in session.rounds]
    assert "gather" in goals[0] and "challenge" in goals[1] and "reconcile" in goals[2]
    assert session.agent_calls <= session.budgets.max_agent_calls


def test_one_disagreement_with_recorded_ruling(session):
    assert len(session.disagreements) == 1
    d = session.disagreements[0]
    assert d.topic == "storage backend"
    assert len(d.positions) == 2
    assert d.critic_test and "VERDICT" in d.critic_test
    assert d.ruling
    assert d.ruling_basis == "evidence"
    assert d.rationale


def test_final_answer_has_required_fields(session):
    final = session.final
    assert final is not None
    assert final.answer
    assert final.confidence in {"high", "medium", "low"}
    assert isinstance(final.assumptions, list)
    assert isinstance(final.risks_unresolved, list)


def test_jsonl_log_contains_every_step(service, session):
    path = service.store.session_log_path(session.session_id)
    assert path.exists()
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert events.count("task_received") == 1
    assert events.count("round_start") == 3
    assert "classified" in events
    assert "council_formed" in events
    assert "disagreement_ruled" in events
    assert "final_composed" in events
    assert events.index("classified") < events.index("council_formed") < events.index("round_start")


def test_session_persisted_in_sqlite(service, session):
    data = service.get(session.session_id)
    assert data is not None
    assert data["status"] == "done"
    assert data["final"]["answer"]
    listed = service.list()
    assert any(s["session_id"] == session.session_id for s in listed)


def test_empty_task_rejected(service):
    with pytest.raises(ValueError):
        service.run("   ", source="test")
