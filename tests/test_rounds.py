"""Panel rounds: parallel multi-seat fan-out, lead synthesis with ROUND:
DONE/CONTINUE, and the every-n-rounds human consent gate. Rotation is
automatic; the human is asked before each additional block of rounds."""

import json
import threading
import time

import pytest

from conclave_os import config, rounds
from conclave_os.adapters.mock import MockAdapter
from conclave_os.models import Role, Session, SessionStatus, Task
from conclave_os.registry import AdapterResult, AgentError
from conclave_os.service import ConclaveService

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
    svc = ConclaveService(data_dir=tmp_path, **kwargs)
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
    svc = ConclaveService(data_dir=tmp_path, panel=["alpha", "beta", "gamma"])
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

    svc = ConclaveService(data_dir=tmp_path, panel=["mock", "boom"])
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
    from conclave_os.models import Budgets

    shared = _SharedCounter()
    svc = ConclaveService(data_dir=tmp_path, panel=["alpha", "beta", "gamma"])
    for name in ("alpha", "beta", "gamma"):
        svc.registry.register(ProbeSeat(name, shared))
    budgets = Budgets(max_rounds=4, max_turns_per_round=1, max_agent_calls=2, max_wall_seconds=60)
    session = svc.run(TASK, source="test", budgets=budgets)
    assert session.status == SessionStatus.done, "budget exhaustion degrades, never crashes"
    assert session.agent_calls <= budgets.max_agent_calls
    assert shared.calls <= budgets.max_agent_calls, "no oversubscription past the reserve"


def test_solo_mode_with_empty_panel(tmp_path):
    svc = ConclaveService(data_dir=tmp_path, panel=[])
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
    assert rounds.synthesis_is_stub(LIVE_STUB) is True


def test_short_direct_answer_is_not_a_stub():
    # the mock lead's legitimate short answer has no deferral phrasing
    from conclave_os.adapters.mock import LEAD_ANSWER

    assert rounds.synthesis_is_stub(LEAD_ANSWER) is False
    assert rounds.synthesis_is_stub("SQLite. It is the safer default.") is False


def test_marker_lines_are_never_stubs():
    assert rounds.synthesis_is_stub("I'll consult a specialist first.\nCONSULT: architect - layout?") is False
    assert rounds.synthesis_is_stub("Let me check the file.\nSKILL: read_file main.py") is False
    assert rounds.synthesis_is_stub("I'll wrap up here.\nROUND: DONE") is False


def test_long_reply_is_not_a_stub():
    assert rounds.synthesis_is_stub("I'll now explain in detail. " + "Substance. " * 60) is False


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


# --- task-aware skill-request cap --------------------------------------------------


def test_analysis_tasks_get_a_higher_skill_cap(tmp_path):
    from conclave_os import loop
    from conclave_os.classifier import classify
    from conclave_os.logstore import LogStore
    from conclave_os.sessions import SessionManager

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


# --- panel derivation & degradation ----------------------------------------------


def test_settings_roster_filtered_to_registered_seats(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.settings.panel_seats = ["mock", "ghost"]
    assert svc._effective_panel() == ["mock"], "unregistered seats are dropped"


def test_cli_panel_degrades_without_openrouter_key(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: f"/bin/{n}" if n in ("claude", "codex") else None)
    svc = ConclaveService(data_dir=tmp_path, backend="cli")
    assert svc.panel == ["claude", "codex"], "installed CLIs only; no OpenRouter without a key"


def test_cli_panel_appends_enabled_keyed_openrouter_seats(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: f"/bin/{n}" if n in ("claude", "codex") else None)
    svc = ConclaveService(data_dir=tmp_path, backend="cli")
    svc.secrets.set("openrouter", "sk-test-key")
    svc.settings.openrouter_enabled = {"deepseek": True, "glm": False}
    svc._apply_settings(backend="cli")
    assert svc.panel == ["claude", "codex", "deepseek"]


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
    svc = ConclaveService(data_dir=tmp_path)
    session = svc._open("build something new", "test", None)
    from conclave_os.models import InputRequest

    session.classification = None  # pre-deliberation pause, as the old gate left it
    req = InputRequest(
        session_id=session.session_id, agent="system", role=Role.coordinator,
        purpose="establish_target", resume_token="",
        question="Where should the finished files go?")
    session.input_requests.append(req)
    session.status = SessionStatus.awaiting_input
    svc.store.save_session(session)

    resumed = svc.answer(session.session_id, req.input_id, "workspace")
    assert resumed.status == SessionStatus.done
    assert resumed.established_asked is True
