"""Panel rounds: parallel multi-seat fan-out, lead synthesis with ROUND:
DONE/CONTINUE, and the every-n-rounds human consent gate. Rotation is
automatic; the human is asked before each additional block of rounds."""

import json
import threading
import time


from gangof8 import config, rounds
from gangof8.adapters.mock import MockAdapter
from gangof8.models import Role, Session, SessionStatus
from gangof8.registry import AdapterResult, AgentError
from gangof8.service import GangOf8Service

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)


# --- marker parsing -------------------------------------------------------------


def test_no_marker_defaults_to_done():
    assert rounds.parse_round_decision("here is my answer") == ("DONE", "")
    assert rounds.parse_round_decision("") == ("DONE", "")


def test_continue_marker_with_reason():
    decision, why = rounds.parse_round_decision(
        "draft so far...\nROUND: CONTINUE - still need the perf numbers"
    )
    assert decision == "CONTINUE"
    assert why == "still need the perf numbers"


def test_marker_survives_markdown_envelopes():
    assert rounds.parse_round_decision("- **ROUND: DONE**")[0] == "DONE"
    assert rounds.parse_round_decision("**ROUND**: CONTINUE — more input")[0] == "CONTINUE"


def test_last_marker_wins():
    text = "ROUND: CONTINUE - early thought\n...revised...\nROUND: DONE"
    assert rounds.parse_round_decision(text)[0] == "DONE"


# --- scripted leads --------------------------------------------------------------


