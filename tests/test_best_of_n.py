"""Best-of-N selection (owner directive 2026-07-05: "true best-of-N").

Every panel seat authors a complete candidate; independent judges score them
blindly; the highest-scoring FILE ships — a real model's code, not a lead
re-author. Winner ships as-is, with optional surgical fixes for judge-flagged
defects.
"""

import pytest

from gangof8 import config, loop, rounds, smoke
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import (Classification, Complexity, Council,
                                CouncilMember, Contribution, ProposedAction,
                                Risk, Role, TaskType)
from gangof8.sessions import SessionManager

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


def test_divergent_single_file_names_are_all_judged(session):
    """Regression: seats that named their ONE candidate differently (the task
    invited an author-chosen title) were fractured by filename and the minority
    group was silently dropped — discarding 3 of 5 legitimate story candidates.
    A single file per seat ⇒ every seat's take competes."""
    _candidate(session, "aaa", "First Car Ride.txt", "story A" * 20)
    _candidate(session, "bbb", "First Car Ride.txt", "story B" * 20)
    _candidate(session, "ccc", "Big Ride.txt", "story C" * 20)
    _candidate(session, "ddd", "Big Ride.txt", "story D" * 20)
    _candidate(session, "eee", "Big Ride.txt", "story E" * 20)
    judged, dropped = loop._candidate_pool(session, loop._collect_candidates(session))
    assert dropped == []
    assert len(judged) == 5


def test_multi_file_project_falls_back_to_dominant_group(session):
    """When a seat produced MULTIPLE files (a real multi-file build), filenames
    are meaningful identities — keep the dominant-base group so we ship one
    coherent deliverable, not a css judged against an html."""
    _candidate(session, "aaa", "index.html", STRONG)
    _candidate(session, "aaa", "styles.css", "body{color:red}")  # aaa = two files
    _candidate(session, "bbb", "index.html", WEAK)
    judged, dropped = loop._candidate_pool(session, loop._collect_candidates(session))
    assert {c["base"] for c in judged} == {"index.html"}
    assert {c["base"] for c in dropped} == {"styles.css"}


# --- end to end ---------------------------------------------------------------


def _bon(session, judges, judge_reply, lead=None):
    council = Council(members=[lead or CouncilMember(role=Role.lead, agent="claude", active=True)] + judges)
    session.council = council

    def call(member, prompt, timeout_s=None):
        # judges see blind labels, never agent names
        assert "codex__" not in prompt and "gemini__" not in prompt
        return Contribution(round=0, role=member.role, agent=member.agent, content=judge_reply)

    def lead_call(member, prompt):
        return Contribution(round=0, role=member.role, agent=member.agent, content="")

    return council, call, lead_call


def test_prose_judge_prompt_omits_runtime_framing(session):
    """Fix B: prose candidates carry no runtime note, so the judge prompt must NOT
    tell judges to weigh 'animate/respond under simulated play' or 'static screen
    scores LOW' — the framing that made a judge invent an on-screen-rendering
    defect for a .txt story. A runtime-bearing candidate still gets it."""
    prose = [("Candidate 1", "Benny loved three things...", ""),
             ("Candidate 2", "Benny sniffed the car...", "")]
    p = rounds.score_candidates_prompt(session, prose)
    assert "RUNTIME" not in p and "animate" not in p.lower()

    runtime = [("Candidate 1", "<html>..", "runs and ANIMATES under play"),
               ("Candidate 2", "<html>..", "throws on load")]
    p2 = rounds.score_candidates_prompt(session, runtime)
    assert "RUNTIME" in p2 and "throws on load" in p2


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
    from gangof8.models import ProposedAction

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


def _finalists(weak=WEAK, strong=STRONG):
    """ordered candidates for _chair_finish: Candidate 1 = weak, Candidate 2 =
    strong; the vote (agg/votes) favors Candidate 2."""
    ordered = [{"content": weak, "agent": "aaa", "base": "game.html"},
               {"content": strong, "agent": "zzz", "base": "game.html"}]
    return ordered, {1: 4, 2: 9}, {1: 0, 2: 2}, {1: [], 2: []}


