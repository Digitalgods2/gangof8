"""Milestone 6 (skill loop): an agent requests a no-approval skill mid-round.

A 'SKILL: <name> <arg>' line in a contribution is run through the permission
kernel; authorized no-approval skills (read_file) execute and their result is
fed back to the same agent on a single re-call. Approval-gated skills are
refused here (they go through the ARTIFACT proposal path), unknown skills and
disallowed roles are fed back as errors rather than crashing, and a contribution
with no marker is left untouched (no extra agent call).
"""

from pathlib import Path

import pytest

from conclave_os import config, executor, loop
from conclave_os.governance import Governance
from conclave_os.logstore import LogStore
from conclave_os.models import Contribution, CouncilMember, Role, Session
from conclave_os.sessions import SessionManager


@pytest.fixture()
def store(tmp_path) -> LogStore:
    return LogStore(tmp_path)


@pytest.fixture()
def governance(store) -> Governance:
    return Governance(store)


@pytest.fixture()
def session(store) -> Session:
    return SessionManager(store).create("skill-request test task", source="test")


def _member(role: Role) -> CouncilMember:
    return CouncilMember(role=role, agent="mock", active=True)


def _contribution(role: Role, content: str) -> Contribution:
    return Contribution(round=0, role=role, agent="mock", content=content)


def _recording_call(answer: str = "informed answer"):
    """A stub agent call that records the prompts it was given and returns a
    fixed follow-up contribution."""
    prompts: list[str] = []

    def call(member: CouncilMember, prompt: str) -> Contribution:
        prompts.append(prompt)
        return _contribution(member.role, answer)

    return call, prompts


def _seed(tmp_path, session: Session, name: str, body: str) -> None:
    d = executor.artifacts_dir(tmp_path, session.session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


# --- the happy path -----------------------------------------------------------


def test_resolved_read_recalls_agent_with_content(tmp_path, store, governance, session):
    _seed(tmp_path, session, "data.txt", "the file contents")
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "I need the data.\nSKILL: read_file data.txt")
    call, prompts = _recording_call()

    out = loop._resolve_skill_requests(session, member, "ORIGINAL", contribution, call, governance, store)

    assert out.content == "informed answer", "agent is re-called; informed answer replaces the request"
    assert len(prompts) == 1, "exactly one re-call"
    assert "the file contents" in prompts[0], "the read result is fed back to the agent"
    assert "ORIGINAL" in prompts[0], "the original prompt is preserved in the re-call"
    executed = [a for a in session.proposed_actions if a.kind == "read_file" and a.status == "executed"]
    assert len(executed) == 1
    assert "read_file" in session.tools_called


def test_search_project_request_maps_query_arg(tmp_path, store, governance, session):
    # a workspace with a file the query should hit
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("import fastapi\n", encoding="utf-8")
    session.workspace_root = str(root)
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "Let me look first.\nSKILL: search_project fastapi")
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "ORIGINAL", contribution, call, governance, store)

    assert "main.py:1:" in prompts[0], "search result (path:line) is fed back to the agent"
    act = [a for a in session.proposed_actions if a.kind == "search_project"][0]
    assert act.args == {"query": "fastapi"}  # positional arg mapped to the skill's input
    assert act.status == "executed"


def test_no_marker_is_a_noop(tmp_path, store, governance, session):
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "Just a normal answer, no skills.")

    def call(member, prompt):  # must not be invoked
        raise AssertionError("agent should not be re-called when no skill is requested")

    out = loop._resolve_skill_requests(session, member, "ORIGINAL", contribution, call, governance, store)
    assert out is contribution
    assert session.proposed_actions == []


# --- error feedback (never crashes the turn) ----------------------------------


def test_unknown_skill_is_fed_back(tmp_path, store, governance, session):
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "SKILL: frobnicate whatever")
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "unknown skill" in prompts[0]
    assert not any(a.status == "executed" for a in session.proposed_actions)


def test_role_not_allowed_is_denied_and_fed_back(tmp_path, store, governance, session):
    _seed(tmp_path, session, "data.txt", "secret-ish")
    member = _member(Role.critic)  # critic is not in read_file.allowed_roles
    contribution = _contribution(Role.critic, "SKILL: read_file data.txt")
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "denied" in prompts[0]
    denied = [a for a in session.proposed_actions if a.status == "denied"]
    assert len(denied) == 1
    assert session.approvals == [], "a denial creates no approval"


def test_non_read_skill_is_refused_midround(tmp_path, store, governance, session):
    """write_file is refused mid-round because it changes state (category != read),
    NOT because it requires approval — only read skills run mid-deliberation."""
    member = _member(Role.implementer)
    contribution = _contribution(Role.implementer, "SKILL: write_file out.md")
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "it changes state" in prompts[0]
    assert "ARTIFACT/EDIT/PROMOTE" in prompts[0]
    assert session.approvals == [], "write_file is not gated mid-round; it is just refused"
    assert not any(a.status == "executed" for a in session.proposed_actions)


def test_requests_are_capped_per_turn(tmp_path, store, governance, session):
    for i in (1, 2, 3):
        _seed(tmp_path, session, f"f{i}.txt", f"body {i}")
    member = _member(Role.researcher)
    content = "SKILL: read_file f1.txt\nSKILL: read_file f2.txt\nSKILL: read_file f3.txt"
    contribution = _contribution(Role.researcher, content)
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    executed = [a for a in session.proposed_actions if a.status == "executed"]
    assert len(executed) == config.MAX_SKILL_REQUESTS_PER_TURN == 2


# --- prompt advertises the skill only when relevant ---------------------------


# The governance context now always explains the read skills, so these check
# the per-role ADVERTISEMENT phrasing ("read a file with a line", "Available
# now:") rather than the bare skill name.
_READ_AD = "read a file with a line"
_LIST_AD = "with a line 'SKILL: list_dir"


def test_build_prompt_advertises_read_only_with_files_and_allowed_role(session):
    from conclave_os.models import RoundSpec

    spec = RoundSpec(round=0, goal="gather facts", agents=[Role.researcher])
    # no files → not advertised (no "Available now")
    assert "Available now:" not in loop.build_prompt(session, spec, Role.researcher, [])
    # files + allowed role → advertised with the filename
    p = loop.build_prompt(session, spec, Role.researcher, ["notes.md"])
    assert _READ_AD in p and "notes.md" in p
    # files but a role not allowed to read → not advertised
    assert _READ_AD not in loop.build_prompt(session, spec, Role.critic, ["notes.md"])


def test_build_prompt_advertises_list_dir_when_workspace_bound(session, tmp_path):
    from conclave_os.models import RoundSpec

    spec = RoundSpec(round=0, goal="gather facts", agents=[Role.researcher])
    # no workspace/established → list_dir not advertised (nothing to enumerate)
    assert _LIST_AD not in loop.build_prompt(session, spec, Role.researcher, [])
    # workspace bound + allowed role → advertised so the council can DISCOVER files
    session.workspace_root = str(tmp_path)
    assert _LIST_AD in loop.build_prompt(session, spec, Role.researcher, [])
