"""Composer polish for the lead-driven model."""

import pytest

from gangof8.adapters.mock import FINAL_JSON, MockAdapter
from gangof8.models import (
    Role,
    Session,
    SessionStatus,
    Task,
)
from gangof8.registry import AdapterResult
from gangof8.service import GangOf8Service

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)


def test_compose_prompt_includes_authoritative_action_outcomes():
    """The summarizer must see what the coordinator actually did, so it reports
    an applied edit as done (not 'couldn't confirm')."""
    from gangof8.composer import compose_prompt
    from gangof8.models import ProposedAction

    session = Session(session_id="s_x", task=Task(task_id="t", session_id="s_x", text="edit it"))
    session.proposed_actions.append(ProposedAction(
        session_id="s_x", kind="edit_file", filename="app.py", status="executed",
        result_path="C:/proj/app.py",
    ))
    prompt = compose_prompt(session)
    assert "edit_file 'app.py': APPLIED" in prompt
    assert "C:/proj/app.py" in prompt
    assert "no filesystem access" in prompt.lower() or "NO filesystem access" in prompt
    assert "authoritative" in prompt.lower()


def test_compose_prompt_omits_ledger_and_rulings_ceremony():
    """The verdict/ruling/ledger ceremony is gone from composition — the prompt
    stays lean even when legacy sessions still carry truth claims."""
    from gangof8.composer import compose_prompt
    from gangof8.models import TruthClaim

    session = Session(session_id="s_x", task=Task(task_id="t", session_id="s_x", text="research it"))
    session.truth_claims.append(TruthClaim(
        claim="SQLite supports atomic transactions",
        source="sqlite docs",
        confidence=0.9,
        asserted_by=Role.knowledge_retriever,
        status="established",
    ))

    prompt = compose_prompt(session)

    assert "Truth ledger" not in prompt
    assert "Disagreement rulings" not in prompt
    assert "Do not promote an assumption to fact" in prompt


class FlakyComposer:
    """Summarizer returns garbage N times before valid JSON; other roles mock."""

    name = "mock"

    def __init__(self, garbage_calls: int):
        self.garbage_left = garbage_calls
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.summarizer and self.garbage_left > 0:
            self.garbage_left -= 1
            return AdapterResult(content="Sure! Here's my summary, in plain prose.", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_composer_retries_once_on_unparseable_output(tmp_path):
    service = GangOf8Service(data_dir=tmp_path)
    service.registry.register(FlakyComposer(garbage_calls=1))
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final.confidence == "high", "the retry should recover a clean final answer"


def test_composer_falls_back_after_two_failures(tmp_path):
    service = GangOf8Service(data_dir=tmp_path)
    service.registry.register(FlakyComposer(garbage_calls=2))
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final.confidence == "low"
    assert session.final.answer.startswith("Partial result")


def test_composer_recomposes_with_a_working_agent_when_summarizer_errors(tmp_path):
    """A flaky summarizer (e.g. gemini timeout) must NOT collapse the answer to a
    partial — recompose with an agent that already worked this run."""
    from gangof8.registry import AgentError

    class GoodAgent:
        name = "good"

        def __init__(self):
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s):
            return self._inner.call(role, prompt, timeout_s)

    class FlakyAgent:
        name = "flaky"

        def call(self, role, prompt, timeout_s):
            raise AgentError("flaky CLI timed out after 150s")

    svc = GangOf8Service(data_dir=tmp_path)
    svc.registry.register(GoodAgent())
    svc.registry.register(FlakyAgent())
    # the lead runs on 'good'; only the summarizer is the flaky agent
    svc.role_agents = {
        Role.lead: "good",
        Role.knowledge_retriever: "good", Role.researcher: "good",
        Role.architect: "good", Role.api_integrator: "good",
        Role.code_generator: "good", Role.critic: "good",
        Role.red_team: "good", Role.fact_validator: "good",
        Role.implementer: "good", Role.summarizer: "flaky",
    }
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final.confidence == "high", "fell back to a working agent, not a partial"
    assert not session.final.answer.startswith("Partial result")
    assert any("recomposed with '" in u for u in session.unresolved)


def test_substantial_prose_is_accepted_at_medium_confidence(tmp_path):
    """Protocol-wrapped agents often answer in plain prose; that IS the answer."""
    PROSE = (
        "SQLite is the recommended default for session logs in a local single-user "
        "service due to durability, crash recovery, and queryability. JSON Lines is "
        "viable only for pure append-only workloads with no querying requirements. "
        "For typical operational needs — debugging, auditing, filtering by session — "
        "SQLite is the conservative and safer choice."
    )

    class ProseComposer(FlakyComposer):
        def call(self, role, prompt, timeout_s):
            if role == Role.summarizer:
                return AdapterResult(content=PROSE, duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    service = GangOf8Service(data_dir=tmp_path)
    service.registry.register(ProseComposer(garbage_calls=0))
    session = service.run(TASK, source="test")
    assert session.final.answer == PROSE, "prose must be accepted whole, not truncated as 'Partial result'"
    assert session.final.confidence == "medium"


def test_labeled_sections_are_parsed(tmp_path):
    """The plain-text contract that protocol-wrapped real agents honor."""

    LABELED = (
        "Here is my summary of the deliberation.\n"
        "**ANSWER:** Use SQLite with a JSONL mirror.\n"
        "It handles querying and durability.\n"
        "CONFIDENCE: I would say High overall.\n"
        "ASSUMPTIONS:\n- single user\n- modest volume\n"
        "RISKS:\n- none\n"
        "NEXT_ACTION: none\n"
    )

    class LabeledComposer(FlakyComposer):
        def call(self, role, prompt, timeout_s):
            if role == Role.summarizer:
                return AdapterResult(content=LABELED, duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    service = GangOf8Service(data_dir=tmp_path)
    service.registry.register(LabeledComposer(garbage_calls=0))
    session = service.run(TASK, source="test")
    final = session.final
    assert final.confidence == "high"
    assert final.answer.startswith("Use SQLite with a JSONL mirror.")
    assert "durability" in final.answer
    assert final.assumptions == ["single user", "modest volume"]
    assert final.risks_unresolved == []  # '- none' is not a risk
    assert final.next_action is None


def test_fenced_json_is_parsed(tmp_path):
    class FencedComposer(FlakyComposer):
        def call(self, role, prompt, timeout_s):
            if role == Role.summarizer:
                return AdapterResult(content=f"```json\n{FINAL_JSON}\n```", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    service = GangOf8Service(data_dir=tmp_path)
    service.registry.register(FencedComposer(garbage_calls=0))
    session = service.run(TASK, source="test")
    assert session.final.confidence == "high"
