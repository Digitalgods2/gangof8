"""Panel seats have real access to the council space (owner directive: a role
unable to land its work in the sandbox is a design failure).

- Council-space read/write skills are open to EVERY role; promote stays the
  one role-gated, human-approved boundary.
- A panel seat's SKILL: requests resolve mid-fan-out (same chained resolver as
  the lead), so its take is grounded in the real files.
- A panel seat's complete ARTIFACT file is saved to the sandbox immediately,
  namespaced per seat (codex__index.html) so parallel takes never clobber each
  other — advisory drafts for the lead, never the delivery itself.
- A failed delegation is retried once before the lead falls back to doing the
  talent's work itself.
"""

import pytest

from conclave_os import executor, loop
from conclave_os.governance import Governance
from conclave_os.logstore import LogStore
from conclave_os.models import Contribution, Council, CouncilMember, Role
from conclave_os.registry import AgentError
from conclave_os.sessions import SessionManager
from conclave_os.skills import get_skill

GAME = "<!doctype html>\n<html><body><script>go()</script></body></html>"


@pytest.fixture()
def store(tmp_path) -> LogStore:
    return LogStore(tmp_path)


@pytest.fixture()
def governance(store) -> Governance:
    return Governance(store)


@pytest.fixture()
def session(store):
    return SessionManager(store).create("panel access test task", source="test")


def test_agent_call_honors_per_seat_timeout_with_authoring_floor(session, store):
    # The Settings per-seat timeout is the seat's base budget; an explicit heavy
    # timeout (authoring) still applies as a floor. Raising the seat raises its
    # authoring headroom too; lowering it can't starve authoring.
    from types import SimpleNamespace
    session.cli_timeouts = {"claude": 500}
    session.budgets.max_agent_calls = 50
    seen = []

    class Reg:
        def call(self, agent, role, prompt, timeout_s=None, images=None):
            seen.append(timeout_s)
            return SimpleNamespace(content="ok", model="m", tokens=0, duration_ms=1)

    reg = Reg()
    claude = CouncilMember(role=Role.panelist, agent="claude")
    loop._agent_call(session, reg, store, claude, "p")                  # None → seat setting 500
    loop._agent_call(session, reg, store, claude, "p", timeout_s=300)   # max(300, 500) = 500 (floor)
    loop._agent_call(session, reg, store, claude, "p", timeout_s=800)   # max(800, 500) = 800 (explicit wins)
    codex = CouncilMember(role=Role.panelist, agent="codex")
    loop._agent_call(session, reg, store, codex, "p")                   # no override → config default 300
    assert seen == [500, 500, 800, 300]


def test_panel_one_uses_the_authoring_timeout(session, governance, store):
    # On a build, a panel seat authors a whole candidate — it must get the long
    # authoring timeout, not the quick per-agent default (live: claude was killed
    # at 240s mid-authoring and dropped every build).
    seen = {}

    def call(m, prompt, timeout_s=None):
        seen["timeout"] = timeout_s
        return Contribution(round=0, role=m.role, agent=m.agent, content="a plain take, no skill requests")

    member = CouncilMember(role=Role.panelist, agent="codex", active=True)
    loop._panel_one(session, member, "author the game", call, governance, store, timeout_s=600)
    assert seen["timeout"] == 600


def _contribution(role: Role, agent: str, content: str) -> Contribution:
    return Contribution(round=0, role=role, agent=agent, content=content)


def test_council_space_skills_are_open_to_every_role():
    for name in ("write_file", "edit_file", "read_file", "search_project",
                 "list_dir", "web_search", "web_fetch"):
        s = get_skill(name)
        for role in Role:
            assert role in s.allowed_roles, f"{role.value} blocked from {name}"
    # the ONE boundary that stays role-gated: delivery
    assert Role.panelist not in get_skill("promote").allowed_roles


def test_panel_seat_resolves_read_skills(tmp_path, store, governance, session):
    d = executor.artifacts_dir(tmp_path, session.session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "notes.txt").write_text("ground truth body", encoding="utf-8")
    member = CouncilMember(role=Role.panelist, agent="mock", active=True)
    replies = iter(["SKILL: read_file notes.txt",
                    "informed take grounded in the actual file contents"])
    prompts: list[str] = []

    def call(m, prompt):
        prompts.append(prompt)
        return _contribution(m.role, m.agent, next(replies))

    out = loop._panel_one(session, member, "P", call, governance, store)
    assert out is not None
    assert out.content.startswith("informed take")
    assert "ground truth body" in prompts[1], "read result fed back to the seat"


