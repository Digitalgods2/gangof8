"""FastAPI surface for the Coordinator OS.

Run with:  python cli.py serve   (or: uvicorn gangof8.main:app --port 8790)
Dashboard: http://127.0.0.1:8790/
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__, config, reporting
from .models import Role
from .service import GangOf8Service

# The trinity of local CLI seats whose call timeout is user-tunable in Settings.
_CLI_TIMEOUT_SEATS = ("gemini", "claude", "codex")


def _cli_timeout_defaults() -> dict[str, int]:
    """Built-in per-seat timeout (seconds) — what a seat uses when unset."""
    return {s: config.agent_timeout(s) for s in _CLI_TIMEOUT_SEATS}

app = FastAPI(title="Gang of 8 — Coordinator", version=__version__)
# env read here (not from config.BACKEND) so `cli.py serve --backend X` can
# set it just before uvicorn imports this module
service = GangOf8Service(backend=os.environ.get("GANGOF8_BACKEND"))

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
    # grant a session-wide standing approval for this approval's category
    # (e.g. every promote in this session) with one deliberate decision
    approve_all: bool = False


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
        "truth_claims": [c.model_dump() for c in session.truth_claims],
        "files_changed": session.files_changed,
        "workspace_root": session.workspace_root,
        "established_root": session.established_root,
        "attachments": session.attachments,
        "council_health": reporting.council_health(session.unresolved),
    }


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(
        _STATIC / "index.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


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
            session_id, approval_id, body.approved, by=body.by,
            background=body.background, approve_all=body.approve_all,
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
    data["council_health"] = reporting.council_health(data.get("unresolved", []))
    return data


@app.get("/sessions/{session_id}/timeline")
def session_timeline(session_id: str) -> dict:
    """A readable run timeline from the session's event log."""
    if service.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return service.timeline(session_id)


class EnhanceIn(BaseModel):
    text: str = ""


@app.post("/enhance")
def enhance(body: EnhanceIn) -> dict:
    """Amplify a raw prompt with the lead model (the composer's Enhance button)."""
    try:
        return service.enhance_prompt(body.text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001 — surface any model/adapter failure to the UI
        raise HTTPException(status_code=502, detail=f"enhance failed: {e}")


class OpenFileIn(BaseModel):
    session_id: str = ""
    path: str = ""


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _os_open(path: str) -> None:
    """Open a file with the host OS's default application (this is a local app,
    so the server runs on the same machine as the browser)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # Windows-only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


@app.post("/files/open")
def open_file(body: OpenFileIn) -> dict:
    """Open one of a session's written files with the OS default app. Guarded:
    the path MUST be one of the files the session actually wrote (its
    files_changed list), so this can never open an arbitrary file on disk."""
    data = service.get(body.session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    allowed = {_norm(f) for f in (data.get("files_changed") or [])}
    if not body.path or _norm(body.path) not in allowed:
        raise HTTPException(status_code=403, detail="file is not one of this session's outputs")
    if not os.path.isfile(body.path):
        raise HTTPException(status_code=404, detail="file no longer exists on disk")
    try:
        _os_open(body.path)
    except Exception as e:  # noqa: BLE001 — surface any OS-open failure to the UI
        raise HTTPException(status_code=500, detail=f"could not open file: {e}")
    return {"ok": True, "opened": body.path}


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


class FollowUpIn(BaseModel):
    text: str = ""
    attachments: list[str] = []


@app.post("/sessions/{session_id}/followup")
def followup_session(session_id: str, body: FollowUpIn) -> dict:
    """Continue the conversation: respond to the council's conclusion; it
    deliberates again with the full thread as context. Multi-modal like the
    original task box — attachments are upload ids from POST /uploads."""
    try:
        session = service.continue_session(session_id, body.text, background=True,
                                           attachments=body.attachments)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"session_id": session.session_id, "status": session.status.value}


# ---- Settings / preferences --------------------------------------------------


class SettingsPatch(BaseModel):
    # All optional — a partial patch. `extra="allow"` lets any Settings field
    # flow through even if not enumerated here (the service ignores keys that
    # aren't real settings), so adding a Settings field + UI can't be silently
    # dropped by a stale patch model — the bug that ate cli_enabled/cli_models.
    model_config = {"extra": "allow"}
    backend: str | None = None
    role_agents: dict[str, str] | None = None
    budgets: dict[str, dict] | None = None
    risk_boundary: str | None = None
    composer: dict | None = None
    ui: dict | None = None
    openrouter_enabled: dict[str, bool] | None = None
    openrouter_models: dict[str, str] | None = None
    cli_models: dict[str, str] | None = None
    cli_timeouts: dict[str, int] | None = None
    cli_enabled: dict[str, bool] | None = None


class ApiKeyIn(BaseModel):
    value: str = ""


@app.get("/settings")
def get_settings() -> dict:
    """Current effective settings plus the role→agent mapping actually in use."""
    data = service.settings.model_dump()
    data["resolved_role_agents"] = {r.value: a for r, a in service.role_agents.items()}
    data["role_catalog"] = [r.value for r in Role if r not in (Role.coordinator, Role.governance)]
    data["cli_timeout_defaults"] = _cli_timeout_defaults()
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
    out["role_catalog"] = [r.value for r in Role if r not in (Role.coordinator, Role.governance)]
    out["cli_timeout_defaults"] = _cli_timeout_defaults()
    out["effective_backend"] = service.backend
    out["note"] = "saved — backend/role changes apply to new sessions"
    return out


@app.get("/settings/seats")
def get_seats(refresh: bool = False) -> dict:
    """All seats (CLI + OpenRouter) with availability, for the role dropdowns.
    ?refresh=1 refetches the live model catalog instead of the 15-min cache."""
    return service.seats(refresh=refresh)


@app.get("/settings/api-keys/{name}")
def get_api_key(name: str) -> dict:
    """Masked status of a known API key ('openrouter' | 'gemini')."""
    try:
        return service.api_key_status(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/settings/api-keys/{name}/reveal")
def reveal_api_key(name: str) -> dict:
    """The full key value, fetched on demand by the dashboard's eye-reveal
    (the app is localhost-only and the key is stored locally anyway)."""
    try:
        return service.reveal_api_key(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/settings/api-keys/{name}")
def put_api_key(name: str, body: ApiKeyIn) -> dict:
    try:
        return service.set_api_key(name, body.value)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/settings/api-keys/{name}")
def delete_api_key(name: str) -> dict:
    try:
        return service.clear_api_key(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
