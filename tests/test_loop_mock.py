"""End-to-end pipeline through the MockAdapter for the lead-driven model — no
network, no tools, no cost."""

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
    assert session.stop_reason == "council produced a result"


def test_classification(session):
    cls = session.classification
    assert cls.task_type == TaskType.question
    assert cls.complexity == Complexity.standard
    assert cls.risk == Risk.none
    assert cls.human_approval_required is False
    assert cls.tools_allowed is False


def test_council_is_lead_plus_panel(session):
    """The council activates coordinator + lead + the panel seats + summarizer;
    the specialist talents are listed but inactive (available for delegation)."""
    active = session.council.active_roles()
    assert {Role.coordinator, Role.lead, Role.panelist, Role.summarizer} <= active
    # specialists are present but inactive until the lead pulls one in
    assert session.council.get(Role.critic).active is False
    assert session.council.get(Role.researcher).active is False
    assert Role.researcher not in active and Role.red_team not in active
    # all roles are still listed, so the UI roster shows what's reachable;
    # the mock backend convenes a one-seat panel
    assert session.panel == ["mock"]
    assert len(session.council.members) == 13 + len(session.panel)


def test_single_round_when_lead_declares_done(session):
    # the mock lead emits no ROUND: marker, which defaults to DONE — one round
    assert len(session.rounds) == 1
    assert "panel round 1" in session.rounds[0].goal
    assert session.rounds[0].agents == [Role.panelist, Role.lead]
    # the panel seat contributed before the lead synthesized
    assert any(c.role == Role.panelist for c in session.contributions)
    assert session.agent_calls <= session.budgets.max_agent_calls


def test_no_disagreement_machinery(session):
    """The court is gone — there are no disagreements to rule."""
    assert session.disagreements == []


def test_final_answer_has_required_fields(session):
    final = session.final
    assert final is not None
    assert final.answer
    assert final.confidence in {"high", "medium", "low"}
    assert isinstance(final.assumptions, list)
    assert isinstance(final.risks_unresolved, list)


def test_truth_ledger_is_a_list(session):
    # The ledger is vestigial in the default flow (no validator seats run unless
    # the lead delegates to one), but it must still be a clean list.
    assert isinstance(session.truth_claims, list)


def test_jsonl_log_contains_every_step(service, session):
    path = service.store.session_log_path(session.session_id)
    assert path.exists()
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert events.count("task_received") == 1
    assert events.count("round_start") == 1
    assert "classified" in events
    assert "council_formed" in events
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


# --- graceful degradation: a failing lead must not crash the session ----------


class _BoomAdapter:
    """A backend that always fails — stands in for the lead CLI stalling."""

    name = "boom"

    def call(self, role, prompt, timeout_s, images=None):
        from conclave_os.registry import AgentError
        raise AgentError("boom CLI timed out after 120s")


def test_failing_lead_is_not_fatal(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(_BoomAdapter())
    # the lead fails; the summarizer still composes a final answer from the rest
    service.role_agents = {**service.role_agents, Role.lead: "boom"}

    session = service.run(TASK, source="test")

    # the run still completes and produces a final answer (does NOT crash)
    assert session.status == SessionStatus.done
    assert session.final is not None and session.final.answer
    # the failure was recorded for the human rather than swallowed
    assert any("agent error" in u for u in session.unresolved)