def test_integration_offer_rides_the_chair_pass_and_is_runtime_validated(session, store):
    """A merge is an offered alternative, not an automatic replacement — and it
    arrives inside the SAME chair reply that ratifies the vote (no separate
    integration call)."""
    session.integration_review_enabled = True
    ordered, agg, votes, defects = _finalists()
    session.council = Council(members=[CouncilMember(role=Role.summarizer, agent="codifier", active=True)])
    seen = {}

    def call(member, prompt, timeout_s=None):
        seen["prompt"] = prompt
        return Contribution(
            round=0, role=member.role, agent=member.agent,
            content=("RATIFY: Candidate 2\nDEFECT: none\n"
                     "SYNERGY: YES\nRATIONALE: keep the winner's stable loop and "
                     "adopt Candidate 1's clearer controls\nSOURCES: Candidate 1, Candidate 2\n"
                     "ARTIFACT: game.html\n" + STRONG.replace("extra=2", "extra=3")),
        )

    wi, content, applied, action, proposal = loop._chair_finish(
        session, ordered, agg, votes, defects, 1, call, store)
    assert wi == 1 and action == "chair ratified the vote"
    assert proposal is not None
    assert proposal.content != STRONG
    assert proposal.source_candidates == ["Candidate 1", "Candidate 2"]
    assert "Evaluate EVERY candidate" in seen["prompt"]
    assert "RATIFY" in seen["prompt"], "one prompt carries decision + fixes + integration"


def test_integration_rejects_a_broken_merge(session, store):
    session.integration_review_enabled = True
    ordered, agg, votes, defects = _finalists()
    session.council = Council(members=[CouncilMember(role=Role.summarizer, agent="codifier", active=True)])

    def call(member, prompt, timeout_s=None):
        return Contribution(
            round=0, role=member.role, agent=member.agent,
            content=("RATIFY: Candidate 2\nDEFECT: none\n"
                     "SYNERGY: YES\nRATIONALE: unsafe merge\nSOURCES: Candidate 1\n"
                     "ARTIFACT: game.html\n" + BROKEN),
        )

    wi, content, applied, action, proposal = loop._chair_finish(
        session, ordered, agg, votes, defects, 1, call, store)
    assert proposal is None, "a merge that fails the runtime gate is not offered"
    assert wi == 1 and content == STRONG, "the ratified winner still ships"


def test_integration_resolves_a_read_skill_before_deciding(session, store, tmp_path):
    """A SKILL request is an intermediate step, not an automatic no-synergy vote."""
    session.integration_review_enabled = True
    session.council = Council(members=[CouncilMember(role=Role.summarizer, agent="codifier", active=True)])
    session.established_root = str(tmp_path)
    (tmp_path / "source.txt").write_text("source continuity", encoding="utf-8")
    ordered, agg, votes, defects = _finalists()
    calls = []

    def call(member, prompt, timeout_s=None):
        calls.append(prompt)
        if len(calls) == 1:
            return Contribution(round=0, role=member.role, agent=member.agent,
                                content="SKILL: read_file source.txt")
        assert "source continuity" in prompt
        return Contribution(
            round=0, role=member.role, agent=member.agent,
            content=("RATIFY: Candidate 2\nDEFECT: none\n"
                     "SYNERGY: YES\nRATIONALE: preserve source continuity\n"
                     "SOURCES: Candidate 1, Candidate 2\nARTIFACT: game.html\n"
                     + STRONG.replace("extra=2", "extra=3")),
        )

    wi, content, applied, action, proposal = loop._chair_finish(
        session, ordered, agg, votes, defects, 1, call, store,
        governance=Governance(store))
    assert proposal is not None
    assert len(calls) == 2


def test_goal_milestone_never_pauses_for_integration(store):
    """An unattended goal run must never stall mid-goal on a human merge
    decision: on a goal-milestone session the chair prompt drops the SYNERGY
    section entirely and no proposal is returned, even when integration review
    is enabled in settings."""
    s = SessionManager(store).create("build game.html in full", source="goal")
    s.classification = Classification(task_type=TaskType.code,
                                      complexity=Complexity.standard, risk=Risk.none,
                                      produces_output=True)
    s.integration_review_enabled = True
    s.council = Council(members=[CouncilMember(role=Role.summarizer, agent="codifier", active=True)])
    ordered, agg, votes, defects = _finalists()
    seen = {}

    def call(member, prompt, timeout_s=None):
        seen["prompt"] = prompt
        return Contribution(  # even a rogue SYNERGY offer must be ignored
            round=0, role=member.role, agent=member.agent,
            content=("RATIFY: Candidate 2\nDEFECT: none\n"
                     "SYNERGY: YES\nRATIONALE: rogue\nSOURCES: Candidate 1\n"
                     "ARTIFACT: game.html\n" + STRONG.replace("extra=2", "extra=3")),
        )

    wi, content, applied, action, proposal = loop._chair_finish(
        s, ordered, agg, votes, defects, 1, call, store)
    assert "SYNERGY" not in seen["prompt"], "goal milestones don't invite merge offers"
    assert proposal is None, "…and never surface one (no mid-goal human gate)"
    assert wi == 1 and action == "chair ratified the vote"


