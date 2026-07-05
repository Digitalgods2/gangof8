"""Best-of-N selection (owner directive 2026-07-05: "true best-of-N").

Every panel seat authors a complete candidate; independent judges score them
blindly; the highest-scoring FILE ships — a real model's code, not a lead
re-author. Winner ships as-is, with optional surgical fixes for judge-flagged
defects.
"""

import pytest

from conclave_os import config, loop, rounds
from conclave_os.logstore import LogStore
from conclave_os.models import (Classification, Complexity, Council,
                                CouncilMember, Contribution, ProposedAction,
                                Risk, Role, TaskType)
from conclave_os.sessions import SessionManager

# Both RUN clean under the headless smoke gate (best-of-N executes every
# candidate before judging). "weak"/"strong" differ only so the mock judges can
# prefer one; the scoring is driven by the judge reply, not the content.
WEAK = "<!doctype html><html><body>weak<script>var w=1;</script></body></html>"
STRONG = "<!doctype html><html><body>strong<script>var s=1; var extra=2;</script></body></html>"
# a candidate that throws on load (kimi's exact bug: empty-array element access)
# — must be disqualified before it can win, no matter how it reads
BROKEN = "<!doctype html><html><body><script>var g=[]; g[0].x=1;</script></body></html>"


@pytest.fixture()
def store(tmp_path) -> LogStore:
    return LogStore(tmp_path)


@pytest.fixture()
def session(store):
    s = SessionManager(store).create("build game.html in full", source="test")
    s.classification = Classification(task_type=TaskType.code,
                                      complexity=Complexity.standard, risk=Risk.none,
                                      produces_output=True)
    return s


def _candidate(session, agent, base, content):
    fn = f"{agent}__{base}"
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.panelist,
        filename=fn, content=content, args={"filename": fn, "content": content}))


# --- the parser (pure) --------------------------------------------------------


def test_parse_scores_winner_and_defects():
    text = ("SCORE Candidate 1: 4\nSCORE Candidate 2: 9\nSCORE Candidate 3: 7\n"
            "WINNER: Candidate 2\nDEFECT: audio not unlocked on first gesture\n"
            "DEFECT: bullet cooldown missing")
    scores, winner, defects = rounds.parse_candidate_scores(text, 3)
    assert scores == {1: 4, 2: 9, 3: 7}
    assert winner == 2
    assert defects == ["audio not unlocked on first gesture", "bullet cooldown missing"]


def test_parse_scores_clamps_and_ignores_out_of_range():
    scores, winner, _ = rounds.parse_candidate_scores(
        "SCORE Candidate 1: 99\nSCORE Candidate 5: 3\nWINNER: Candidate 9", 2)
    assert scores == {1: config.JUDGE_SCORE_MAX}, "clamped; candidate 5 out of range dropped"
    # out-of-range WINNER line is ignored; falls back to the top in-range score
    assert winner == 1


def test_parse_scores_defaults_winner_to_top_when_no_winner_line():
    scores, winner, _ = rounds.parse_candidate_scores(
        "SCORE Candidate 1: 3\nSCORE Candidate 2: 8", 2)
    assert winner == 2


# --- candidate collection + grouping -----------------------------------------


def test_collect_candidates_only_namespaced_panel_files(session):
    _candidate(session, "codex", "game.html", STRONG)
    _candidate(session, "gemini", "game.html", WEAK)
    # a lead/implementer write is NOT a candidate; an empty one is skipped
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="game.html", content="x", args={"filename": "game.html", "content": "x"}))
    _candidate(session, "qwen", "game.html", "   ")
    cands = loop._collect_candidates(session)
    assert sorted(c["agent"] for c in cands) == ["codex", "gemini"]


def test_dominant_base_group_prefers_established_revision_name(tmp_path, session):
    est = tmp_path / "est"
    est.mkdir()
    (est / "index.html").write_text("old", encoding="utf-8")
    session.established_root = str(est)
    _candidate(session, "codex", "index.html", STRONG)
    _candidate(session, "gemini", "index.html", WEAK)
    _candidate(session, "kimi", "centipede.html", STRONG)  # a different base name
    group = loop._dominant_base_group(session, loop._collect_candidates(session))
    assert {c["base"] for c in group} == {"index.html"}, "the revision target wins the group"


# --- end to end ---------------------------------------------------------------


def _bon(session, judges, judge_reply, lead=None):
    council = Council(members=[lead or CouncilMember(role=Role.lead, agent="claude", active=True)] + judges)
    session.council = council

    def call(member, prompt):
        # judges see blind labels, never agent names
        assert "codex__" not in prompt and "gemini__" not in prompt
        return Contribution(round=0, role=member.role, agent=member.agent, content=judge_reply)

    def lead_call(member, prompt):
        return Contribution(round=0, role=member.role, agent=member.agent, content="")

    return council, call, lead_call


