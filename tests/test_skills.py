"""Milestone 6A: skill registry + permission kernel.

write_file behaves exactly as in Phase 4 (sandboxed, approval-gated). The
registry generalizes to a second skill, read_file, which is low-risk and
requires no approval — proving the kernel is metadata-driven, not
write_file-only. The kernel role-gates actions and rejects unknown skills
without killing the session.
"""

from pathlib import Path

import pytest

from conclave_os.executor import ExecutionError, execute
from conclave_os.governance import Governance
from conclave_os.logstore import LogStore
from conclave_os.models import ProposedAction, Risk, Role, Session
from conclave_os.sessions import SessionManager
from conclave_os.skills import HANDLERS, SKILLS, Skill, get_skill


@pytest.fixture()
def governance(tmp_path):
    return Governance(LogStore(tmp_path))


@pytest.fixture()
def session(tmp_path) -> Session:
    return SessionManager(LogStore(tmp_path)).create("skill test task", source="test")


# --- registry metadata --------------------------------------------------------


def test_registry_contains_skills():
    assert set(SKILLS) == {"write_file", "read_file", "search_project"}
    assert set(HANDLERS) == {"write_file", "read_file", "search_project"}
    assert all(isinstance(s, Skill) for s in SKILLS.values())


def test_search_project_metadata():
    s = get_skill("search_project")
    assert s.category == "read"
    assert s.requires_approval is False
    assert s.inputs == ["query"]
    assert Role.architect in s.allowed_roles


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


def test_write_file_metadata():
    s = get_skill("write_file")
    assert s.category == "file_write"
    assert s.risk == Risk.medium
    assert s.requires_approval is True
    assert s.allowed_roles == [Role.implementer]
    assert s.inputs == ["filename", "content"]


def test_read_file_metadata():
    s = get_skill("read_file")
    assert s.category == "read"
    assert s.risk == Risk.low
    assert s.requires_approval is False
    assert Role.researcher in s.allowed_roles and Role.implementer in s.allowed_roles
    assert s.inputs == ["filename"]


def test_get_skill_unknown_returns_none():
    assert get_skill("nope") is None


# --- write_file end to end (mirrors Phase 4 expectations) ---------------------


def test_write_file_requires_approval_then_sandboxes(governance, session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="report.md", content="hello",
        args={"filename": "report.md", "content": "hello"},
    )
    session.proposed_actions.append(action)
    approval = governance.authorize_action(session, action)
    assert approval is not None, "write_file must be gated"
    assert approval.category == "file_write" and approval.risk == Risk.medium
    assert approval.action_ref == action.action_id

    # only an approved approval lets it run; the handler sandboxes the write
    governance.resolve(session, approval.approval_id, approved=True)
    assert governance.authorize_action(session, action) is None  # now cleared
    result = execute(session, action, tmp_path)
    path = Path(result)
    assert path.parent == (tmp_path / "artifacts" / session.session_id).resolve()
    assert path.read_text(encoding="utf-8") == "hello"


def test_write_file_rejects_path_escape(session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file",
        args={"filename": "..\\..\\evil.md", "content": "x"},
    )
    # directory parts are dropped → still lands safely in the sandbox
    result = execute(session, action, tmp_path)
    assert Path(result).name == "evil.md"
    assert Path(result).parent == (tmp_path / "artifacts" / session.session_id).resolve()


def test_write_file_rejects_bad_filename(session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file",
        args={"filename": "###", "content": "x"},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


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
    out_dir = tmp_path / "artifacts" / session.session_id
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
    # name is stripped to secret.txt, which does not exist inside the sandbox
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


# --- permission kernel --------------------------------------------------------


def test_role_not_allowed_is_denied_without_approval(governance, session):
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.researcher,
        args={"filename": "x.md", "content": "y"},
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
    out_dir = tmp_path / "artifacts" / session.session_id
    out_dir.mkdir(parents=True)
    (out_dir / "f.txt").write_text("ok", encoding="utf-8")
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.implementer,
        args={"filename": "f.txt"},
    )
    assert governance.authorize_action(session, action) is None
    assert action.status == "proposed", "kernel does not mutate a permitted, ungated action"
    assert session.approvals == []
