"""Milestone 6A: skill registry + permission kernel.

write_file behaves exactly as in Phase 4 (sandboxed, approval-gated). The
registry generalizes to a second skill, read_file, which is low-risk and
requires no approval — proving the kernel is metadata-driven, not
write_file-only. The kernel role-gates actions and rejects unknown skills
without killing the session.
"""

from pathlib import Path

import pytest

from gangof8 import executor
from gangof8.executor import ExecutionError, execute
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import ProposedAction, Risk, Role, Session
from gangof8.sessions import SessionManager
from gangof8.skills import HANDLERS, SKILLS, Skill, get_skill


@pytest.fixture()
def governance(tmp_path):
    return Governance(LogStore(tmp_path))


@pytest.fixture()
def session(tmp_path) -> Session:
    return SessionManager(LogStore(tmp_path)).create("skill test task", source="test")


# --- registry metadata --------------------------------------------------------


def test_registry_contains_skills():
    expected = {"write_file", "read_file", "search_project", "list_dir",
                "web_search", "web_fetch", "edit_file", "run_tests", "stage", "promote"}
    assert set(SKILLS) == expected
    assert set(HANDLERS) == expected
    assert all(isinstance(s, Skill) for s in SKILLS.values())


def test_search_project_metadata():
    s = get_skill("search_project")
    assert s.category == "read"
    assert s.requires_approval is False
    assert s.inputs == ["query", "target"]
    assert Role.architect in s.allowed_roles


def test_stage_metadata():
    s = get_skill("stage")
    assert s.category == "stage"
    assert s.risk == Risk.low
    assert s.requires_approval is False
    assert s.allowed_roles == [Role.lead, Role.implementer]
    assert s.inputs == ["filename"]


def test_promote_metadata():
    s = get_skill("promote")
    assert s.category == "promote"
    assert s.risk == Risk.medium
    assert s.requires_approval is True, "promote is the one approval-gated skill"
    assert s.allowed_roles == [Role.lead, Role.implementer]
    assert s.inputs == ["filename"]


