"""Every loop is bounded: sessions always terminate, and budget exhaustion
degrades to a partial answer instead of spinning or crashing."""

import pytest

from conclave_os.models import Budgets, SessionStatus
from conclave_os.service import ConclaveService

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


def test_lead_runs_a_single_round(service):
    budgets = Budgets(max_rounds=2, max_turns_per_round=1, max_agent_calls=12, max_wall_seconds=60)
    session = service.run(TASK, source="test", budgets=budgets)
    assert session.status == SessionStatus.done
    assert len(session.rounds) == 1  # no ROUND: marker defaults to DONE after round 1
    assert session.stop_reason == "council produced a result"
    assert session.agent_calls <= budgets.max_agent_calls


def test_agent_call_budget_forces_partial_answer(service):
    budgets = Budgets(max_rounds=4, max_turns_per_round=1, max_agent_calls=1, max_wall_seconds=60)
    session = service.run(TASK, source="test", budgets=budgets)
    # the single allowed call is spent by the lead; the composer hits the cap, so
    # the session still finishes but with a partial, low-confidence answer
    assert session.status == SessionStatus.done
    assert session.agent_calls == 1
    assert any("budget exhausted" in u for u in session.unresolved)
    assert session.final is not None
    assert session.final.confidence == "low"


def test_trivial_task_gets_one_round(service):
    session = service.run("What is SQLite?", source="test")
    assert session.status == SessionStatus.done
    assert session.classification.complexity.value == "trivial"
    assert len(session.rounds) == 1
    assert session.budgets.max_rounds == 1


def test_budgets_always_respected(service):
    for budgets in (
        Budgets(max_rounds=1, max_agent_calls=2),
        Budgets(max_rounds=3, max_agent_calls=4),
        Budgets(max_rounds=4, max_agent_calls=100),
    ):
        session = service.run(TASK, source="test", budgets=budgets)
        assert session.status == SessionStatus.done, "session must always terminate"
        assert len(session.rounds) <= budgets.max_rounds
        assert session.agent_calls <= budgets.max_agent_calls
