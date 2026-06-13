"""Workspace state — the allowed work area (Type-2 module).

A workspace is a real project directory the council may read and (with
approval) write into, instead of the throwaway per-session sandbox. File skills
resolve inside the workspace root with a hard containment boundary; sessions
capture the active workspace at submit time. No workspace ⇒ unchanged sandbox
behaviour.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conclave_os.adapters.mock import MockAdapter
from conclave_os.executor import ExecutionError, execute, resolve_in_workspace
from conclave_os.governance import Governance
from conclave_os.logstore import LogStore
from conclave_os.models import ProposedAction, Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService
from conclave_os.sessions import SessionManager
from conclave_os.workspaces import WorkspaceError, WorkspaceStore


# ---- WorkspaceStore ----------------------------------------------------------


def test_store_add_list_activate_roundtrip(tmp_path):
    store = WorkspaceStore(tmp_path / "data")
    assert store.list() == []
    proj = tmp_path / "proj"
    ws = store.add("My Project", str(proj))
    assert ws.root == str(proj.resolve())
    assert proj.is_dir()  # created if missing
    assert [w.id for w in store.list()] == [ws.id]
    assert store.active() is None
    store.set_active(ws.id)
    assert store.active().id == ws.id
    store.set_active(None)
    assert store.active() is None
    store.remove(ws.id)
    assert store.list() == []


def test_store_rejects_blank_name_and_file_root(tmp_path):
    store = WorkspaceStore(tmp_path / "data")
    with pytest.raises(WorkspaceError):
        store.add("", str(tmp_path / "x"))
    f = tmp_path / "afile"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        store.add("bad", str(f))


def test_set_active_unknown_raises(tmp_path):
    store = WorkspaceStore(tmp_path / "data")
    with pytest.raises(WorkspaceError):
        store.set_active("ws_nope")


# ---- path containment --------------------------------------------------------


def test_resolve_allows_subdirs(tmp_path):
    p = resolve_in_workspace(tmp_path, "src/app/main.py")
    assert p == (tmp_path / "src" / "app" / "main.py").resolve()


@pytest.mark.parametrize("bad", ["../escape.txt", "../../x", "/etc/passwd", "C:/evil.txt", "", "."])
def test_resolve_rejects_escapes(tmp_path, bad):
    with pytest.raises(ExecutionError):
        resolve_in_workspace(tmp_path, bad)


# ---- skills operate in the workspace -----------------------------------------


@pytest.fixture()
def session(tmp_path):
    return SessionManager(LogStore(tmp_path / "data")).create("ws task", source="test")


def test_write_file_into_workspace_subdir(tmp_path, session):
    session.workspace_root = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        args={"filename": "src/main.py", "content": "print('hi')"},
    )
    result = execute(session, action, tmp_path / "data")
    assert Path(result) == (tmp_path / "proj" / "src" / "main.py").resolve()
    assert Path(result).read_text(encoding="utf-8") == "print('hi')"


def test_read_file_from_workspace(tmp_path, session):
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "note.md").write_text("hello", encoding="utf-8")
    session.workspace_root = str(root)
    action = ProposedAction(
        session_id=session.session_id, kind="read_file", role=Role.researcher,
        args={"filename": "docs/note.md"},
    )
    assert execute(session, action, tmp_path / "data") == "hello"


def test_write_rejects_escape_in_workspace(tmp_path, session):
    session.workspace_root = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        args={"filename": "../escape.py", "content": "x"},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path / "data")


def test_no_workspace_uses_flat_sandbox(tmp_path, session):
    # workspace_root None ⇒ legacy artifacts sandbox; subdir parts are dropped
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        args={"filename": "report.md", "content": "x"},
    )
    result = execute(session, action, tmp_path / "data")
    assert Path(result).parent == (tmp_path / "data" / "artifacts" / session.session_id).resolve()


# ---- service binds the active workspace + end to end -------------------------


class WsArtifactAdapter:
    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.implementer:
            return AdapterResult(content="ARTIFACT: src/main.py\nprint('built')\n", duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_session_binds_active_workspace_and_writes_there(tmp_path):
    svc = ConclaveService(data_dir=tmp_path / "data")
    svc.registry.register(WsArtifactAdapter())
    proj = tmp_path / "proj"
    ws = svc.create_workspace("proj", str(proj))
    svc.set_active_workspace(ws.id)

    session = svc.run("Build an app: src/main.py", source="test")
    assert session.workspace_root == str(proj.resolve())
    assert session.status == SessionStatus.awaiting_approval
    action = session.proposed_actions[0]
    # the approval names the workspace, not the sandbox
    assert any("workspace" in a.action for a in session.approvals)

    done = svc.approve(session.session_id, session.approvals[0].approval_id, approved=True)
    assert done.status == SessionStatus.done
    written = proj / "src" / "main.py"
    assert written.read_text(encoding="utf-8") == "print('built')"


# ---- endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path / "data")
    return TestClient(main_mod.app)


def test_pick_folder_returns_selected_path(tmp_path, monkeypatch):
    import subprocess
    import sys

    class _P:
        stdout = "C:\\Users\\me\\proj"
        returncode = 0

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    svc = ConclaveService(data_dir=tmp_path / "data")
    assert svc.pick_folder() == {"path": "C:\\Users\\me\\proj"}


def test_pick_folder_cancel_returns_none(tmp_path, monkeypatch):
    import subprocess
    import sys

    class _P:
        stdout = ""  # user cancelled
        returncode = 0

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    svc = ConclaveService(data_dir=tmp_path / "data")
    assert svc.pick_folder() == {"path": None}


def test_list_dir_lists_subdirs_and_parent(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")  # files excluded
    svc = ConclaveService(data_dir=tmp_path / "data")
    out = svc.list_dir(str(tmp_path))
    names = {Path(d).name for d in out["dirs"]}
    assert names == {"a", "b", "data"}  # only directories
    assert out["parent"] == str(tmp_path.parent)


def test_list_dir_bad_path_errors_gracefully(tmp_path):
    svc = ConclaveService(data_dir=tmp_path / "data")
    out = svc.list_dir(str(tmp_path / "does_not_exist"))
    assert out["dirs"] == [] and "error" in out


def test_fs_list_endpoint(client, tmp_path):
    (tmp_path / "sub").mkdir()
    r = client.get("/fs/list", params={"path": str(tmp_path)})
    assert r.status_code == 200
    assert any(Path(d).name == "sub" for d in r.json()["dirs"])


def test_workspace_endpoints(client, tmp_path):
    assert client.get("/workspaces").json() == {"workspaces": [], "active": None}
    created = client.post("/workspaces", json={"name": "p", "root": str(tmp_path / "p")}).json()
    wid = created["id"]
    listing = client.get("/workspaces").json()
    assert [w["id"] for w in listing["workspaces"]] == [wid]
    activated = client.put("/workspaces/active", json={"id": wid}).json()
    assert activated["active"] == wid
    cleared = client.put("/workspaces/active", json={"id": None}).json()
    assert cleared["active"] is None
    after_delete = client.delete(f"/workspaces/{wid}").json()
    assert after_delete["workspaces"] == []
