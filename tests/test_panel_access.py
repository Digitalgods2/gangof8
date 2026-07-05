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
