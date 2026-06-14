"""FastAPI surface for the Coordinator OS.

Run with:  python cli.py serve   (or: uvicorn conclave_os.main:app --port 8790)
Dashboard: http://127.0.0.1:8790/
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__
from .service import ConclaveService

app = FastAPI(title="Conclave OS — Coordinator", version=__version__)
# env read here (not from config.BACKEND) so `cli.py serve --backend X` can
# set it just before uvicorn imports this module
service = ConclaveService(backend=os.environ.get("CONCLAVE_OS_BACKEND"))

_STATIC = Path(__file__).parent / "static"


class TaskIn(BaseModel):
    text: str
    source: str = "api"
    background: bool = False  # True: return immediately, poll GET /sessions/{id}
    attachments: list[str] = []  # upload ids whose text is folded into the task


class UploadIn(BaseModel):
    name: str
    content_base64: str


class ApprovalIn(BaseModel):
    approved: bool
    by: str = "user"
    background: bool = False


class InputAnswerIn(BaseModel):
    answer: str | None = None
    decline: bool = False
    by: str = "user"
    background: bool = False


def _summary(session) -> dict:
    return {
        "session_id": session.session_id,
        "status": session.status.value,
        "stop_reason": session.stop_reason,
        "final": session.final.model_dump() if session.final else None,
        "pending_approvals": [a.model_dump() for a in session.approvals if a.status == "pending"],
        "pending_inputs": [r.model_dump() for r in session.input_requests if r.status == "pending"],
        "actions": [
            {"action_id": p.action_id, "kind": p.kind, "filename": p.filename,
             "status": p.status, "result_path": p.result_path}
            for p in session.proposed_actions
        ],
        "files_changed": session.files_changed,
        "workspace_root": session.workspace_root,
        "established_root": session.established_root,
        "attachments": session.attachments,
    }


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "backend": service.backend}


@app.post("/tasks")
def submit_task(body: TaskIn) -> dict:
    try:
        if body.background:
            session = service.submit_background(
                body.text, source=body.source, attachments=body.attachments)
        else:
            session = service.run(
                body.text, source=body.source, attachments=body.attachments)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _summary(session)


@app.post("/uploads")
def upload_file(body: UploadIn) -> dict:
    """Accept a base64-encoded attachment (text/PDF/image), store + extract its
    text, and return a record. Submit the returned id in TaskIn.attachments."""
    try:
        return service.save_upload(body.name, body.content_base64)
    except Exception as e:  # noqa: BLE001 — clean error, never 500 the composer
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/sessions/{session_id}/approvals/{approval_id}")
def resolve_approval(session_id: str, approval_id: str, body: ApprovalIn) -> dict:
    try:
        session = service.approve(
            session_id, approval_id, body.approved, by=body.by, background=body.background
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _summary(session)


@app.get("/approvals")
def pending_approvals() -> list[dict]:
    return service.pending_approvals()


@app.post("/sessions/{session_id}/inputs/{input_id}")
def resolve_input(session_id: str, input_id: str, body: InputAnswerIn) -> dict:
    try:
        if body.decline:
            session = service.decline_input(session_id, input_id, by=body.by)
        else:
            if not (body.answer or "").strip():
                raise HTTPException(status_code=422, detail="answer required unless decline=true")
            session = service.answer(
                session_id, input_id, body.answer, by=body.by, background=body.background
            )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _summary(session)


@app.get("/inputs")
def pending_inputs() -> list[dict]:
    return service.pending_inputs()


@app.get("/sessions")
def list_sessions() -> list[dict]:
    return service.list()


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    data = service.get(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    return data


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    if not service.delete_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": session_id}


@app.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str) -> dict:
    try:
        return service.cancel_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


# ---- Settings / preferences --------------------------------------------------


class SettingsPatch(BaseModel):
    # All optional — a partial patch. Unknown keys are ignored by the service.
    backend: str | None = None
    role_agents: dict[str, str] | None = None
    budgets: dict[str, dict] | None = None
    risk_boundary: str | None = None
    composer: dict | None = None
    ui: dict | None = None


@app.get("/settings")
def get_settings() -> dict:
    """Current effective settings plus the role→agent mapping actually in use."""
    data = service.settings.model_dump()
    data["resolved_role_agents"] = {r.value: a for r, a in service.role_agents.items()}
    data["effective_backend"] = service.backend
    return data


@app.put("/settings")
def put_settings(body: SettingsPatch) -> dict:
    """Apply a partial settings patch, persist it, and return the new settings.
    Backend / role-mapping changes take effect for new sessions; sessions
    already running keep the backend they started on."""
    patch = body.model_dump(exclude_none=True)
    try:
        service.update_settings(patch)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    out = service.settings.model_dump()
    out["resolved_role_agents"] = {r.value: a for r, a in service.role_agents.items()}
    out["effective_backend"] = service.backend
    out["note"] = "saved — backend/role changes apply to new sessions"
    return out


@app.get("/settings/seats")
def get_seats() -> dict:
    """The local CLI agents (claude/codex/gemini) with PATH availability, for
    the role→agent dropdowns in settings."""
    return service.seats()


# ---- Workspaces --------------------------------------------------------------


class WorkspaceIn(BaseModel):
    name: str
    root: str


class ActiveWorkspaceIn(BaseModel):
    id: str | None = None  # null clears the active workspace (→ per-session sandbox)


@app.get("/workspaces")
def list_workspaces() -> dict:
    """All registered workspaces plus the active one. The active workspace is
    the allowed work area new sessions read/write within (governed)."""
    return service.list_workspaces()


@app.post("/workspaces")
def create_workspace(body: WorkspaceIn) -> dict:
    try:
        ws = service.create_workspace(body.name, body.root)
    except Exception as e:  # noqa: BLE001 — surface a clean validation error
        raise HTTPException(status_code=422, detail=str(e))
    return ws.model_dump()


@app.get("/fs/list")
def fs_list(path: str | None = None) -> dict:
    """List sub-directories under `path` (drives at the root) for the in-page
    folder browser. Folders only; localhost convenience."""
    return service.list_dir(path)


@app.get("/fs/shortcuts")
def fs_shortcuts() -> dict:
    """Quick-access locations (Home/Desktop/Documents/… + This PC)."""
    return service.fs_shortcuts()


class MkdirIn(BaseModel):
    path: str
    name: str


@app.post("/fs/mkdir")
def fs_mkdir(body: MkdirIn) -> dict:
    """Create a sub-folder (in-page browser's New-folder button)."""
    return service.make_dir(body.path, body.name)


@app.put("/workspaces/active")
def set_active_workspace(body: ActiveWorkspaceIn) -> dict:
    try:
        service.set_active_workspace(body.id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(e))
    return service.list_workspaces()


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str) -> dict:
    service.remove_workspace(workspace_id)
    return service.list_workspaces()


@app.post("/workspaces/empty")
def empty_workspace() -> dict:
    """Delete the contents of the ACTIVE workspace (the council's own area).
    Does not touch any established folder."""
    try:
        return service.empty_workspace()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))