def test_panel_complete_file_saved_namespaced(tmp_path, store, governance, session):
    member = CouncilMember(role=Role.panelist, agent="codex", active=True)
    take = f"My complete implementation follows.\nARTIFACT: index.html\n{GAME}"

    def call(m, prompt):
        return _contribution(m.role, m.agent, take)

    out = loop._panel_one(session, member, "P", call, governance, store)
    assert out is not None
    saved = executor.artifacts_dir(tmp_path, session.session_id) / "codex__index.html"
    assert saved.read_text(encoding="utf-8") == GAME, "cleaned body on disk at once"
    acts = [a for a in session.proposed_actions if a.kind == "write_file"]
    assert len(acts) == 1
    assert acts[0].role == Role.panelist and acts[0].status == "executed"
    assert acts[0].filename == "codex__index.html", "namespaced — no clobbering"
    assert str(saved) in session.files_changed
    # a panel draft must never masquerade as the delivery: materialization /
    # salvage still see an empty pipeline
    assert not loop._has_proposals(session)


def test_two_seats_same_filename_do_not_clobber(tmp_path, store, governance, session):
    for agent, body in (("codex", GAME), ("gemini", GAME.replace("go()", "run()"))):
        member = CouncilMember(role=Role.panelist, agent=agent, active=True)
        loop._capture_panel_artifacts(
            session, member, f"take\nARTIFACT: index.html\n{body}", governance, store)
    d = executor.artifacts_dir(tmp_path, session.session_id)
    assert (d / "codex__index.html").is_file()
    assert (d / "gemini__index.html").is_file()
    assert "run()" in (d / "gemini__index.html").read_text(encoding="utf-8")


def test_council_drafts_read_whole_not_truncated(tmp_path, store, governance, session):
    """The 2k skill window starved the lead on its own ~25KB drafts (live:
    'every draft is truncated mid-file', looping on re-reads). A file the
    council itself wrote into the sandbox reads at the sandbox cap."""
    d = executor.artifacts_dir(tmp_path, session.session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex__game.html").write_text("x" * 3000 + "THE_REAL_ENDING", encoding="utf-8")
    member = CouncilMember(role=Role.lead, agent="mock", active=True)
    contribution = _contribution(Role.lead, "mock", "SKILL: read_file codex__game.html")
    prompts: list[str] = []

    def call(m, prompt):
        prompts.append(prompt)
        return _contribution(m.role, m.agent, "picked the winner")

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "THE_REAL_ENDING" in prompts[0], "past the old 2000-char window"


def test_consults_run_after_delegates_and_see_their_files(store, session):
    """DELEGATE (production) resolves before CONSULT (review), and the consult
    sees the freshly authored file contents — all-concurrent siblings made the
    critic fire before the artifact existed (live: 'I am awaiting the
    artifact', a wasted call)."""
    lead = CouncilMember(role=Role.lead, agent="mock", active=True)
    coder = CouncilMember(role=Role.code_generator, agent="mock")
    critic = CouncilMember(role=Role.critic, agent="mock")
    council = Council(members=[lead, coder, critic])
    contribution = _contribution(
        Role.lead, "mock",
        "DELEGATE: code_generator - author the complete game.html\n"
        "CONSULT: critic - review the authored file for defects")
    calls: list[tuple[Role, str]] = []

    def call(m, prompt):
        calls.append((m.role, prompt))
        if m.role == Role.code_generator:
            return _contribution(m.role, m.agent, f"done\nARTIFACT: game.html\n{GAME}")
        if m.role == Role.critic:
            return _contribution(m.role, m.agent, "reviewed: sound")
        return _contribution(m.role, m.agent, "integrated. ROUND: DONE")

    out = loop._resolve_delegations(session, council, lead, "P", contribution, call, store)
    roles = [r for r, _ in calls]
    assert roles.index(Role.critic) > roles.index(Role.code_generator)
    critic_prompt = next(p for r, p in calls if r == Role.critic)
    assert "FILES JUST AUTHORED" in critic_prompt
    assert GAME in critic_prompt, "the critic reviews the real file, not a promise"
    assert out.content.startswith("integrated")


def test_delegation_reseats_on_requester_model_after_double_failure(store, session):
    """A seat that fails twice gets the role RESEATED on the requester's own
    model before the assignment collapses back onto the lead (live: codex as
    code_generator failed the same way across two runs)."""
    lead = CouncilMember(role=Role.lead, agent="claude", active=True)
    critic = CouncilMember(role=Role.critic, agent="codex")
    council = Council(members=[lead, critic])
    contribution = _contribution(Role.lead, "claude", "CONSULT: critic - check the loop")
    seen: list[str] = []

    def call(m, prompt):
        seen.append(m.agent)
        if m.agent == "codex":
            raise AgentError("codex CLI exited 1: sandbox")
        if m.role == Role.critic:
            return _contribution(m.role, m.agent, "the loop is sound")
        return _contribution(m.role, m.agent, "integrated. ROUND: DONE")

    out = loop._resolve_delegations(session, council, lead, "P", contribution, call, store)
    assert seen[:3] == ["codex", "codex", "claude"], "retry, then reseat"
    assert out.content.startswith("integrated")
    assert not any("failed" in u for u in session.unresolved), \
        "a reseated delegation leaves no failure note"


def test_sandbox_reads_work_with_established_root_bound(tmp_path, store, governance, session):
    """The single-space read default made council-authored sandbox drafts
    unreadable whenever an established folder was bound — the read resolved
    to the (empty) established folder and failed (live, twice). Reads now
    fall back across spaces."""
    est = tmp_path / "est"
    est.mkdir()
    (est / "notes.txt").write_text("established copy", encoding="utf-8")
    session.established_root = str(est)
    d = executor.artifacts_dir(tmp_path, session.session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "centipede.html").write_text("<html>the draft</html>", encoding="utf-8")
    member = CouncilMember(role=Role.lead, agent="mock", active=True)
    contribution = _contribution(Role.lead, "mock",
                                 "SKILL: read_file centipede.html\n"
                                 "SKILL: read_file notes.txt")
    prompts: list[str] = []

    def call(m, prompt):
        prompts.append(prompt)
        return _contribution(m.role, m.agent, "grounded take")

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "the draft" in prompts[0], "sandbox draft readable despite established root"
    assert "established copy" in prompts[0], "established files still read (default space)"


def test_declared_destination_autofills_promotes_for_new_files(tmp_path, store, session):
    """A run that authored a verified file for an explicitly named destination
    must PROPOSE its delivery even when the lead omitted the PROMOTE line —
    the old already-delivered-only rule left the user's folder empty on every
    first (greenfield) delivery while reporting success. Still human-gated.
    Panel drafts never qualify."""
    from conclave_os.models import ProposedAction

    est = tmp_path / "est"
    est.mkdir()  # empty: greenfield — nothing pre-delivered
    session.established_root = str(est)
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="centipede.html", content=GAME,
        args={"filename": "centipede.html", "content": GAME}))
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.panelist,
        filename="codex__centipede.html", content=GAME,
        args={"filename": "codex__centipede.html", "content": GAME}))
    loop._ensure_redelivery_promotes(session, store)
    promotes = [a.filename for a in session.proposed_actions if a.kind == "promote"]
    assert promotes == ["centipede.html"], "authored deliverable proposed; panel draft not"
    # idempotent on resume
    loop._ensure_redelivery_promotes(session, store)
    assert len([a for a in session.proposed_actions if a.kind == "promote"]) == 1


