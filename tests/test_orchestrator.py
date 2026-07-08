"""Orchestrator model: the lead organizes and integrates; DELEGATED talents do
the substantive work — including authoring files, which are captured directly
from their replies as real proposals instead of being squeezed through the
capped folded-summary pipe (which would truncate a whole file to nothing).
"""

import pytest

from gangof8 import loop, rounds
from gangof8.logstore import LogStore
from gangof8.models import (Classification, Complexity, Contribution,
                                Council, CouncilMember, Risk, Role, TaskType)
from gangof8.sessions import SessionManager

GAME_HTML = "<!doctype html>\n<html><body><script>go()</script></body></html>"


@pytest.fixture()
def store(tmp_path) -> LogStore:
    return LogStore(tmp_path)


@pytest.fixture()
def session(store):
    s = SessionManager(store).create("build game.html in full", source="test")
    s.classification = Classification(
        task_type=TaskType.code, complexity=Complexity.standard,
        risk=Risk.none, produces_output=True)
    return s


def _member(role: Role, agent: str = "mock") -> CouncilMember:
    return CouncilMember(role=role, agent=agent, active=(role == Role.lead))


def _contribution(role: Role, content: str) -> Contribution:
    return Contribution(round=0, role=role, agent="mock", content=content)


def test_delegated_talent_artifacts_are_captured_directly(store, session):
    """A DELEGATE'd coder's ARTIFACT block becomes a real write_file proposal
    stamped with the talent's role; the lead's follow-up gets the file NAME
    and a do-not-re-emit instruction, never the file body."""
    lead = _member(Role.lead)
    coder = _member(Role.code_generator, agent="coder-model")
    council = Council(members=[lead, coder])
    contribution = _contribution(
        Role.lead, "DELEGATE: code_generator - author the complete game.html")
    prompts: list[tuple[Role, str]] = []

    def call(member, prompt):
        prompts.append((member.role, prompt))
        if member.role == Role.code_generator:
            return _contribution(Role.code_generator,
                                 f"Here is the game.\nARTIFACT: game.html\n{GAME_HTML}")
        return _contribution(member.role, "PROMOTE: game.html\nROUND: DONE")

    out = loop._resolve_delegations(session, council, lead, "P", contribution, call, store)

    writes = [a for a in session.proposed_actions if a.kind == "write_file"]
    assert len(writes) == 1
    assert writes[0].filename == "game.html"
    assert writes[0].content == GAME_HTML
    assert writes[0].role == Role.code_generator, "attributed to the talent"
    # the lead was re-called to integrate; it saw the capture note, not the body
    lead_followup = next(p for r, p in prompts if r == Role.lead)
    assert "authored directly into the council space: game.html" in lead_followup
    assert "do NOT re-emit" in lead_followup
    assert GAME_HTML not in lead_followup, "file body never folded into the lead context"
    assert out.content.startswith("PROMOTE:")


def test_consult_reply_artifacts_are_not_captured(store, session):
    """CONSULT is advice — an ARTIFACT block in a consult answer is folded as
    text, never captured as a file proposal."""
    lead = _member(Role.lead)
    critic = _member(Role.critic, agent="critic-model")
    council = Council(members=[lead, critic])
    contribution = _contribution(Role.lead, "CONSULT: critic - is the loop sound?")

    def call(member, prompt):
        if member.role == Role.critic:
            return _contribution(Role.critic, f"ARTIFACT: sketch.html\n{GAME_HTML}")
        return _contribution(member.role, "integrated. ROUND: DONE")

    loop._resolve_delegations(session, council, lead, "P", contribution, call, store)
    assert [a for a in session.proposed_actions if a.kind == "write_file"] == []