def test_search_project_finds_names_and_content(governance, session, tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (root / "README.md").write_text("A demo project.\n", encoding="utf-8")
    (root / "app" / ".venv").mkdir()  # skipped dir
    (root / "app" / ".venv" / "junk.py").write_text("FastAPI noise", encoding="utf-8")
    session.workspace_root = str(root)

    action = ProposedAction(
        session_id=session.session_id, kind="search_project", role=Role.researcher,
        args={"query": "FastAPI"},
    )
    assert governance.authorize_action(session, action) is None  # no approval
    out = execute(session, action, tmp_path)
    assert "app/main.py:1:" in out                # content hit with path:line
    assert ".venv" not in out                     # skipped directory excluded


def test_search_project_no_match(governance, session, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("nothing here", encoding="utf-8")
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="search_project", role=Role.researcher,
        args={"query": "zzznope"},
    )
    assert "No matches" in execute(session, action, tmp_path)


# --- list_dir: low-risk discovery, no approval --------------------------------


def test_list_dir_metadata():
    s = get_skill("list_dir")
    assert s.category == "read"
    assert s.risk == Risk.low
    assert s.requires_approval is False
    assert s.inputs == ["path", "target"]
    assert Role.researcher in s.allowed_roles


def test_list_dir_lists_workspace_tree(governance, session, tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hi", encoding="utf-8")
    (root / "node_modules").mkdir()  # skipped dir
    (root / "node_modules" / "junk.js").write_text("noise", encoding="utf-8")
    session.workspace_root = str(root)

    action = ProposedAction(
        session_id=session.session_id, kind="list_dir", role=Role.researcher,
        args={"path": "."},
    )
    assert governance.authorize_action(session, action) is None  # no approval
    out = execute(session, action, tmp_path)
    assert "app/" in out and "app/main.py" in out and "README.md" in out
    assert "node_modules" not in out          # skipped directory excluded
    assert "B)" in out or "KB)" in out         # file sizes shown


def test_list_dir_subdirectory(governance, session, tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("a", encoding="utf-8")
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="list_dir", role=Role.architect,
        args={"path": "src"},
    )
    out = execute(session, action, tmp_path)
    assert "a.py" in out


def test_list_dir_rejects_escape(session, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="list_dir", role=Role.researcher,
        args={"path": "../.."},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


def test_write_file_metadata():
    s = get_skill("write_file")
    assert s.category == "file_write"
    assert s.risk == Risk.low
    assert s.requires_approval is False, "write_file is now free (no approval)"
    # council-space writes are open to EVERY seat; promote is the boundary
    assert all(role in s.allowed_roles for role in Role)
    assert s.inputs == ["filename", "content", "target"]


def test_read_file_metadata():
    s = get_skill("read_file")
    assert s.category == "read"
    assert s.risk == Risk.low
    assert s.requires_approval is False
    assert Role.researcher in s.allowed_roles and Role.implementer in s.allowed_roles
    assert s.inputs == ["filename", "target"]


def test_get_skill_unknown_returns_none():
    assert get_skill("nope") is None


# --- write_file end to end (mirrors Phase 4 expectations) ---------------------


def test_write_file_is_free_and_sandboxes(governance, session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="report.md", content="hello",
        args={"filename": "report.md", "content": "hello"},
    )
    session.proposed_actions.append(action)
    # write_file is now FREE — no approval, executes straight through
    assert governance.authorize_action(session, action) is None, "write_file is no longer gated"
    assert session.approvals == [], "a free write creates no approval"
    result = execute(session, action, tmp_path)
    path = Path(result)
    assert path.parent == executor.artifacts_dir(tmp_path, session.session_id).resolve()
    assert path.read_text(encoding="utf-8") == "hello"


def test_write_file_rejects_path_escape(session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file",
        args={"filename": "..\\..\\evil.md", "content": "x"},
    )
    # escaping paths are REJECTED by containment, not flattened into the sandbox
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


def test_write_file_rejects_empty_artifact(session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        args={"filename": "empty.html", "content": ""},
    )
    with pytest.raises(ExecutionError, match="empty artifact"):
        execute(session, action, tmp_path)
    assert not (executor.artifacts_dir(tmp_path, session.session_id) / "empty.html").exists()


def test_write_file_bad_charset_resolves_in_sandbox(session, tmp_path):
    """No charset validation anymore — resolution is by containment only, so a
    name like '###' resolves inside the sandbox and does NOT raise. Escaping
    paths are what get rejected (covered by test_write_file_rejects_path_escape)."""
    action = ProposedAction(
        session_id=session.session_id, kind="write_file",
        args={"filename": "###", "content": "x"},
    )
    result = execute(session, action, tmp_path)
    assert Path(result).name == "###"
    assert Path(result).parent == executor.artifacts_dir(tmp_path, session.session_id).resolve()
    assert Path(result).read_text(encoding="utf-8") == "x"


def test_write_file_falls_back_to_legacy_fields(session, tmp_path):
    """No args → handler reads the legacy filename/content fields."""
    action = ProposedAction(
        session_id=session.session_id, kind="write_file",
        filename="legacy.txt", content="legacy body",
    )
    result = execute(session, action, tmp_path)
    assert Path(result).read_text(encoding="utf-8") == "legacy body"


# --- read_file: low-risk, no approval -----------------------------------------


def test_read_file_requires_no_approval_and_reads(governance, session, tmp_path):
    # seed a file via write_file
    out_dir = executor.artifacts_dir(tmp_path, session.session_id)
    out_dir.mkdir(parents=True)
    (out_dir / "data.txt").write_text("contents here", encoding="utf-8")

    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": "data.txt"},
    )
    assert governance.authorize_action(session, action) is None, "read needs no gate"
    assert session.approvals == []
    assert execute(session, action, tmp_path) == "contents here"


def test_read_file_missing_file_errors(session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": "absent.txt"},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


def test_read_file_rejects_sandbox_escape(session, tmp_path):
    # a sibling file outside the session sandbox must be unreachable
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": "..\\..\\secret.txt"},
    )
    # the escaping path is rejected by containment before any read happens
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


def test_read_file_honors_absolute_path_inside_established(session, tmp_path):
    # A model may cite the full path the USER wrote in the task. When it lands
    # inside the established folder, the read must succeed (reads are safe) —
    # not be refused as "must be relative" (live: every seat was denied the
    # source file it named in full and invented the story instead).
    est = tmp_path / "Benny"
    est.mkdir()
    (est / "Benny").mkdir()  # the decoy subdir the apostrophe bug used to select
    src = est / "Benny's Splash.txt"
    src.write_text("SPLASH!", encoding="utf-8")
    session.established_root = str(est)

    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": str(src)},
    )
    assert execute(session, action, tmp_path) == "SPLASH!"


def test_read_file_absolute_path_outside_all_spaces_is_refused(session, tmp_path):
    # The boundary holds: an absolute path NOT inside any bound space is denied.
    est = tmp_path / "Benny"
    est.mkdir()
    session.established_root = str(est)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": str(secret)},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


def test_read_file_absolute_dotdot_escape_still_refused(session, tmp_path):
    # `..` that climbs out of the established folder is neutralized by resolve().
    est = tmp_path / "Benny"
    est.mkdir()
    session.established_root = str(est)
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": str(est / ".." / "secret.txt")},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


# --- stage: sandbox → workspace, free -----------------------------------------


def test_stage_moves_sandbox_file_into_workspace(governance, session, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    session.workspace_root = str(ws)
    sandbox = executor.artifacts_dir(tmp_path, session.session_id)
    sandbox.mkdir(parents=True)
    (sandbox / "keep.py").write_text("print('keep')", encoding="utf-8")

    action = ProposedAction(
        session_id=session.session_id, kind="stage", role=Role.implementer,
        args={"filename": "keep.py"},
    )
    assert governance.authorize_action(session, action) is None  # free, no approval
    result = execute(session, action, tmp_path)
    assert Path(result) == (ws / "keep.py").resolve()
    assert (ws / "keep.py").read_text(encoding="utf-8") == "print('keep')"


def test_stage_missing_sandbox_file_errors(session, tmp_path):
    session.workspace_root = str(tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    action = ProposedAction(
        session_id=session.session_id, kind="stage", role=Role.implementer,
        args={"filename": "absent.py"},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


# --- promote: council → established folder, the ONE approval-gated skill -------


def test_promote_is_gated_with_diff_then_copies(governance, session, tmp_path):
    ws = tmp_path / "ws"
    est = tmp_path / "established"
    ws.mkdir()
    est.mkdir()
    session.workspace_root = str(ws)
    session.established_root = str(est)
    (ws / "x.py").write_text("print('new code')\n", encoding="utf-8")

    action = ProposedAction(
        session_id=session.session_id, kind="promote", role=Role.implementer,
        args={"filename": "x.py"},
    )
    approval = governance.authorize_action(session, action)
    assert approval is not None, "promote must be gated"
    assert approval.category == "promote"
    assert approval.action_ref == action.action_id
    assert approval.details, "promote approval carries a non-empty diff preview"
    assert "print('new code')" in approval.details

    governance.resolve(session, approval.approval_id, approved=True)
    assert governance.authorize_action(session, action) is None  # now cleared
    result = execute(session, action, tmp_path)
    assert Path(result) == (est / "x.py").resolve()
    assert (est / "x.py").read_text(encoding="utf-8") == "print('new code')\n"


def test_promote_falls_back_to_sandbox_source(governance, session, tmp_path):
    """An ARTIFACT written to the sandbox can promote without an explicit stage."""
    est = tmp_path / "established"
    est.mkdir()
    session.established_root = str(est)
    sandbox = executor.artifacts_dir(tmp_path, session.session_id)
    sandbox.mkdir(parents=True)
    (sandbox / "y.py").write_text("scratch body\n", encoding="utf-8")

    action = ProposedAction(
        session_id=session.session_id, kind="promote", role=Role.implementer,
        args={"filename": "y.py"},
    )
    approval = governance.authorize_action(session, action)
    assert approval is not None and approval.category == "promote"
    governance.resolve(session, approval.approval_id, approved=True)
    execute(session, action, tmp_path)
    assert (est / "y.py").read_text(encoding="utf-8") == "scratch body\n"


def test_promote_ignores_workspace_when_it_is_established(governance, session, tmp_path):
    """If the active workspace is also the delivery folder, promote must use the
    sandbox artifact instead of treating the target file as council-owned source."""
    est = tmp_path / "established"
    est.mkdir()
    session.workspace_root = str(est)
    session.established_root = str(est)
    (est / "z.py").write_text("", encoding="utf-8")

    sandbox = executor.artifacts_dir(tmp_path, session.session_id)
    sandbox.mkdir(parents=True)
    (sandbox / "z.py").write_text("scratch body\n", encoding="utf-8")

    action = ProposedAction(
        session_id=session.session_id, kind="promote", role=Role.implementer,
        args={"filename": "z.py"},
    )
    approval = governance.authorize_action(session, action)
    assert approval is not None and "scratch body" in (approval.details or "")
    governance.resolve(session, approval.approval_id, approved=True)
    result = execute(session, action, tmp_path)
    assert Path(result) == (est / "z.py").resolve()
    assert (est / "z.py").read_text(encoding="utf-8") == "scratch body\n"


def test_promote_refuses_empty_source(governance, session, tmp_path):
    est = tmp_path / "established"
    est.mkdir()
    session.established_root = str(est)
    sandbox = executor.artifacts_dir(tmp_path, session.session_id)
    sandbox.mkdir(parents=True)
    (sandbox / "empty.html").write_text("", encoding="utf-8")

    action = ProposedAction(
        session_id=session.session_id, kind="promote", role=Role.implementer,
        args={"filename": "empty.html"},
    )
    approval = governance.authorize_action(session, action)
    assert approval is not None
    assert "REFUSING PROMOTE" in (approval.details or "")
    governance.resolve(session, approval.approval_id, approved=True)
    with pytest.raises(ExecutionError, match="empty artifact"):
        execute(session, action, tmp_path)
    assert not (est / "empty.html").exists()


# --- permission kernel --------------------------------------------------------


def test_role_not_allowed_is_denied_without_approval(governance, session):
    # council-space skills are open to all roles now; DELIVERY (promote) is the
    # boundary that stays role-gated — the summarizer may not propose one
    action = ProposedAction(
        session_id=session.session_id, kind="promote", role=Role.summarizer,
        args={"filename": "x.md"},
    )
    assert governance.authorize_action(session, action) is None
    assert action.status == "denied"
    assert "may not use skill" in action.error
    assert session.approvals == [], "denial creates no approval"


def test_unknown_skill_is_rejected_cleanly(governance, session):
    action = ProposedAction(
        session_id=session.session_id, kind="frobnicate", role=Role.implementer,
    )
    assert governance.authorize_action(session, action) is None
    assert action.status == "denied"
    assert "unknown skill" in action.error
    assert session.approvals == []


def test_no_approval_skill_executes_without_gate(governance, session, tmp_path):
    out_dir = executor.artifacts_dir(tmp_path, session.session_id)
    out_dir.mkdir(parents=True)
    (out_dir / "f.txt").write_text("ok", encoding="utf-8")
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.implementer,
        args={"filename": "f.txt"},
    )
    assert governance.authorize_action(session, action) is None
    assert action.status == "proposed", "kernel does not mutate a permitted, ungated action"
    assert session.approvals == []


# --- Fix 3: no cribbing a rival/prior answer from the source folder -----------


def test_read_of_rival_candidate_in_source_folder_is_blocked(tmp_path):
    """Fix 3: a seat may read the task's NAMED source, but NOT a file in the
    source folder whose name is a candidate deliverable being judged this round —
    a prior/rival answer to the same task (live: three seats read a prior
    'Benny's First Car Ride.txt' from the source folder that nobody authorized)."""
    task = ("Read Benny's Splash.txt and write the next story, Benny's First Car "
            "Ride, saving it as a .txt file.")
    session = SessionManager(LogStore(tmp_path)).create(task, source="test")
    est = tmp_path / "Benny"
    est.mkdir()
    (est / "Benny's Splash.txt").write_text("SPLASH!", encoding="utf-8")
    (est / "Benny's First Car Ride.txt").write_text("A PRIOR ANSWER", encoding="utf-8")
    session.established_root = str(est)
    # a sibling seat has drafted its candidate for the same deliverable this round
    sb = executor.artifacts_dir(tmp_path, session.session_id)
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "codex__Benny's First Car Ride.txt").write_text("draft", encoding="utf-8")

    # the NAMED source still reads (its full name appears in the task)
    ok = ProposedAction(session_id=session.session_id, kind="read_file",
                        role=Role.researcher, args={"filename": str(est / "Benny's Splash.txt")})
    assert execute(session, ok, tmp_path) == "SPLASH!"

    # the prior/rival answer by that candidate's name is refused
    bad = ProposedAction(session_id=session.session_id, kind="read_file",
                         role=Role.researcher,
                         args={"filename": str(est / "Benny's First Car Ride.txt")})
    with pytest.raises(ExecutionError):
        execute(session, bad, tmp_path)
    assert any("prior/rival" in u for u in session.unresolved)


def test_source_read_allowed_when_no_candidate_shares_its_name(tmp_path):
    """The guard is surgical: with candidates drafted under DIFFERENT names, an
    ordinary source read still works — only a name-collision with a live candidate
    is blocked, so real source reads are never caught."""
    session = SessionManager(LogStore(tmp_path)).create("read notes.txt", source="test")
    est = tmp_path / "src"
    est.mkdir()
    (est / "notes.txt").write_text("NOTES", encoding="utf-8")
    session.established_root = str(est)
    sb = executor.artifacts_dir(tmp_path, session.session_id)
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "codex__game.html").write_text("draft", encoding="utf-8")  # different name
    action = ProposedAction(session_id=session.session_id, kind="read_file",
                            role=Role.researcher, args={"filename": str(est / "notes.txt")})
    assert execute(session, action, tmp_path) == "NOTES"