class ContinuingLead:
    """Lead that always wants another round — drives the consent gate."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            return AdapterResult(
                content="Refined the recommendation further.\nROUND: CONTINUE - keep refining",
                duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


class DoneAfterLead:
    """CONTINUE for n rounds, then DONE — proves mid-block early exit."""

    name = "mock"

    def __init__(self, continues: int):
        self.continues = continues
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            if self.continues > 0:
                self.continues -= 1
                return AdapterResult(content="More to do.\nROUND: CONTINUE - open", duration_ms=1)
            return AdapterResult(content="Final synthesis.\nROUND: DONE", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def _svc(tmp_path, adapter=None, **kwargs):
    svc = GangOf8Service(data_dir=tmp_path, **kwargs)
    if adapter is not None:
        svc.registry.register(adapter)
    return svc


def _events(svc, session):
    path = svc.store.session_log_path(session.session_id)
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]


# --- automatic rotation + the consent gate ---------------------------------------


def test_three_continues_pause_for_consent(tmp_path):
    svc = _svc(tmp_path, ContinuingLead())
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.awaiting_input
    req = session.input_requests[-1]
    assert req.agent == "system" and req.purpose == "continue_rounds"
    assert "3 rounds" in req.question
    assert "round 1:" in req.question and "round 3:" in req.question, "summaries shown"
    assert len(session.rounds) == config.ROUNDS_PER_CONSENT
    assert _events(svc, session).count("round_start") == 3
    assert session.final is None, "no answer composed while awaiting consent"


def test_consent_yes_grants_another_block(tmp_path):
    svc = _svc(tmp_path, ContinuingLead())
    session = svc.run(TASK, source="test")
    req = session.input_requests[-1]
    session = svc.answer(session.session_id, req.input_id, "yes")
    # another block ran and the gate asked again
    assert session.consent_extra_rounds == config.ROUNDS_PER_CONSENT
    assert len(session.rounds) == 2 * config.ROUNDS_PER_CONSENT
    assert session.status == SessionStatus.awaiting_input
    assert session.input_requests[-1].purpose == "continue_rounds"
    assert len(session.input_requests) == 2


def test_consent_number_grants_exactly_that_many(tmp_path):
    svc = _svc(tmp_path, ContinuingLead())
    session = svc.run(TASK, source="test")
    req = session.input_requests[-1]
    session = svc.answer(session.session_id, req.input_id, "2")
    assert session.consent_extra_rounds == 2
    assert len(session.rounds) == config.ROUNDS_PER_CONSENT + 2
    assert session.status == SessionStatus.awaiting_input


def test_consent_no_composes_from_work_so_far(tmp_path):
    svc = _svc(tmp_path, ContinuingLead())
    session = svc.run(TASK, source="test")
    rounds_before = len(session.rounds)
    req = session.input_requests[-1]
    session = svc.answer(session.session_id, req.input_id, "no")
    assert session.compose_now is True
    assert session.status == SessionStatus.done
    assert session.final is not None and session.final.answer
    assert len(session.rounds) == rounds_before, "no further rounds after 'no'"


def test_done_mid_block_exits_early(tmp_path):
    svc = _svc(tmp_path, DoneAfterLead(continues=1))
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert len(session.rounds) == 2  # CONTINUE once, DONE in round 2
    assert session.final is not None


def test_round_decisions_logged(tmp_path):
    svc = _svc(tmp_path, DoneAfterLead(continues=1))
    session = svc.run(TASK, source="test")
    events = _events(svc, session)
    assert events.count("round_synthesized") == 2


# --- panel fan-out ----------------------------------------------------------------


class _SharedCounter:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.calls = 0


class ProbeSeat:
    """A panel seat that records overlap with its sibling seats."""

    def __init__(self, name: str, shared: _SharedCounter, delay: float = 0.05):
        self.name = name
        self.shared = shared
        self.delay = delay

    def call(self, role, prompt, timeout_s, images=None):
        with self.shared.lock:
            self.shared.running += 1
            self.shared.max_running = max(self.shared.max_running, self.shared.running)
            self.shared.calls += 1
        try:
            time.sleep(self.delay)
            return AdapterResult(content=f"panel take from {self.name}", duration_ms=1)
        finally:
            with self.shared.lock:
                self.shared.running -= 1


def test_panel_seats_run_concurrently(tmp_path):
    shared = _SharedCounter()
    svc = GangOf8Service(data_dir=tmp_path, panel=["alpha", "beta", "gamma"])
    for name in ("alpha", "beta", "gamma"):
        svc.registry.register(ProbeSeat(name, shared))
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert shared.calls == 3
    assert shared.max_running >= 2, "panel seats should overlap, not serialize"
    panelists = [c for c in session.contributions if c.role == Role.panelist]
    assert {c.agent for c in panelists} == {"alpha", "beta", "gamma"}
    # each seat's take is fed to the lead's synthesis prompt
    assert all(c.content.startswith("panel take from") for c in panelists)


def test_failing_seat_is_dropped_not_fatal(tmp_path):
    class BoomSeat:
        name = "boom"

        def call(self, role, prompt, timeout_s, images=None):
            raise AgentError("boom seat exploded")

    svc = GangOf8Service(data_dir=tmp_path, panel=["mock", "boom"])
    svc.registry.register(BoomSeat())
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final is not None
    assert "panel_seat_dropped" in _events(svc, session)
    assert any("boom" in u and "dropped" in u for u in session.unresolved)
    panelists = [c for c in session.contributions if c.role == Role.panelist]
    assert [c.agent for c in panelists] == ["mock"], "the healthy seat still contributed"


def test_budget_exact_under_panel_contention(tmp_path):
    """With fewer budget slots than concurrent seats, the reserve-under-lock
    accounting never oversubscribes and the run degrades to compose."""
    from gangof8.models import Budgets

    shared = _SharedCounter()
    svc = GangOf8Service(data_dir=tmp_path, panel=["alpha", "beta", "gamma"])
    for name in ("alpha", "beta", "gamma"):
        svc.registry.register(ProbeSeat(name, shared))
    budgets = Budgets(max_agent_calls=2, max_wall_seconds=60)
    session = svc.run(TASK, source="test", budgets=budgets)
    assert session.status == SessionStatus.done, "budget exhaustion degrades, never crashes"
    assert session.agent_calls <= budgets.max_agent_calls
    assert shared.calls <= budgets.max_agent_calls, "no oversubscription past the reserve"


def test_solo_mode_with_empty_panel(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.panel == []
    assert not any(c.role == Role.panelist for c in session.contributions)
    assert session.rounds[0].agents == [Role.lead]


def test_consult_still_available_inside_a_round(tmp_path):
    class ConsultingLead:
        name = "mock"

        def __init__(self):
            self.asked = False
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                if not self.asked:
                    self.asked = True
                    return AdapterResult(
                        content="CONSULT: architect - which storage layout?", duration_ms=1)
                return AdapterResult(content="Final, informed answer.\nROUND: DONE", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    svc = _svc(tmp_path, ConsultingLead())
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert any(c.role == Role.architect for c in session.contributions), "specialist consulted"
    assert len(session.rounds) == 1, "delegation happens inside the round, not as a new round"


# --- stub synthesis: announcing the work is not doing it --------------------------


LIVE_STUB = (
    "I'll read the core source files directly to ground my analysis, "
    "then deliver the recommendation set."
)


def test_stub_detection_matches_the_live_failure():
    assert rounds.reply_is_stub(LIVE_STUB) is True


def test_stub_panel_and_failed_lead_cannot_false_success_an_output_task(tmp_path):
    """Regression for the live failure: Gemini-style stubs plus a dead lead
    must not let the composer claim that an output file was written."""

    class StubGemini:
        name = "gemini"

        def __init__(self):
            self.calls = []

        def call(self, role, prompt, timeout_s, images=None):
            del prompt, timeout_s, images
            self.calls.append(role)
            return AdapterResult(content=LIVE_STUB, duration_ms=1)

    class FailedLeadAndLyingComposer:
        name = "failed-lead"

        def __init__(self):
            self.calls = []

        def call(self, role, prompt, timeout_s, images=None):
            del prompt, timeout_s, images
            self.calls.append(role)
            if role == Role.lead:
                raise AgentError("lead backend disabled")
            return AdapterResult(
                content=(
                    "ANSWER: report.md was written successfully.\n"
                    "CONFIDENCE: high\nASSUMPTIONS:\n- none\nRISKS:\n- none\n"
                    "NEXT_ACTION: none"
                ),
                duration_ms=1,
            )

    gemini = StubGemini()
    failed = FailedLeadAndLyingComposer()
    svc = GangOf8Service(
        data_dir=tmp_path,
        panel=["gemini"],
        role_agents={Role.lead: "failed-lead", Role.summarizer: "failed-lead"},
    )
    svc.registry.register(gemini)
    svc.registry.register(failed)

    session = svc.run("Write report.md containing a launch checklist.", source="test")

    assert session.classification.produces_output is True
    assert session.status == SessionStatus.failed
    assert session.outcome == "failed_verification"
    assert session.quality_gate["stage"] == "required_output"
    assert session.final.confidence == "low"
    assert "did not produce or modify" in session.final.answer
    assert "available implementation author" in session.final.next_action
    assert session.files_changed == []
    assert not [
        action for action in session.proposed_actions
        if action.kind in ("write_file", "edit_file", "promote")
        and action.status == "executed"
    ]
    assert Role.panelist in gemini.calls
    assert failed.calls == [Role.lead], "the output gate must run before composition"
    assert "required_output_gate_failed" in _events(svc, session)


def test_output_gate_covers_code_content_and_explicit_revisions(tmp_path):
    from gangof8 import loop
    from gangof8.logstore import LogStore
    from gangof8.models import Classification, Complexity, Risk, TaskType
    from gangof8.sessions import SessionManager

    session = SessionManager(LogStore(tmp_path)).create("task", source="test")
    session.classification = Classification(
        task_type=TaskType.content,
        complexity=Complexity.standard,
        risk=Risk.none,
        produces_output=True,
    )
    assert loop._requires_file_output(session)

    session.classification.produces_output = False
    session.classification.task_type = TaskType.code
    assert loop._requires_file_output(session)

    session.classification.task_type = TaskType.question
    session.revision_targets = ["app.py"]
    assert loop._requires_file_output(session)

    session.revision_targets = []
    session.classification.task_type = TaskType.action
    session.classification.produces_output = True
    assert not loop._requires_file_output(session)


def test_short_direct_answer_is_not_a_stub():
    # the mock lead's legitimate short answer has no deferral phrasing
    from gangof8.adapters.mock import LEAD_ANSWER

    assert rounds.reply_is_stub(LEAD_ANSWER) is False
    assert rounds.reply_is_stub("SQLite. It is the safer default.") is False


def test_marker_lines_are_never_stubs():
    assert rounds.reply_is_stub("I'll consult a specialist first.\nCONSULT: architect - layout?") is False
    assert rounds.reply_is_stub("Let me check the file.\nSKILL: read_file main.py") is False
    assert rounds.reply_is_stub("I'll wrap up here.\nROUND: DONE") is False


def test_unresolved_skill_request_is_a_stub_after_resolution():
    """Before the resolver runs, a SKILL: line is legitimate work-in-progress;
    after it has run, a line still standing is a request that will never be
    honored (live: a round ended on the bare line 'SKILL: search_project …'
    accepted as the synthesis, and nothing was delivered)."""
    dangling = "SKILL: search_project ghost speed interval moveGhost keydown"
    assert rounds.reply_is_stub(dangling) is False  # pre-resolution: legitimate
    assert rounds.reply_is_stub(dangling, skills_resolved=True) is True
    # substantial prose around a leftover request line still counts as work
    real = "Substance. " * 40 + "\nSKILL: read_file x.py"
    assert rounds.reply_is_stub(real, skills_resolved=True) is False
    # an ARTIFACT block is real work regardless of the flag
    art = "SKILL: read_file x.py\nARTIFACT: index.html\n<html></html>"
    assert rounds.reply_is_stub(art, skills_resolved=True) is False


def test_long_reply_is_not_a_stub():
    assert rounds.reply_is_stub("I'll now explain in detail. " + "Substance. " * 60) is False


# the exact shape a claude lead produced live: blocked native tool calls
# rendered as text — 968 chars of debris that sailed past the length-only check
LIVE_TOOL_DEBRIS = (
    "I'm running in the actual repo, so I'll read the core files directly rather "
    "than relying on the excerpts — starting with the loop, delegation machinery, "
    "and the prompt layer.\n\n"
    "<summary>Read loop.py (deliberation loop core)</summary>\n"
    '{\n  "file_path": "C:\\\\Users\\\\gosmo\\\\Desktop\\\\Gang of 8\\\\gangof8\\\\loop.py"\n}\n\n'
    "<summary>Read rounds.py (prompt layer)</summary>\n"
    '{\n  "file_path": "C:\\\\Users\\\\gosmo\\\\Desktop\\\\Gang of 8\\\\gangof8\\\\rounds.py"\n}\n\n'
    "<summary>Read config.py (budgets, seats)</summary>\n"
    '{\n  "file_path": "C:\\\\Users\\\\gosmo\\\\Desktop\\\\Gang of 8\\\\gangof8\\\\config.py"\n}\n\n'
    "<summary>Read models.py</summary>\n"
    '{\n  "file_path": "C:\\\\Users\\\\gosmo\\\\Desktop\\\\Gang of 8\\\\gangof8\\\\models.py"\n}\n\n'
    "<summary>Read roles.py (council building)</summary>\n"
    '{\n  "file_path": "C:\\\\Users\\\\gosmo\\\\Desktop\\\\Gang of 8\\\\gangof8\\\\roles.py"\n}\n\n'
    "<summary>Read governance.py</summary>\n"
    '{\n  "file_path": "C:\\\\Users\\\\gosmo\\\\Desktop\\\\Gang of 8\\\\gangof8\\\\governance.py"\n}'
)


def test_tool_call_debris_is_a_stub_regardless_of_length():
    assert len(LIVE_TOOL_DEBRIS) > config.SYNTHESIS_STUB_CHARS
    assert rounds.reply_is_stub(LIVE_TOOL_DEBRIS) is True


def test_debris_plus_real_analysis_is_not_a_stub():
    # a reply that attempted a tool call but ALSO delivered real substance keeps
    # the substance
    real = LIVE_TOOL_DEBRIS + "\n\nDespite that, my analysis: " + "the loop should split. " * 20
    assert rounds.reply_is_stub(real) is False


def test_prose_mentioning_file_path_is_not_debris():
    prose = ('The config uses a "file_path" argument throughout, which is fine. '
             + "More detail on why this design holds up. " * 10)
    assert rounds.reply_is_stub(prose) is False


class StubbingLead:
    """Lead answers with an announcement first; delivers only when re-asked
    (nudged) — reproduces the live cli-backend failure."""

    name = "mock"

    def __init__(self, stubs: int = 1):
        self.stubs = stubs
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            if self.stubs > 0:
                self.stubs -= 1
                return AdapterResult(content=LIVE_STUB, duration_ms=1)
            return AdapterResult(
                content="Full grounded recommendation set: use X, refactor Y, delete Z.\nROUND: DONE",
                duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_stub_lead_is_recalled_once_and_delivers(tmp_path):
    svc = _svc(tmp_path, StubbingLead(stubs=1))
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert "synthesis_stub_retry" in _events(svc, session)
    leads = [c for c in session.contributions if c.role == Role.lead]
    assert len(leads) == 2, "the stub triggered exactly one re-call"
    assert "recommendation set" in leads[-1].content
    assert len(session.rounds) == 1
    assert not any("stub twice" in u for u in session.unresolved)


def test_double_stub_degrades_to_composer(tmp_path):
    svc = _svc(tmp_path, StubbingLead(stubs=99))
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final is not None and session.final.answer
    assert any("stub twice" in u for u in session.unresolved)
    # only one retry was spent — no runaway re-calling
    assert _events(svc, session).count("synthesis_stub_retry") == 1


def test_stubbing_panel_seat_is_dropped_from_synthesis(tmp_path):
    """A panelist that emits tool-call debris is dropped for the round: its
    debris must NOT reach the lead's synthesis prompt; healthy seats still do."""

    class HealthySeat:
        name = "alpha"

        def call(self, role, prompt, timeout_s, images=None):
            return AdapterResult(content="panel take from alpha: use SQLite.", duration_ms=1)

    class DebrisSeat:
        name = "beta"

        def call(self, role, prompt, timeout_s, images=None):
            return AdapterResult(content=LIVE_TOOL_DEBRIS, duration_ms=1)

    class CapturingLead:
        name = "mock"

        def __init__(self):
            self.synthesis_prompt = None
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                self.synthesis_prompt = prompt
                return AdapterResult(content="Synthesis over the healthy takes.\nROUND: DONE",
                                     duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    lead = CapturingLead()
    svc = GangOf8Service(data_dir=tmp_path, panel=["alpha", "beta"])
    svc.registry.register(HealthySeat())
    svc.registry.register(DebrisSeat())
    svc.registry.register(lead)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert "panel take from alpha" in lead.synthesis_prompt
    assert "file_path" not in lead.synthesis_prompt, "debris kept out of the synthesis"
    assert "panel_seat_dropped" in _events(svc, session)
    assert any("beta" in u and "stub" in u for u in session.unresolved)


# --- task-aware skill-request cap --------------------------------------------------


def test_analysis_tasks_get_a_higher_skill_cap(tmp_path):
    from gangof8 import loop
    from gangof8.classifier import classify
    from gangof8.logstore import LogStore
    from gangof8.sessions import SessionManager

    store = LogStore(tmp_path)
    s = SessionManager(store).create("t", source="test")
    s.classification = classify("examine this app and recommend improvements")
    assert s.classification.task_type.value == "research"
    assert loop._skill_request_cap(s) == config.MAX_SKILL_REQUESTS_ANALYSIS

    s.classification = classify("implement a parser module in parser.py")
    assert s.classification.task_type.value == "code"
    assert loop._skill_request_cap(s) == config.MAX_SKILL_REQUESTS_PER_TURN


def test_research_lead_gets_more_than_two_skill_results(tmp_path):
    """The live starvation: an examine-task lead asked for many reads and got 2.
    On an analysis task, more than two SKILL: lines now resolve in one turn."""

    class HungryLead:
        name = "mock"

        def __init__(self):
            self.followup_prompt = None
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                if "Skill results" in prompt:
                    self.followup_prompt = prompt
                    return AdapterResult(content="Analysis done.\nROUND: DONE", duration_ms=1)
                return AdapterResult(
                    content=("SKILL: bogus_one x\nSKILL: bogus_two y\nSKILL: bogus_three z\n"
                             "SKILL: bogus_four w\n"),
                    duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    lead = HungryLead()
    svc = _svc(tmp_path, lead, panel=[])
    session = svc.run("examine the project and recommend improvements to its design", source="test")
    assert session.status == SessionStatus.done
    assert lead.followup_prompt is not None
    # all four unknown-skill results were fed back (old cap stopped at two)
    assert lead.followup_prompt.count("unknown skill") == 4


def test_analysis_read_results_get_more_room(tmp_path):
    """The live truncation bug: 'reading' a big file meant seeing its first
    2000 chars, and the lead reasoned wrongly from the fragment. Analysis
    tasks now get 8000-char reads."""
    est = tmp_path / "proj"
    est.mkdir()
    (est / "big.txt").write_text("x" * 12000, encoding="utf-8")

    class ReadingLead:
        name = "mock"

        def __init__(self):
            self.followup = None
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                if "Skill results" in prompt:
                    self.followup = prompt
                    return AdapterResult(content="Read it. Analysis done.\nROUND: DONE", duration_ms=1)
                return AdapterResult(content="SKILL: read_file big.txt", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    lead = ReadingLead()
    svc = GangOf8Service(data_dir=tmp_path / "data", panel=[])
    svc.registry.register(lead)
    session = svc.run(f'examine and evaluate the project in "{est}"', source="test")
    assert session.status == SessionStatus.done
    assert session.classification.task_type.value in ("research", "question")  # both analysis
    assert lead.followup is not None
    assert "x" * config.SKILL_RESULT_ANALYSIS_MAX_CHARS in lead.followup, "deep read fed back"
    assert "x" * (config.SKILL_RESULT_ANALYSIS_MAX_CHARS + 1) not in lead.followup, "still capped"


# --- lead synthesis as the final answer --------------------------------------------


SUBSTANTIAL_SYNTHESIS = (
    "## Verdict first\n\nThe panel misdiagnosed two of three points; here is the "
    "grounded reconciliation with evidence and a concrete plan.\n\n"
    + "Detailed reasoning that weighs each panel view on its merits. " * 40
    + "\nROUND: DONE"
)


class SubstantialLead:
    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            return AdapterResult(content=SUBSTANTIAL_SYNTHESIS, duration_ms=1)
        if role == Role.summarizer:
            raise AssertionError("the summarizer must not run — the synthesis IS the answer")
        return self._inner.call(role, prompt, timeout_s)


def test_substantial_done_synthesis_is_the_final_answer(tmp_path):
    svc = _svc(tmp_path, SubstantialLead())
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert "Verdict first" in session.final.answer
    assert "ROUND" not in session.final.answer, "the control marker is stripped"
    assert session.final.confidence == "high", "panel seats contributed to what the lead weighed"
    assert not any(c.role == Role.summarizer for c in session.contributions)
    assert "synthesis_final" in _events(svc, session)


def test_solo_synthesis_final_is_medium_confidence(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(SubstantialLead())
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert "Verdict first" in session.final.answer
    assert session.final.confidence == "medium", "no panel weighed in — one model's view"


def test_thin_synthesis_still_composes_via_summarizer(tmp_path):
    # the default mock lead's short answer must NOT bypass the summarizer
    svc = GangOf8Service(data_dir=tmp_path)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert any(c.role == Role.summarizer for c in session.contributions)
    assert "synthesis_final" not in _events(svc, session)


# --- panel derivation & degradation ----------------------------------------------


def test_settings_roster_filtered_to_registered_seats(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    svc.settings.panel_seats = ["mock", "ghost"]
    assert svc._effective_panel() == ["mock"], "unregistered seats are dropped"


def test_cli_panel_degrades_without_openrouter_key(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: f"/bin/{n}" if n in ("claude", "codex") else None)
    svc = GangOf8Service(data_dir=tmp_path, backend="cli")
    assert svc.panel == ["claude", "codex"], "installed CLIs only; no OpenRouter without a key"


def test_cli_panel_appends_enabled_keyed_openrouter_seats(tmp_path, monkeypatch):
    import shutil

    # Budget seats join the default panel only in council mode since
    # ARCHITECTURE-REVIEW.md Phase 1; duo is the default.
    monkeypatch.setattr(config, "PANEL_MODE", "council")
    monkeypatch.setattr(shutil, "which", lambda n: f"/bin/{n}" if n in ("claude", "codex") else None)
    svc = GangOf8Service(data_dir=tmp_path, backend="cli")
    svc.secrets.set("openrouter", "sk-test-key")
    svc.settings.openrouter_enabled = {"deepseek": True, "glm": False}
    svc._apply_settings(backend="cli")
    assert svc.panel == ["claude", "codex", "deepseek"]


def test_lead_recall_after_skill_results_keeps_the_lead_timeout(tmp_path):
    """Live failure: 'modify the existing game' had the lead read index.html,
    then time out at 120s regenerating it — the skill-resolution re-call ran on
    the generic specialist timeout instead of LEAD_TIMEOUT. Both lead calls
    must carry the long timeout."""

    class TimeoutProbe:
        name = "mock"

        def __init__(self):
            self.lead_timeouts = []
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                self.lead_timeouts.append(timeout_s)
                if "Skill results" in prompt:
                    return AdapterResult(content="Done with the file.\nROUND: DONE", duration_ms=1)
                return AdapterResult(content="SKILL: bogus x", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    probe = TimeoutProbe()
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(probe)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert len(probe.lead_timeouts) == 2, "initial call + skill-results re-call"
    assert all(t == config.LEAD_TIMEOUT for t in probe.lead_timeouts), \
        f"every lead call gets the lead timeout, got {probe.lead_timeouts}"


# --- delegated RESULT block survives folding ----------------------------------------


def test_split_result_block():
    pre, block = rounds.split_result_block("long analysis...\nRESULT:\nfinding: X\nconfidence: high")
    assert pre == "long analysis...\n"
    assert block.startswith("RESULT:")
    assert rounds.split_result_block("no block here") == ("no block here", "")


def test_delegated_result_block_survives_truncation(tmp_path):
    """The conclusion used to be exactly what end-truncation cut off. A reply
    whose preamble alone exceeds the cap keeps its RESULT: block whole."""
    long_reply = ("preamble waffle. " * 300  # ~5100 chars, well past the 2500 cap
                  + "\nRESULT:\nfinding: SQLite wins decisively.\n"
                    "artifacts: none\nconfidence: high")

    class ConsultingLead:
        name = "mock"

        def __init__(self):
            self.asked = False
            self.followup = None
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.architect:
                return AdapterResult(content=long_reply, duration_ms=1)
            if role == Role.lead:
                if not self.asked:
                    self.asked = True
                    return AdapterResult(content="CONSULT: architect - which store?", duration_ms=1)
                self.followup = prompt
                return AdapterResult(content="Done.\nROUND: DONE", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    lead = ConsultingLead()
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(lead)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert lead.followup is not None
    assert "finding: SQLite wins decisively." in lead.followup, "conclusion survived"
    assert "confidence: high" in lead.followup
    # and the delegate was told about the contract
    assert config.DELEGATION_RESULT_MAX_CHARS >= len(
        "RESULT:\nfinding: SQLite wins decisively.\nartifacts: none\nconfidence: high")


def test_delegation_shape_scales_with_complexity():
    from gangof8.config import budgets_for
    from gangof8.models import Complexity

    assert budgets_for(Complexity.trivial).max_delegation_depth == 1
    assert budgets_for(Complexity.standard).max_delegation_depth == 2
    assert budgets_for(Complexity.complex).max_delegation_depth == 3
    assert budgets_for(Complexity.complex).max_delegations == 6


# --- model attribution ---------------------------------------------------------------


def test_contributions_record_the_model_that_produced_them(tmp_path):
    class NamedModelSeat:
        name = "alpha"

        def call(self, role, prompt, timeout_s, images=None):
            return AdapterResult(content="my take", model="alpha-9000", duration_ms=1)

    svc = GangOf8Service(data_dir=tmp_path, panel=["alpha"])
    svc.registry.register(NamedModelSeat())
    session = svc.run(TASK, source="test")
    panelist = next(c for c in session.contributions if c.role == Role.panelist)
    assert panelist.model == "alpha-9000"
    # the mock adapter reports no model — attribution stays honest, not invented
    lead = next(c for c in session.contributions if c.role == Role.lead)
    assert lead.model is None


def test_cli_model_pins_flow_from_settings_to_adapters(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: f"/bin/{n}")
    svc = GangOf8Service(data_dir=tmp_path, backend="cli")
    svc.update_settings({"backend": "cli",
                         "cli_models": {"claude": "claude-opus-4-8", "gemini": "gemini-2.5-pro"}})
    assert svc.registry._adapters["claude"].model == "claude-opus-4-8"
    assert svc.registry._adapters["gemini"].model == "gemini-2.5-pro"
    assert svc.registry._adapters["codex"].model is None, "unpinned seat keeps its CLI default"
    seats = {s["name"]: s for s in svc.seats()["seats"] if s["kind"] == "cli"}
    assert seats["claude"]["model"] == "claude-opus-4-8"
    assert seats["codex"]["model"] is None
    # each vendor seat carries its dropdown catalog of reasoning models
    for name in ("claude", "codex", "gemini"):
        assert seats[name]["models"], f"{name} should offer a model catalog"
    assert "opus" in seats["claude"]["models"]
    assert any(m.startswith("gemini-") for m in seats["gemini"]["models"])


# --- live model catalog ---------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


_PUBLIC_CATALOG = {"data": [
    {"id": "anthropic/claude-fable-5:free", "created": 400},   # variant → base id
    {"id": "anthropic/claude-fable-5", "created": 300},        # deduped
    {"id": "anthropic/claude-opus-4-8", "created": 200},
    {"id": "openai/gpt-5.1-codex-max", "created": 250},
    {"id": "openai/whisper-large-v4", "created": 999},         # not a reasoning model
    {"id": "google/gemini-3-pro", "created": 100},
    {"id": "mistralai/mistral-large", "created": 500},         # not one of our CLI vendors
]}


def _catalog_service(tmp_path, monkeypatch, get=None):
    import httpx

    monkeypatch.setattr(config, "WEB_ENABLED", True)
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        if get is not None:
            return get(url)
        return _FakeResp(_PUBLIC_CATALOG)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(GangOf8Service, "_gemini_sdk_models", lambda self: [])
    return GangOf8Service(data_dir=tmp_path), calls


def test_live_catalog_groups_by_vendor_newest_first(tmp_path, monkeypatch):
    svc, _ = _catalog_service(tmp_path, monkeypatch)
    cat = svc.cli_model_catalog()
    # tier aliases stay on top (never stale), then live models newest-first
    assert cat["claude"][:3] == ["opus", "sonnet", "haiku"]
    live = cat["claude"][3:]
    assert live.index("claude-fable-5") < live.index("claude-opus-4-8")
    assert cat["claude"].count("claude-fable-5") == 1, ":free variant deduped to base id"
    assert "gpt-5.1-codex-max" in cat["codex"]
    assert not any("whisper" in m for m in cat["codex"]), "non-reasoning families excluded"
    assert "gemini-3-pro" in cat["gemini"]
    assert not any("mistral" in m for models in cat.values() for m in models)


def test_catalog_failure_falls_back_to_static(tmp_path, monkeypatch):
    def boom(url):
        raise OSError("offline")

    svc, _ = _catalog_service(tmp_path, monkeypatch, get=boom)
    cat = svc.cli_model_catalog()
    assert cat == config.CLI_MODEL_CATALOG, "Settings never breaks offline"
    assert "claude-fable-5" in cat["claude"], "fallback still knows the newest family"


def test_catalog_cached_until_refresh(tmp_path, monkeypatch):
    svc, calls = _catalog_service(tmp_path, monkeypatch)
    svc.cli_model_catalog()
    svc.cli_model_catalog()
    assert calls["n"] == 1, "second call served from cache"
    svc.cli_model_catalog(refresh=True)
    assert calls["n"] == 2, "?refresh=1 refetches"


def test_web_disabled_never_fetches(tmp_path, monkeypatch):
    import httpx

    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        return _FakeResp(_PUBLIC_CATALOG)

    monkeypatch.setattr(httpx, "get", fake_get)  # WEB stays off via conftest
    svc = GangOf8Service(data_dir=tmp_path)
    assert svc.cli_model_catalog() == config.CLI_MODEL_CATALOG
    assert calls["n"] == 0


# --- disk back-compat --------------------------------------------------------------


def test_old_session_dict_without_new_fields_still_validates():
    data = {
        "session_id": "s_old",
        "task": {"task_id": "t_old", "session_id": "s_old", "text": "legacy task"},
    }
    s = Session.model_validate(data)
    assert s.panel == []
    assert s.consent_extra_rounds == 0
    assert s.compose_now is False


def test_legacy_establish_target_input_still_answerable(tmp_path):
    """A session paused on the OLD up-front greenfield question (written to disk
    before the promote-time ask replaced it) must still resume."""
    svc = GangOf8Service(data_dir=tmp_path)
    session = svc._open("build something new", "test", None)
    from gangof8.models import InputRequest

    session.classification = None  # pre-deliberation pause, as the old gate left it
    req = InputRequest(
        session_id=session.session_id, agent="system", role=Role.coordinator,
        purpose="establish_target", resume_token="",
        question="Where should the finished files go?")
    session.input_requests.append(req)
    session.status = SessionStatus.awaiting_input
    svc.store.save_session(session)

    resumed = svc.answer(session.session_id, req.input_id, "workspace")
    assert resumed.status == SessionStatus.failed
    assert resumed.outcome == "failed_verification"
    assert resumed.established_asked is True


def test_release_prompt_mandates_repairs_including_whole_file_rewrites():
    """Phase 2 repair mandate: the release engineer must ship fixes for every
    fixable FAIL — surgical EDITs or complete ARTIFACT rewrites — and may only
    leave a defect unrepaired by stating it requires the owner's rebuild."""
    from gangof8.models import Session, Task
    session = Session(
        session_id="s_prompt", task=Task(
            task_id="t", session_id="s_prompt", text="build a game"))
    prompt = rounds.frontier_release_prompt(
        session, [("game.html", "<html></html>")], defect_register=[])
    assert "REPAIR MANDATE" in prompt
    assert "ARTIFACT:" in prompt
    assert "END_ARTIFACT" in prompt
    assert "requires owner rebuild" in prompt
    # the confirmation pass stays a clean-room re-inspection, not a repair pass
    confirm = rounds.frontier_release_prompt(
        session, [("game.html", "<html></html>")], repair_attempt=1)
    assert "REPAIR MANDATE" not in confirm
    assert "re-inspect the resulting files from scratch" in confirm