def test_delegates_run_on_the_production_call(store, session):
    """DELEGATE (whole-file authoring) uses the lead-grade produce_call; the
    quick-specialist call killed a reseated coder at 240s mid-file (live)."""
    lead = CouncilMember(role=Role.lead, agent="mock", active=True)
    coder = CouncilMember(role=Role.code_generator, agent="mock")
    critic = CouncilMember(role=Role.critic, agent="mock")
    council = Council(members=[lead, coder, critic])
    contribution = _contribution(
        Role.lead, "mock",
        "DELEGATE: code_generator - author game.html\n"
        "CONSULT: critic - sanity-check the plan")
    via: list[tuple[str, Role]] = []

    def call(m, prompt):
        via.append(("specialist", m.role))
        return _contribution(m.role, m.agent, "ok. ROUND: DONE")

    def produce(m, prompt):
        via.append(("production", m.role))
        return _contribution(m.role, m.agent, f"done\nARTIFACT: game.html\n{GAME}")

    loop._run_delegations(session, council, lead, contribution.content,
                          call, store, depth=1, produce_call=produce)
    assert ("production", Role.code_generator) in via
    assert ("specialist", Role.critic) in via
    assert ("specialist", Role.code_generator) not in via


def test_delegation_retries_once_on_agent_error(store, session):
    lead = CouncilMember(role=Role.lead, agent="mock", active=True)
    critic = CouncilMember(role=Role.critic, agent="mock")
    council = Council(members=[lead, critic])
    contribution = _contribution(Role.lead, "mock", "CONSULT: critic - check the loop")
    attempts = {"n": 0}

    def call(m, prompt):
        if m.role == Role.critic:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise AgentError("codex CLI exited 1: transient sandbox hiccup")
            return _contribution(m.role, m.agent, "the loop is sound")
        return _contribution(m.role, m.agent, "integrated. ROUND: DONE")

    out = loop._resolve_delegations(session, council, lead, "P", contribution, call, store)
    assert attempts["n"] == 2, "one retry before giving up"
    assert out.content.startswith("integrated")
    assert not any("failed" in u for u in session.unresolved), \
        "a recovered delegation leaves no failure note"