# --- parallel judge waves + unanimity early stop -------------------------------


def _judge_group():
    return [{"agent": "aaa", "base": "game.html", "namespaced": "aaa__game.html", "content": WEAK},
            {"agent": "zzz", "base": "game.html", "namespaced": "zzz__game.html", "content": STRONG}]


def test_unanimous_first_wave_skips_remaining_judges(session, store):
    """5 judges convened, but the first wave of JUDGE_FIRST_WAVE votes 3/3 for
    the same winner — the remaining judges (each a full re-read of the whole
    candidate corpus) are never called."""
    judges = [CouncilMember(role=Role.panelist, agent=f"j{i}") for i in range(5)]
    called = []

    def call(member, prompt, timeout_s=None):
        called.append(member.agent)
        return Contribution(round=0, role=member.role, agent=member.agent,
                            content="SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2")

    ordered, agg, votes, defects, judged = loop._score_candidates(
        session, judges, _judge_group(), call, store)
    assert judged == config.JUDGE_FIRST_WAVE
    assert len(called) == config.JUDGE_FIRST_WAVE, "unanimity stopped the vote early"
    assert votes[2] == config.JUDGE_FIRST_WAVE


def test_split_first_wave_convenes_every_judge(session, store):
    judges = [CouncilMember(role=Role.panelist, agent=f"j{i}") for i in range(5)]
    called = []

    def call(member, prompt, timeout_s=None):
        called.append(member.agent)
        pick = 1 if member.agent == "j0" else 2  # j0 dissents → wave 1 splits
        return Contribution(round=0, role=member.role, agent=member.agent,
                            content=f"SCORE Candidate 1: 5\nSCORE Candidate 2: 6\nWINNER: Candidate {pick}")

    ordered, agg, votes, defects, judged = loop._score_candidates(
        session, judges, _judge_group(), call, store)
    assert len(called) == 5, "a split vote runs the full bench"
    assert judged == 5 and votes[1] == 1 and votes[2] == 4


def test_lone_surviving_judge_does_not_early_stop(session, store):
    """One real vote isn't unanimity (JUDGE_EARLY_STOP_MIN_VOTES): if the rest
    of the first wave dropped, the remaining judges still run."""
    from gangof8.registry import AgentError

    judges = [CouncilMember(role=Role.panelist, agent=f"j{i}") for i in range(4)]
    called = []

    def call(member, prompt, timeout_s=None):
        called.append(member.agent)
        if member.agent in ("j1", "j2"):  # two of wave 1 drop
            raise AgentError(f"{member.agent} unavailable")
        return Contribution(round=0, role=member.role, agent=member.agent,
                            content="SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2")

    ordered, agg, votes, defects, judged = loop._score_candidates(
        session, judges, _judge_group(), call, store)
    assert len(called) == 4, "a 1-vote 'unanimous' wave still convenes the rest"
    assert judged == 2


def test_build_summary_uses_medium_confidence_after_council_drop(session, store, tmp_path):
    delivered = tmp_path / "out.txt"
    delivered.write_text("done", encoding="utf-8")
    session.unresolved.append("panel seat 'claude' dropped this round: not logged in")
    final = loop._build_summary_final(session, [ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="out.txt", status="executed", result_path=str(delivered),
    )])
    assert final.confidence == "medium"


def test_build_summary_uses_medium_confidence_after_preflight_exclusion(session, store, tmp_path):
    delivered = tmp_path / "out.txt"
    delivered.write_text("done", encoding="utf-8")
    session.unresolved.append("panel seat 'claude' unavailable before run: not logged in")
    final = loop._build_summary_final(session, [ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="out.txt", status="executed", result_path=str(delivered),
    )])
    assert final.confidence == "medium"


