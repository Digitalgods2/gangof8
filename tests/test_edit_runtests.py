"""edit_file (surgical replace) and run_tests (governed code execution).

Both are approval-gated. edit_file replaces a unique OLD snippet in an existing
file; run_tests executes a command in the workspace/sandbox only after approval
and returns its output. The implementer proposes them via EDIT / RUNTESTS blocks
in its draft, parsed alongside ARTIFACT.
"""

from pathlib import Path

import pytest

from conclave_os import loop
from conclave_os.adapters.mock import MockAdapter
from conclave_os.executor import ExecutionError, execute
from conclave_os.governance import Governance
from conclave_os.logstore import LogStore
from conclave_os.models import ProposedAction, Risk, Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService
from conclave_os.sessions import SessionManager
from conclave_os.skills import get_skill


@pytest.fixture()
def governance(tmp_path):
    return Governance(LogStore(tmp_path))


@pytest.fixture()
def session(tmp_path):
    return SessionManager(LogStore(tmp_path)).create("edit/run task", source="test")


# ---- skill metadata ----------------------------------------------------------


def test_edit_file_metadata():
    s = get_skill("edit_file")
    assert s.category == "file_edit" and s.requires_approval is True
    assert s.risk == Risk.medium and s.allowed_roles == [Role.implementer]


def test_run_tests_metadata():
    s = get_skill("run_tests")
    assert s.category == "code_exec" and s.requires_approval is True
    assert s.risk == Risk.high and Role.implementer in s.allowed_roles


# ---- edit_file handler -------------------------------------------------------


def test_edit_file_replaces_unique_snippet(governance, session, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="edit_file", role=Role.implementer,
        args={"filename": "app.py", "old": "x = 1", "new": "x = 42"},
    )
    approval = governance.authorize_action(session, action)
    assert approval is not None and approval.category == "file_edit"  # gated
    governance.resolve(session, approval.approval_id, approved=True)
    execute(session, action, tmp_path)  # data_dir unused (workspace bound)
    assert (root / "app.py").read_text(encoding="utf-8") == "x = 42\nprint(x)\n"


def test_edit_file_rejects_missing_and_ambiguous(session, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("dup\ndup\n", encoding="utf-8")
    session.workspace_root = str(root)
    missing = ProposedAction(session_id=session.session_id, kind="edit_file",
                             args={"filename": "a.py", "old": "nope", "new": "z"})
    with pytest.raises(ExecutionError, match="not found"):
        execute(session, missing, tmp_path)
    ambiguous = ProposedAction(session_id=session.session_id, kind="edit_file",
                               args={"filename": "a.py", "old": "dup", "new": "z"})
    with pytest.raises(ExecutionError, match="not unique"):
        execute(session, ambiguous, tmp_path)


# ---- run_tests handler -------------------------------------------------------


def test_run_tests_executes_and_captures(session, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="run_tests", role=Role.implementer,
        args={"command": "python -c \"print('TESTS-OK')\""},
    )
    out = execute(session, action, tmp_path)
    assert "TESTS-OK" in out
    assert "[passed]" in out


def test_run_tests_reports_nonzero_exit(session, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="run_tests", role=Role.implementer,
        args={"command": "python -c \"import sys; sys.exit(3)\""},
    )
    out = execute(session, action, tmp_path)
    assert "exit 3" in out


# ---- proposal parsing (EDIT / RUNTESTS blocks in a draft) --------------------


def test_collect_parses_edit_and_runtests_blocks(tmp_path):
    store = LogStore(tmp_path)
    session = SessionManager(store).create("t", source="test")
    from conclave_os.models import Contribution
    draft = Contribution(round=0, role=Role.implementer, agent="mock", content=(
        "Here is the plan.\n"
        "ARTIFACT: new.py\nprint('new')\n"
        "EDIT: app.py\n<<<<<<< OLD\nold line\n=======\nnew line\n>>>>>>> NEW\n"
        "RUNTESTS: pytest -q\n"
    ))
    session.contributions.append(draft)
    loop._collect_proposals(session, store)
    kinds = [a.kind for a in session.proposed_actions]
    assert kinds == ["write_file", "edit_file", "run_tests"]  # document order
    edit = session.proposed_actions[1]
    assert edit.args == {"filename": "app.py", "old": "old line", "new": "new line"}
    assert session.proposed_actions[2].args == {"command": "pytest -q"}
    # ARTIFACT content stops at the EDIT block (not swallowed)
    assert session.proposed_actions[0].content == "print('new')"


# ---- end to end: implementer proposes an edit, human approves ---------------


class EditAdapter:
    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.implementer:
            return AdapterResult(
                content="EDIT: note.txt\n<<<<<<< OLD\nhello\n=======\ngoodbye\n>>>>>>> NEW\n",
                duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_edit_end_to_end_after_approval(tmp_path):
    svc = ConclaveService(data_dir=tmp_path / "data")
    svc.registry.register(EditAdapter())
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "note.txt").write_text("hello world", encoding="utf-8")
    ws = svc.create_workspace("p", str(proj))
    svc.set_active_workspace(ws.id)

    session = svc.run("Change the greeting in note.txt", source="test")
    assert session.status == SessionStatus.awaiting_approval
    assert session.proposed_actions[0].kind == "edit_file"
    done = svc.approve(session.session_id, session.approvals[0].approval_id, approved=True)
    assert done.status == SessionStatus.done
    assert (proj / "note.txt").read_text(encoding="utf-8") == "goodbye world"
