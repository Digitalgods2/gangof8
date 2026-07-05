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
from conclave_os.models import Contribution, Council, CouncilMember, Role, Session
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


def _role_recording_call(answers: dict[Role, str]):
    calls: list[tuple[Role, str]] = []

    def call(member: CouncilMember, prompt: str) -> Contribution:
        calls.append((member.role, prompt))
        return _contribution(member.role, answers.get(member.role, "ok"))

    return call, calls


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


def test_chained_requests_resolve_across_recalls(tmp_path, store, governance, session):
    """A follow-up reply may open a NEW request the first read made necessary
    (read one file → decide the next read from what it said). It is resolved
    too, with every result accumulated — the live failure was a round ending on
    a bare unresolved 'SKILL: search_project …' line."""
    _seed(tmp_path, session, "a.txt", "alpha body")
    _seed(tmp_path, session, "b.txt", "beta body")
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "SKILL: read_file a.txt")
    replies = iter(["Now I need more.\nSKILL: read_file b.txt", "final synthesis"])
    prompts: list[str] = []

    def call(m, prompt):
        prompts.append(prompt)
        return _contribution(m.role, next(replies))

    out = loop._resolve_skill_requests(session, member, "ORIGINAL", contribution, call, governance, store)

    assert out.content == "final synthesis"
    assert len(prompts) == 2
    assert "alpha body" in prompts[0]
    assert "alpha body" in prompts[1] and "beta body" in prompts[1], \
        "the second re-call carries BOTH results (accumulated)"
    executed = [a for a in session.proposed_actions if a.status == "executed"]
    assert len(executed) == 2


def test_chain_is_bounded(tmp_path, store, governance, session):
    """A model that asks for a new file on every re-call stops after
    MAX_SKILL_CHAIN_TURNS re-calls; the dangling request is returned so the
    stub check can judge it."""
    for i in range(6):
        _seed(tmp_path, session, f"f{i}.txt", f"body {i}")
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "SKILL: read_file f0.txt")
    n = iter(range(1, 6))
    prompts: list[str] = []

    def call(m, prompt):
        prompts.append(prompt)
        return _contribution(m.role, f"SKILL: read_file f{next(n)}.txt")

    out = loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert len(prompts) == config.MAX_SKILL_CHAIN_TURNS
    assert "SKILL" in out.content, "the unresolved request comes back for the stub check"


def test_repeated_request_ends_the_chain(tmp_path, store, governance, session):
    """A reply that re-asks for exactly what it was already given is returned
    as-is: no re-execution, no further re-calls."""
    _seed(tmp_path, session, "a.txt", "alpha body")
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "SKILL: read_file a.txt")
    prompts: list[str] = []

    def call(m, prompt):
        prompts.append(prompt)
        return _contribution(m.role, "Still thinking.\nSKILL: read_file a.txt")

    out = loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert len(prompts) == 1, "one re-call; the all-repeats reply ends the chain"
    executed = [a for a in session.proposed_actions if a.status == "executed"]
    assert len(executed) == 1
    assert "Still thinking" in out.content


# --- error feedback (never crashes the turn) ----------------------------------


def test_unknown_skill_is_fed_back(tmp_path, store, governance, session):
    member = _member(Role.researcher)
    contribution = _contribution(Role.researcher, "SKILL: frobnicate whatever")
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "unknown skill" in prompts[0]
    assert not any(a.status == "executed" for a in session.proposed_actions)


def test_reads_are_open_to_every_seat(tmp_path, store, governance, session):
    """Discovery is free for ALL roles (owner directive: a seat unable to work
    with the council space is a design failure) — the critic, once read-gated,
    now resolves its read like anyone else."""
    _seed(tmp_path, session, "data.txt", "the evidence")
    member = _member(Role.critic)
    contribution = _contribution(Role.critic, "SKILL: read_file data.txt")
    call, prompts = _recording_call()

    loop._resolve_skill_requests(session, member, "P", contribution, call, governance, store)
    assert "the evidence" in prompts[0]
    assert any(a.status == "executed" for a in session.proposed_actions)
    assert session.approvals == [], "reads never create approvals"


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


# --- on-demand delegation contract ------------------------------------------


def test_delegation_grants_specific_talent(store, session):
    lead = _member(Role.lead)
    researcher = CouncilMember(role=Role.researcher, agent="researcher-model", active=False)
    council = Council(members=[lead, researcher])
    contribution = _contribution(
        Role.lead,
        "I need a fact.\nCONSULT: researcher - need current documentation for this browser API",
    )
    call, calls = _role_recording_call({
        Role.researcher: "Use the current documented API.",
        Role.lead: "ARTIFACT: index.html\n<html></html>",
    })

    out = loop._resolve_delegations(
        session, council, lead, "ORIGINAL", contribution, call, store
    )

    assert out.content.startswith("ARTIFACT:")
    assert researcher.active is True
    assert [role for role, _ in calls] == [Role.researcher, Role.lead]
    assert "Use the current documented API" in calls[1][1]


def test_delegation_to_unavailable_talent_is_refused(store, session):
    lead = _member(Role.lead)
    council = Council(members=[lead])
    # governance is not an advertised talent → refused, lead still re-called once
    contribution = _contribution(Role.lead, "DELEGATE: governance - do the whole thing")
    call, calls = _role_recording_call({Role.lead: "continuing"})

    out = loop._resolve_delegations(
        session, council, lead, "ORIGINAL", contribution, call, store
    )

    assert out.content == "continuing"
    assert [role for role, _ in calls] == [Role.lead]
    assert "unavailable" in calls[0][1]


# --- the lead's skill hints advertise a skill only when relevant --------------


# The governance context always explains the read skills, so these check the
# per-role ADVERTISEMENT phrasing rather than the bare skill name.
_READ_AD = "read a file with a line"
_LIST_AD = "with a line 'SKILL: list_dir"


def test_skill_hints_advertise_read_only_with_files_and_allowed_role(session):
    # no files → not advertised (no "Available now")
    assert "Available now:" not in loop._skill_hints(session, Role.lead, [])
    # files + allowed role → advertised with the filename
    p = loop._skill_hints(session, Role.lead, ["notes.md"])
    assert _READ_AD in p and "notes.md" in p
    # discovery is open to every seat now — critic and panelist included
    assert _READ_AD in loop._skill_hints(session, Role.critic, ["notes.md"])
    assert _READ_AD in loop._skill_hints(session, Role.panelist, ["notes.md"])


def test_skill_hints_advertise_list_dir_when_workspace_bound(session, tmp_path):
    # no workspace/established → list_dir not advertised (nothing to enumerate)
    assert _LIST_AD not in loop._skill_hints(session, Role.lead, [])
    # workspace bound + allowed role → advertised so the lead can DISCOVER files
    session.workspace_root = str(tmp_path)
    assert _LIST_AD in loop._skill_hints(session, Role.lead, [])