def test_all_judges_failing_falls_back(session, store):
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    council, _, lead_call = _bon(session, judges, "no scores here at all")
    council.members[0].active = True

    def call(member, prompt, timeout_s=None):
        return Contribution(round=0, role=member.role, agent=member.agent, content="garbage")

    assert loop._run_best_of_n(session, council, judges, call, lead_call, store) is None


def _chaired(session, judges, judge_reply, chair_reply, lead_agent="claude"):
    """A council whose lead (chair) answers `chair_reply` and whose judges
    answer `judge_reply`. Returns (council, call, lead_call)."""
    lead = CouncilMember(role=Role.lead, agent=lead_agent, active=True)
    council = Council(members=[lead] + judges)
    session.council = council

    def call(member, prompt, timeout_s=None):
        return Contribution(round=0, role=member.role, agent=member.agent, content=judge_reply)

    def lead_call(member, prompt):
        return Contribution(round=0, role=member.role, agent=member.agent, content=chair_reply)

    return council, call, lead_call


def test_judges_are_shown_runtime_evidence(session, store):
    """Judges score by reading; give them the RUNTIME result (does it run/animate)
    so a good-reading-but-dead candidate can't win a reading contest (live: the
    judges picked a barely-rendering game over ones that actually run)."""
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    lead = CouncilMember(role=Role.lead, agent="claude", active=True)
    council = Council(members=[lead] + judges)
    session.council = council
    seen = {}

    def call(m, p, timeout_s=None):
        seen["prompt"] = p
        return Contribution(round=0, role=m.role, agent=m.agent,
                            content="SCORE Candidate 1: 5\nSCORE Candidate 2: 9\nWINNER: Candidate 2")

    def codifier_call(m, p):
        return Contribution(round=0, role=m.role, agent=m.agent, content="RATIFY: Candidate 2\nDEFECT: none")

    loop._run_best_of_n(session, council, judges, call, codifier_call, store)
    assert "RUNTIME" in seen.get("prompt", ""), "the judge prompt carries per-candidate runtime evidence"


def test_chair_work_runs_on_the_codifier_summarizer(session, store):
    """Stage 3 runs on the strong CODIFIER (the Summarizer seat), not the fast
    lead — and it is ONE call: the merged chair pass delivers the ratify
    decision AND the surgical fixes in a single reply (the old flow spent three
    serial codifier calls re-reading the same candidate bodies)."""
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    lead = CouncilMember(role=Role.lead, agent="claude", active=True)
    summ = CouncilMember(role=Role.summarizer, agent="gemini", active=True)
    council = Council(members=[lead, summ] + judges)
    session.council = council
    codifier_agents = []

    def call(m, p, timeout_s=None):  # judges pick a winner and flag a fixable defect
        return Contribution(round=0, role=m.role, agent=m.agent,
                            content="SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2\n"
                                    "DEFECT: rename extra to reviewed")

    def codifier_call(m, p):  # the ONE merged chair pass: decision + fixes together
        codifier_agents.append(m.agent)
        assert "RATIFY" in p, "the merged prompt asks for the RATIFY/OVERRIDE decision"
        assert "EDIT" in p, "…and for the surgical fixes in the same reply"
        return Contribution(round=0, role=m.role, agent=m.agent,
                            content="RATIFY: Candidate 2\nDEFECT: extra should be reviewed\n"
                                    "EDIT: game.html\n<<<<<<< OLD\nvar extra=2;\n"
                                    "=======\nvar reviewed=2;\n>>>>>>> NEW\n")

    out = loop._run_best_of_n(session, council, judges, call, codifier_call, store)
    assert codifier_agents == ["gemini"], \
        "the chair's review AND fix ran as ONE call on the summarizer/codifier"
    assert out["fixes"] == 1
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    assert "var reviewed=2;" in shipped.content


def test_chair_ratifies_the_vote(session, store):
    """The lead CHAIRS the blind vote — here it ratifies, so the vote's winner
    (Candidate 2 / zzz / STRONG) ships, credited to that model."""
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1"),
              CouncilMember(role=Role.panelist, agent="j2")]
    council, call, lead_call = _chaired(
        session, judges, "SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2",
        chair_reply="RATIFY: Candidate 2\nDEFECT: none")
    out = loop._run_best_of_n(session, council, judges, call, lead_call, store)
    assert out["agent"] == "zzz" and out["chair"] == "chair ratified the vote"
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    assert shipped.content == STRONG