def test_lead_promotes_still_collected_after_delegate_capture(store, session):
    """The lead's final draft (PROMOTE lines) must still be collected when
    talent-captured proposals already exist — the old any-proposals guard
    would have dropped delivery exactly when a talent authored the files."""
    from gangof8.models import ProposedAction

    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.code_generator,
        filename="game.html", content=GAME_HTML,
        args={"filename": "game.html", "content": GAME_HTML}))
    session.contributions.append(_contribution(
        Role.lead, "Reviewed the coder's file.\nPROMOTE: game.html\nROUND: DONE"))
    loop._collect_proposals(session, store)
    promotes = [a for a in session.proposed_actions if a.kind == "promote"]
    assert [a.filename for a in promotes] == ["game.html"]
    # and a second collect (resume) does not duplicate
    loop._collect_proposals(session, store)
    assert len([a for a in session.proposed_actions if a.kind == "promote"]) == 1


def test_delegate_prompt_carries_the_file_contract_for_output_tasks(session):
    p = loop.delegate_prompt(session, Role.code_generator, "delegate",
                             "author the complete game.html")
    assert "ARTIFACT: <filename>" in p
    assert "Do NOT emit PROMOTE" in p
    assert "specialist DOING this piece" in p
    # consults stay advice-only (no file-authoring contract; the governance
    # context may still MENTION the ARTIFACT grammar in passing)
    c = loop.delegate_prompt(session, Role.critic, "consult", "is the loop sound?")
    assert "captured directly as real files" not in c
    assert "Do not produce final deliverables" in c


def test_delegate_prompt_carries_the_no_native_tools_context(session):
    """Without the governance context, an agentic CLI seat (codex) told to
    'author a file' tries to CREATE it with its own tools inside its read-only
    sandbox and exits 1 (live delegation failure). Both delegate AND consult
    prompts must state: no native tools, the reply text IS the contribution."""
    for kind in ("delegate", "consult"):
        p = loop.delegate_prompt(session, Role.code_generator, kind, "do the thing")
        assert "NO native tools" in p
        assert "your reply text" in p.lower()


def test_contribution_is_persisted_immediately(tmp_path, store, session):
    """The dashboard polls the STORED snapshot mid-run (service.get →
    store.load_session), and deliberation otherwise saves only at status
    transitions — so every landed contribution must persist the session at
    once, or a whole round of panel takes and talent answers stays invisible
    until the run pauses or finishes (live: 'why am I not seeing anything?')."""
    from gangof8.registry import AdapterResult

    store.save_session(session)

    class OneShotRegistry:
        def call(self, agent, role, prompt, timeout_s, images=None):
            return AdapterResult(content="the take", duration_ms=1)

    member = CouncilMember(role=Role.panelist, agent="mock", active=True)
    loop._agent_call(session, OneShotRegistry(), store, member, "p")
    snap = store.load_session(session.session_id)
    assert snap["contributions"], "visible to the dashboard poll immediately"
    assert snap["contributions"][0]["content"] == "the take"


def test_file_builds_ask_every_seat_for_a_full_candidate(session):
    """Best-of-N: on a file build EVERY seat authors its complete candidate —
    the candidates are scored and the winner ships, so a full draft is the
    point, not waste. (Greenfield and revision both.)"""
    member = CouncilMember(role=Role.panelist, agent="mock", active=True)
    p = rounds.panel_prompt(session, member, 0)
    assert "BEST-OF-N build" in p
    assert "ARTIFACT: <filename>" in p
    assert "highest-scoring file is shipped" in p


def test_non_output_tasks_get_no_file_contract(store):
    """A research/question task doesn't produce a deliverable — no candidate
    contract, panel stays prose."""
    from gangof8.models import Classification, Complexity, Risk, TaskType
    s = SessionManager(store).create("compare SQLite vs JSON", source="test")
    s.classification = Classification(task_type=TaskType.question,
                                      complexity=Complexity.standard, risk=Risk.none,
                                      produces_output=False)
    member = CouncilMember(role=Role.panelist, agent="mock", active=True)
    p = rounds.panel_prompt(s, member, 0)
    assert "BEST-OF-N" not in p and "ARTIFACT:" not in p


def test_lead_charter_is_orchestrator(session):
    """The lead prompt frames the lead as organizer/integrator and, for output
    tasks, tells it to DELEGATE the authoring."""
    council = Council(members=[_member(Role.lead)])
    p = rounds.synthesis_prompt(session, council, None, 0, [])
    assert "NOT the doer" in p
    assert "DELEGATE the authoring itself" in p
