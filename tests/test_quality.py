"""Phase 3: richer disagreement detection + composer polish."""

import pytest

from conclave_os.adapters.mock import FINAL_JSON, MockAdapter
from conclave_os.loop import detect_disagreements
from conclave_os.models import (
    Contribution,
    Role,
    RoundSpec,
    Session,
    SessionStatus,
    Task,
)
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)


def _session_with(contents: list[tuple[Role, str]]) -> tuple[Session, RoundSpec]:
    task = Task(task_id="t_x", session_id="s_x", text="test task")
    session = Session(session_id="s_x", task=task)
    for role, content in contents:
        session.contributions.append(
            Contribution(round=0, role=role, agent="mock", content=content)
        )
    spec = RoundSpec(round=0, goal="challenge", agents=[Role.critic])
    return session, spec


def test_compose_prompt_includes_authoritative_action_outcomes():
    """The summarizer must see what the coordinator actually did, so it reports
    an applied edit as done (not 'couldn't confirm')."""
    from conclave_os.composer import compose_prompt
    from conclave_os.models import ProposedAction

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


def test_detects_bullets_case_and_multiline():
    session, spec = _session_with([
        (Role.researcher, "- SQLite has transactions."),
        (Role.critic,
         "1. disagreement: topic A — claim line one\n"
         "   continuation of the claim\n"
         "\n"
         "- DISAGREE: topic B — a second conflict"),
    ])
    found = detect_disagreements(session, spec)
    assert [d.topic for d in found] == ["topic A", "topic B"]
    assert "continuation of the claim" in found[0].positions[1]["claim"]
    assert found[0].positions[0]["role"] == "researcher"


def test_attribution_prefers_claim_making_roles():
    session, spec = _session_with([
        (Role.researcher, "Claim: use SQLite."),
        (Role.summarizer, "Interim note."),
        (Role.critic, "DISAGREEMENT: storage — JSON is fine."),
    ])
    found = detect_disagreements(session, spec)
    assert found[0].positions[0]["role"] == "researcher", (
        "the challenged claim should come from a claim-making role, "
        "not just the most recent speaker"
    )


def test_pass_yields_no_disagreements():
    session, spec = _session_with([
        (Role.researcher, "- facts"),
        (Role.critic, "PASS"),
    ])
    assert detect_disagreements(session, spec) == []


def test_prose_mentions_of_disagree_are_not_markers():
    session, spec = _session_with([
        (Role.researcher, "Some people disagree: that is normal in research."),
        (Role.critic, "I would not disagree with the facts presented."),
    ])
    assert detect_disagreements(session, spec) == []


def test_dedupes_topics_across_rounds():
    session, spec = _session_with([
        (Role.critic, "DISAGREEMENT: storage backend — JSON suffices."),
    ])
    first = detect_disagreements(session, spec)
    session.disagreements.extend(first)
    assert detect_disagreements(session, spec) == []


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
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(FlakyComposer(garbage_calls=1))
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final.confidence == "high", "the retry should recover a clean final answer"


def test_composer_falls_back_after_two_failures(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(FlakyComposer(garbage_calls=2))
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final.confidence == "low"
    assert session.final.answer.startswith("Partial result")


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

    service = ConclaveService(data_dir=tmp_path)
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

    service = ConclaveService(data_dir=tmp_path)
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

    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(FencedComposer(garbage_calls=0))
    session = service.run(TASK, source="test")
    assert session.final.confidence == "high"