def test_chair_overrides_the_vote_to_the_runner_up(session, store):
    """When the chair judges the runner-up actually better, it OVERRIDES — the
    lead's arbitration beats the raw tally. Vote winner is Candidate 2 (zzz);
    the chair overrides to Candidate 1 (aaa)."""
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    judges = [CouncilMember(role=Role.panelist, agent="j1"),
              CouncilMember(role=Role.panelist, agent="j2")]
    council, call, lead_call = _chaired(
        session, judges, "SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2",
        chair_reply="OVERRIDE: Candidate 1 - the vote winner has a subtle off-by-one the judges missed")
    out = loop._run_best_of_n(session, council, judges, call, lead_call, store)
    assert out["agent"] == "aaa", "chair overrode to the runner-up"
    assert "overrode" in out["chair"]
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    assert shipped.content == WEAK


def test_chair_recovers_when_every_candidate_crashes(session, store):
    """No candidate runs → the chair repairs the most complete attempt to run,
    instead of discarding the panel's work. The recovered file ships, credited
    as chair-repaired, and is re-verified to actually run."""
    import shutil as _sh
    if _sh.which("node") is None:
        pytest.skip("node not on PATH")
    # both crash: empty-array access on load; the larger one is the recovery target
    small = "<!doctype html><html><body><script>var g=[]; g[0].x=1;</script></body></html>"
    big = "<!doctype html><html><body><!-- more complete --><script>\nvar grid=[];\n"\
          "grid[0].y = 2;\n</script></body></html>"
    _candidate(session, "small", "game.html", small)
    _candidate(session, "big", "game.html", big)
    lead = CouncilMember(role=Role.lead, agent="claude", active=True)
    council = Council(members=[lead])
    session.council = council

    def call(member, prompt, timeout_s=None):  # judges never reached (nothing runs)
        raise AssertionError("no judging when all candidates crash")

    def lead_call(member, prompt):
        # surgical fix: initialise the array so the load access is valid
        return Contribution(round=0, role=member.role, agent=member.agent,
                            content="EDIT: game.html\n<<<<<<< OLD\nvar grid=[];\n"
                                    "grid[0].y = 2;\n=======\nvar grid=[[0]];\n"
                                    "grid[0][0] = 2;\n>>>>>>> NEW\n")

    out = loop._run_best_of_n(session, council, [], call, lead_call, store)
    assert out is not None, "chair recovered rather than failing"
    assert "chair recovered" in out["chair"]
    assert out["agent"].startswith("big")
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    ran, _t, _d, _dyn = smoke.smoke_source(shipped.content, ".html")
    assert ran, "the recovered file actually runs"


def test_verification_failure_enters_bounded_artifact_repair(session, store):
    """Coordinator validation failures repair before a terminal failure result."""
    session.required_files = ["app.js"]
    lead = CouncilMember(role=Role.lead, agent="repair", active=True)
    session.council = Council(members=[lead])
    _candidate(session, "draft", "app.js", "var g=[]; g[0].x=1;")
    session.unresolved.append("artifact verification failed: app.js: does not run")

    def repair_call(member, prompt):
        assert member.agent == "repair"
        assert "artifact verification failed" in prompt
        return Contribution(round=0, role=Role.lead, agent="repair",
                            content="ARTIFACT: app.js\nvar answer = 42;\n")

    repaired = loop._repair_artifact_failure(
        session, SessionManager(store), Governance(store), store, repair_call)
    assert repaired is True
    assert session.artifact_repair_attempts == 1
    assert loop._verify_artifact_outputs(session, store, require_file=True) is True
    assert any(a.kind == "write_file" and a.role == Role.implementer
               and a.status == "executed" for a in session.proposed_actions)