def test_winner_is_highest_scored_and_ships(session, store):
    # sorted by namespaced name: aaa__game.html=Candidate 1, zzz__game.html=Candidate 2
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1"),
              CouncilMember(role=Role.panelist, agent="j2")]
    council, call, lead_call = _bon(
        session, judges, "SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2")

    out = loop._run_best_of_n(session, council, judges, call, lead_call, store)

    assert out["agent"] == "zzz" and out["file"] == "game.html"
    assert out["judges"] == 2 and out["candidates"] == 2
    writes = [a for a in session.proposed_actions
              if a.kind == "write_file" and a.role == Role.implementer]
    assert len(writes) == 1
    assert writes[0].filename == "game.html" and writes[0].content == STRONG
    assert any(a.kind == "promote" and a.filename == "game.html"
               for a in session.proposed_actions), "winner proposed for delivery"


def test_verification_fails_a_web_file_that_does_not_run(tmp_path, session, store):
    """The delivery gate: a promoted-bound web file that throws on load fails
    verification, so it can never be reported as success (and its promote is
    stripped in _deliberate). A running one passes."""
    import shutil as _sh
    if _sh.which("node") is None:
        pytest.skip("node not on PATH")
    from conclave_os.models import ProposedAction

    broken = tmp_path / "broken.html"
    broken.write_text(BROKEN, encoding="utf-8")
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="broken.html", status="executed", result_path=str(broken),
        args={"filename": "broken.html"}))
    assert loop._verify_artifact_outputs(session, store, require_file=True) is False
    assert any("does not run" in u for u in session.unresolved)

    good = tmp_path / "good.html"
    good.write_text(STRONG, encoding="utf-8")
    s2 = SessionManager(store).create("build good.html", source="test")
    s2.proposed_actions.append(ProposedAction(
        session_id=s2.session_id, kind="write_file", role=Role.implementer,
        filename="good.html", status="executed", result_path=str(good),
        args={"filename": "good.html"}))
    assert loop._verify_artifact_outputs(s2, store, require_file=True) is True


def test_too_few_candidates_falls_back(session, store):
    _candidate(session, "solo", "game.html", STRONG)  # only one
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    council, call, lead_call = _bon(session, judges, "SCORE Candidate 1: 9\nWINNER: Candidate 1")
    assert loop._run_best_of_n(session, council, judges, call, lead_call, store) is None
    assert not any(a.role == Role.implementer for a in session.proposed_actions)


def test_all_judges_failing_falls_back(session, store):
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    council, _, lead_call = _bon(session, judges, "no scores here at all")
    council.members[0].active = True

    def call(member, prompt):
        return Contribution(round=0, role=member.role, agent=member.agent, content="garbage")

    assert loop._run_best_of_n(session, council, judges, call, lead_call, store) is None


def test_broken_candidate_disqualified_sole_runner_wins(session, store):
    """THE fix: a candidate that crashes on load is disqualified before judging;
    when it leaves one runner, that runner ships without a vote. (Live: 5 judges
    unanimously picked the one crasher over two working files.)"""
    _candidate(session, "polished", "game.html", BROKEN)  # reads great, crashes
    _candidate(session, "works", "game.html", STRONG)      # runs
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    # judges would be TEMPTED to pick the broken one, but it never reaches them
    council, call, lead_call = _bon(session, judges,
                                    "SCORE Candidate 1: 9\nWINNER: Candidate 1")
    out = loop._run_best_of_n(session, council, judges, call, lead_call, store)
    assert out["agent"] == "works", "the crasher can't win; the runner ships"
    assert out["judges"] == 0, "sole survivor — no vote needed"
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    assert shipped.content == STRONG


def test_all_candidates_broken_returns_none(session, store):
    _candidate(session, "a", "game.html", BROKEN)
    _candidate(session, "b", "game.html", BROKEN)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    council, call, lead_call = _bon(session, judges, "SCORE Candidate 1: 9\nWINNER: Candidate 1")
    assert loop._run_best_of_n(session, council, judges, call, lead_call, store) is None
    assert not any(a.role == Role.implementer for a in session.proposed_actions)


def test_winner_gets_surgical_fixes_when_defects_flagged(session, store):
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    lead = CouncilMember(role=Role.lead, agent="claude", active=True)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    council = Council(members=[lead] + judges)
    session.council = council
    reply = ("SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2\n"
             "DEFECT: rename extra to reviewed")

    def call(member, prompt):
        return Contribution(round=0, role=member.role, agent=member.agent, content=reply)

    def lead_call(member, prompt):
        # surgical EDIT that keeps the file runnable, applied in-memory to the winner
        return Contribution(round=0, role=member.role, agent=member.agent,
                            content="EDIT: game.html\n<<<<<<< OLD\nvar extra=2;\n"
                                    "=======\nvar reviewed=2;\n>>>>>>> NEW\n")

    out = loop._run_best_of_n(session, council, judges, call, lead_call, store)
    assert out["fixes"] == 1
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    assert "var reviewed=2;" in shipped.content and "var extra=2;" not in shipped.content