def test_package_repair_stays_with_owner_and_exact_contract_path(session, store):
    session.collaboration_mode = "build_team"
    session.work_package_owner = "claude"
    session.required_files = ["src/games/asteroids.js"]
    owner = CouncilMember(role=Role.panelist, agent="claude", active=True)
    validator = CouncilMember(role=Role.summarizer, agent="gemini", active=True)
    session.council = Council(members=[owner, validator])
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="src/games/asteroids.js", content="broken source",
    ))
    session.unresolved.append(
        "artifact verification failed: src/games/asteroids.js: invalid token")
    seen = {}

    def repair_call(member, prompt):
        seen["agent"] = member.agent
        seen["prompt"] = prompt
        return Contribution(
            round=0, role=member.role, agent=member.agent,
            content=("ARTIFACT: src/games/asteroids.js\n"
                     "globalThis.Asteroids = function Asteroids() {};\n"
                     "END_ARTIFACT\n"),
        )

    assert loop._repair_artifact_failure(
        session, SessionManager(store), Governance(store), store, repair_call)
    assert seen["agent"] == "claude"
    assert "Target file: src/games/asteroids.js" in seen["prompt"]
    executed = [a for a in session.proposed_actions
                if a.kind == "write_file" and a.status == "executed"]
    assert executed and executed[-1].filename == "src/games/asteroids.js"
    assert not any(a.filename == "asteroids.js" for a in session.proposed_actions)


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

    def call(member, prompt, timeout_s=None):
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


# --- Fix 1: the blind stages get the source they must match -------------------


def test_judge_prompt_carries_source_and_fidelity_when_named(session):
    """Fix 1: when the task named a source to MATCH, the blind judges must SEE it
    and be told to score structural fidelity — not judge prose in a vacuum (the
    plain-prose candidate that dropped an illustrated-spread source's whole format
    won a 'matched set' vote because judges never saw the source)."""
    session.classification.match_source = True
    labeled = [("Candidate 1", "Benny loved three things...", "")]
    src = "----- SOURCE: Splash.txt -----\nSPREAD 1\n[RIGHT PAGE — ILLUSTRATION PROMPT]\n"
    p = rounds.score_candidates_prompt(session, labeled, source=src)
    assert "SOURCE THE OUTPUT MUST MATCH" in p
    assert "ILLUSTRATION PROMPT" in p              # the real source text is present
    assert "MATCHED SET" in p                      # hard requirement (match_source)
    assert "never the 'commentary'" in p           # the don't-strip-the-format guidance

    # no source named → no fidelity block (greenfield judging is unchanged)
    assert "SOURCE THE OUTPUT MUST MATCH" not in rounds.score_candidates_prompt(session, labeled)


def test_judge_prompt_source_is_soft_without_match_intent(session):
    """A source is referenced but the task didn't ask for an exact match: the
    block still appears (judges should weigh fidelity) but is stated softly, not
    as a HARD 'matched set' requirement."""
    session.classification.match_source = False
    labeled = [("Candidate 1", "body", "")]
    p = rounds.score_candidates_prompt(session, labeled, source="SRC BODY")
    assert "SOURCE THE OUTPUT MUST MATCH" in p and "SRC BODY" in p
    assert "MATCHED SET" not in p
    assert "references this source" in p


def test_finish_pass_with_no_edits_surfaces_unaddressed_defects(session, store):
    """Fix 4: if the finisher returns no usable edits for judge-flagged defects
    (live: it answered with a SKILL request instead of EDIT blocks), the winner
    still ships — but the unaddressed defects are SURFACED, not shipped silently."""
    _candidate(session, "aaa", "game.html", WEAK)
    _candidate(session, "zzz", "game.html", STRONG)
    lead = CouncilMember(role=Role.lead, agent="claude", active=True)
    judges = [CouncilMember(role=Role.panelist, agent="j1")]
    council = Council(members=[lead] + judges)
    session.council = council
    reply = ("SCORE Candidate 1: 3\nSCORE Candidate 2: 9\nWINNER: Candidate 2\n"
             "DEFECT: strip the orphan scaffolding line")

    def call(member, prompt, timeout_s=None):
        return Contribution(round=0, role=member.role, agent=member.agent, content=reply)

    def codifier_call(member, prompt):
        # chair ratifies (keeping the judges' defect); the fix pass then gets a
        # skill request instead of EDIT blocks — the exact live failure mode
        if "RATIFY" in prompt:
            return Contribution(round=0, role=member.role, agent=member.agent,
                                content="RATIFY: Candidate 2")
        return Contribution(round=0, role=member.role, agent=member.agent,
                            content="SKILL: read_file Splash.txt")

    out = loop._run_best_of_n(session, council, judges, call, codifier_call, store)
    assert out["fixes"] == 0
    shipped = next(a for a in session.proposed_actions
                   if a.kind == "write_file" and a.role == Role.implementer)
    assert shipped.content == STRONG                       # shipped unchanged...
    assert any("did not apply" in u for u in session.unresolved)  # ...but surfaced
