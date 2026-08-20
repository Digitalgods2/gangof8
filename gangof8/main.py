"""FastAPI surface for the Coordinator OS.

Run with:  python cli.py serve   (or: uvicorn gangof8.main:app --port 8790)
Dashboard: http://127.0.0.1:8790/
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__, reporting, security, skills
from .models import Role
from .service import GangOf8Service
from .settings import SettingsProfile

app = FastAPI(title="Gang of 8 — Coordinator", version=__version__)
# env read here (not from config.BACKEND) so `cli.py serve --backend X` can
# set it just before uvicorn imports this module
service = GangOf8Service(backend=os.environ.get("GANGOF8_BACKEND"))

_STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    """Keep the local-control API off the network unless explicitly enabled."""
    client_host = request.client.host if request.client else None
    if not security.local_request_allowed(client_host):
        return JSONResponse(
            status_code=403,
            content={"detail": "Gang of 8 accepts local requests only"},
        )
    return await call_next(request)


def _require_sensitive_local(request: Request, action: str) -> None:
    client_host = request.client.host if request.client else None
    if not security.sensitive_local_action_allowed(client_host):
        raise HTTPException(
            status_code=403,
            detail=f"{action} requires a local request",
        )


class TaskIn(BaseModel):
    text: str
    source: str = "api"
    background: bool = False  # True: return immediately, poll GET /sessions/{id}
    attachments: list[str] = Field(default_factory=list)
    outcome_contract: dict | None = None
    execution_profile: str = "auto"
    playbook_id: str | None = None
    parent_session_id: str | None = None


class TaskPreviewIn(BaseModel):
    text: str
    source: str = "api"
    attachments: list[str] = Field(default_factory=list)
    outcome_contract: dict | None = None
    execution_profile: str = "auto"


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
        "original_text": session.task.original_text or session.task.text,
        "outcome_contract": session.outcome_contract,
        "execution_profile": session.execution_profile,
        "routing_decision": session.routing_decision,
        "playbook_id": session.playbook_id,
        "parent_session_id": session.parent_session_id,
        "outcome": session.outcome,
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
        "collaboration_mode": session.collaboration_mode,
        "delivery_mode": session.delivery_mode,
        "work_package_id": session.work_package_id,
        "work_package_owner": session.work_package_owner,
        "resource_roster": session.resource_roster,
        "participation_mode": session.participation_mode,
        "collaboration_assignments": [
            assignment.model_dump() for assignment in session.collaboration_assignments
        ],
        "collaboration_integrated_files": session.collaboration_integrated_files,
        "collaboration_integration_status": session.collaboration_integration_status,
        "required_frontier_authors": session.required_frontier_authors,
        "frontier_author_recoveries": session.frontier_author_recoveries,
        "candidate_metrics": session.candidate_metrics,
        "quality_gate": session.quality_gate,
        "goal_release": session.goal_release,
        "attachments": session.attachments,
        "integration_proposal": (
            session.integration_proposal.model_dump() if session.integration_proposal else None
        ),
        "council_health": reporting.council_health(session.unresolved),
        "run_summary": reporting.run_summary(session),
    }


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(
        _STATIC / "index.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/logo.png")
def logo() -> FileResponse:
    """The Gang of 8 emblem — shown in the header and used as the browser-tab
    favicon (referenced from index.html)."""
    return FileResponse(_STATIC / "logo.png", media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/gangof8-text.png")
def header_lockup() -> FileResponse:
    """The single-image dashboard header lockup supplied by the user."""
    return FileResponse(_STATIC / "gangof8-text.png", media_type="image/png",
                        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})


@app.get("/app.css")
def dashboard_css() -> FileResponse:
    return FileResponse(
        _STATIC / "app.css", media_type="text/css",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/app.js")
def dashboard_js() -> FileResponse:
    return FileResponse(
        _STATIC / "app.js", media_type="text/javascript",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/dashboard-utils.js")
def dashboard_utils_js() -> FileResponse:
    return FileResponse(
        _STATIC / "dashboard-utils.js", media_type="text/javascript",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "backend": service.backend}


@app.get("/diagnostics")
def diagnostics() -> dict:
    data = service.diagnostics()
    data["seats"] = service.seat_health.snapshot()
    return data


@app.post("/tasks")
def submit_task(body: TaskIn) -> dict:
    try:
        kind, item = service.start_task(
            body.text,
            source=body.source,
            background=body.background,
            attachments=body.attachments,
            outcome_contract=body.outcome_contract,
            execution_profile=body.execution_profile,
            playbook_id=body.playbook_id,
            parent_session_id=body.parent_session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if kind == "goal":
        payload = service.get_goal(item.goal_id) or item.model_dump()
    else:
        payload = _summary(item)
    payload["kind"] = kind
    routing = getattr(item, "routing_decision", {}) or {}
    payload["auto_routed"] = bool(
        routing.get("requested_profile") == "auto"
        and routing.get("selected_route") == "build_team"
    )
    payload["route_reason"] = routing.get("reason", "")
    return payload


@app.post("/tasks/preview")
def preview_task(body: TaskPreviewIn) -> dict:
    """Infer an editable outcome contract and show the routing decision before
    any model call or side effect occurs."""
    try:
        return service.preview_task(
            body.text,
            source=body.source,
            attachments=body.attachments,
            outcome_contract=body.outcome_contract,
            execution_profile=body.execution_profile,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


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
    # Old persisted sessions predate these audit fields; expose explicit empty
    # values so API/UI consumers never have to guess whether a key disappeared.
    data.setdefault("intent", {})
    data.setdefault("intent_clarification", "")
    data.setdefault("frontier_author_seats", [])
    data.setdefault("required_frontier_authors", [])
    data.setdefault("frontier_author_recoveries", {})
    data.setdefault("candidate_author_recoveries", {})
    data.setdefault("candidate_metrics", {})
    data.setdefault("quality_gate", {})
    data.setdefault("outcome_contract", {})
    data.setdefault("execution_profile", "auto")
    data.setdefault("routing_decision", {})
    data["evaluation"] = service.session_evaluation(session_id)
    service.annotate_council_models(data)  # label each member with the model it runs
    data["council_health"] = reporting.council_health(data.get("unresolved", []))
    data["run_summary"] = reporting.run_summary(data)
    return data


@app.get("/sessions/{session_id}/timeline")
def session_timeline(session_id: str) -> dict:
    """A readable run timeline from the session's event log."""
    if service.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return service.timeline(session_id)


class CloneIn(BaseModel):
    run: bool = False
    background: bool = True


class EvaluationIn(BaseModel):
    verdict: str
    rating: int | None = None
    notes: str = ""


class SteeringIn(BaseModel):
    kind: str
    payload: dict = Field(default_factory=dict)


@app.post("/sessions/{session_id}/clone")
def clone_session(session_id: str, body: CloneIn) -> dict:
    """Return a clean editable template, or launch it as a fresh independent run."""
    try:
        return service.clone_session(
            session_id, run=body.run, background=body.background
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/sessions/{session_id}/artifacts")
def session_artifacts(session_id: str) -> dict:
    try:
        return service.artifact_manifest(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@app.get("/sessions/{session_id}/artifacts/{artifact_id}/preview")
def preview_artifact(session_id: str, artifact_id: str):
    try:
        preview = service.preview_artifact(session_id, artifact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="artifact not found")
    if preview.get("file_path"):
        return FileResponse(
            preview["file_path"],
            media_type=preview.get("media_type") or "application/octet-stream",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return preview


@app.get("/sessions/{session_id}/artifacts/{artifact_id}/download")
def download_artifact(session_id: str, artifact_id: str) -> FileResponse:
    try:
        item = service.download_artifact(session_id, artifact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(
        item["file_path"],
        media_type=item.get("media_type") or "application/octet-stream",
        filename=item["name"],
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/sessions/{session_id}/commands")
def session_commands(session_id: str) -> list[dict]:
    try:
        return service.list_steering_commands(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@app.post("/sessions/{session_id}/commands")
def steer_session(session_id: str, body: SteeringIn) -> dict:
    try:
        return service.add_steering_command(session_id, body.kind, body.payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/sessions/{session_id}/commands/{command_id}")
def revoke_steering_command(session_id: str, command_id: str) -> dict:
    try:
        return service.revoke_steering_command(session_id, command_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="command not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.put("/sessions/{session_id}/evaluation")
def evaluate_session(session_id: str, body: EvaluationIn) -> dict:
    try:
        return service.evaluate_session(
            session_id, body.verdict, rating=body.rating, notes=body.notes
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/goals/{goal_id}/timeline")
def goal_timeline(goal_id: str) -> dict:
    """The whole goal's ordered story plus a derived postmortem summary."""
    try:
        return service.goal_timeline(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="goal not found")


@app.get("/seats")
def seats() -> dict:
    """Live per-seat health: state, reason, since. Fed by every adapter
    call's outcome; consulted by scheduling; rendered as dashboard badges."""
    return {
        "panel": service.panel,
        "seats": service.seat_health.snapshot(),
    }


@app.get("/events/stream")
async def events_stream(request: Request, since: int = 0):
    """Server-Sent Events: every log event as it happens, rendered through
    the human vocabulary (icon/label/detail). The dashboard's live feed."""
    import asyncio
    import json as _json

    from fastapi.responses import StreamingResponse

    async def generate():
        cursor = since if since > 0 else service.store.feed_cursor
        # On connect, replay a short recent window so the pane is never empty.
        backlog = service.store.feed_since(max(0, cursor - 30), limit=30)
        loop = asyncio.get_event_loop()
        while True:
            if await request.is_disconnected():
                return
            batch = backlog or await loop.run_in_executor(
                None, service.store.feed_wait, cursor, 20.0)
            backlog = []
            if not batch:
                yield ": keepalive\n\n"
                continue
            for entry in batch:
                cursor = max(cursor, entry["seq"])
                rendered = reporting.format_timeline([entry])[0]
                rendered["seq"] = entry["seq"]
                rendered["session_id"] = entry.get("session_id", "")
                payload = entry.get("payload") or {}
                if payload.get("goal_id"):
                    rendered["goal_id"] = payload["goal_id"]
                yield "data: " + _json.dumps(
                    rendered, ensure_ascii=False, default=str) + "\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


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
def open_file(body: OpenFileIn, request: Request) -> dict:
    """Open one of a session's written files with the OS default app. Guarded:
    the path MUST be one of the files the session actually wrote (its
    files_changed list), so this can never open an arbitrary file on disk."""
    client_host = request.client.host if request.client else None
    if not security.sensitive_local_action_allowed(client_host):
        raise HTTPException(status_code=403, detail="opening local files requires a local request")
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


class DeleteHistoryIn(BaseModel):
    confirmation: str = ""


@app.delete("/history")
def delete_all_history(body: DeleteHistoryIn, request: Request) -> dict:
    _require_sensitive_local(request, "deleting all history")
    if body.confirmation != "DELETE ALL":
        raise HTTPException(
            status_code=400,
            detail="confirmation must be exactly DELETE ALL",
        )
    return service.delete_all_history()


@app.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str) -> dict:
    try:
        return service.cancel_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@app.post("/sessions/{session_id}/calls/{call_id}/stop")
def stop_agent_call(session_id: str, call_id: str) -> dict:
    """Stop one long-running model seat without cancelling sibling work."""
    try:
        return service.stop_agent_call(session_id, call_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="active call not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


class FollowUpIn(BaseModel):
    text: str = ""
    attachments: list[str] = []
    artifact_id: str | None = None


@app.post("/sessions/{session_id}/followup")
def followup_session(session_id: str, body: FollowUpIn) -> dict:
    """Continue the conversation: respond to the council's conclusion; it
    deliberates again with the full thread as context. Multi-modal like the
    original task box — attachments are upload ids from POST /uploads."""
    try:
        session = service.continue_session(session_id, body.text, background=True,
                                           attachments=body.attachments,
                                           artifact_id=body.artifact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    acknowledged = service.is_terminal_acknowledgement(
        body.text,
        attachments=body.attachments,
        artifact_id=body.artifact_id,
    )
    return {
        "session_id": session.session_id,
        "status": session.status.value,
        "acknowledged": acknowledged,
        "message": (
            "Acknowledged. The completed session remains closed; no models "
            "were called."
            if acknowledged else ""
        ),
    }


# ---- Goals (/goal): long-horizon objectives, milestone by milestone -----------


class GoalIn(BaseModel):
    text: str
    background: bool = False  # True: plan + run on a worker, poll GET /goals/{id}
    participation_mode: str | None = None
    outcome_contract: dict | None = None
    execution_profile: str = "build_team"
    playbook_id: str | None = None
    parent_goal_id: str | None = None


@app.post("/goals")
def create_goal(body: GoalIn) -> dict:
    """Open a build-team goal: owned packages share private staging and the
    complete verified manifest crosses into the project through one approval."""
    try:
        goal = service.create_goal(
            body.text,
            background=body.background,
            participation_mode=body.participation_mode,
            outcome_contract=body.outcome_contract,
            execution_profile=body.execution_profile,
            playbook_id=body.playbook_id,
            parent_goal_id=body.parent_goal_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return service.get_goal(goal.goal_id) or goal.model_dump()


@app.get("/goals")
def list_goals() -> list[dict]:
    return service.list_goals()


@app.get("/goals/{goal_id}")
def get_goal(goal_id: str) -> dict:
    data = service.get_goal(goal_id)
    if data is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return data


@app.post("/goals/{goal_id}/clone")
def clone_goal(goal_id: str, body: CloneIn) -> dict:
    try:
        return service.clone_goal(goal_id, run=body.run, background=body.background)
    except KeyError:
        raise HTTPException(status_code=404, detail="goal not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/goals/{goal_id}/cancel")
def cancel_goal(goal_id: str) -> dict:
    try:
        return service.cancel_goal(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="goal not found")


@app.post("/goals/{goal_id}/calls/{call_id}/stop")
def stop_goal_agent_call(goal_id: str, call_id: str) -> dict:
    """Stop the planning model without cancelling the goal."""
    try:
        return service.stop_goal_agent_call(goal_id, call_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="active planning call not found")


@app.post("/goals/{goal_id}/resume")
def resume_goal(goal_id: str) -> dict:
    try:
        return service.resume_goal(goal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="goal not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/goals/{goal_id}")
def delete_goal(goal_id: str) -> dict:
    if not service.delete_goal(goal_id):
        raise HTTPException(status_code=404, detail="goal not found")
    return {"deleted": goal_id}


# ---- Reusable playbooks / capability catalogue -------------------------------


class PlaybookIn(BaseModel):
    name: str
    description: str = ""
    task_template: str = ""
    outcome_contract: dict | None = None
    execution_profile: str = "auto"
    session_id: str | None = None


class PlaybookRunIn(BaseModel):
    text: str | None = None
    background: bool = True


@app.get("/playbooks")
def list_playbooks() -> list[dict]:
    return service.list_playbooks()


@app.post("/playbooks")
def create_playbook(body: PlaybookIn) -> dict:
    try:
        return service.save_playbook(
            name=body.name,
            description=body.description,
            task_template=body.task_template,
            outcome_contract=body.outcome_contract,
            execution_profile=body.execution_profile,
            session_id=body.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.put("/playbooks/{playbook_id}")
def update_playbook(playbook_id: str, body: PlaybookIn) -> dict:
    try:
        return service.save_playbook(
            playbook_id=playbook_id,
            name=body.name,
            description=body.description,
            task_template=body.task_template,
            outcome_contract=body.outcome_contract,
            execution_profile=body.execution_profile,
            session_id=body.session_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="playbook not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/playbooks/{playbook_id}")
def delete_playbook(playbook_id: str) -> dict:
    if not service.delete_playbook(playbook_id):
        raise HTTPException(status_code=404, detail="playbook not found")
    return {"deleted": playbook_id}


@app.post("/playbooks/{playbook_id}/run")
def run_playbook(playbook_id: str, body: PlaybookRunIn) -> dict:
    try:
        return service.run_playbook(
            playbook_id, text=body.text, background=body.background
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="playbook not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/capabilities")
def capability_catalogue() -> dict:
    return skills.capability_manifest()


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
    out["effective_backend"] = service.backend
    out["note"] = "saved — backend/role changes apply to new sessions"
    return out


@app.get("/settings/profile")
def export_settings_profile() -> dict:
    """Export one portable JSON profile: no secrets, workspaces, or paths."""
    return service.settings_profile().model_dump()


@app.post("/settings/profile")
def import_settings_profile(body: SettingsProfile) -> dict:
    """Load a portable profile while preserving installation-local state."""
    try:
        service.import_settings_profile(body)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "loaded": True,
        "profile": service.settings_profile().model_dump(),
        "note": "profile loaded; API keys, workspaces, and sandbox location were unchanged",
    }


@app.post("/settings/profile/default")
def apply_default_settings_profile() -> dict:
    """Load the versioned default-settings.json bundled with the app."""
    try:
        service.load_default_settings_profile()
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"packaged settings profile is invalid: {e}")
    return {
        "loaded": True,
        "profile": service.settings_profile().model_dump(),
        "note": "packaged defaults loaded; API keys, workspaces, and sandbox location were unchanged",
    }


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
def reveal_api_key(name: str, request: Request) -> dict:
    """The full key value, fetched on demand by the dashboard's eye-reveal
    (the app is localhost-only and the key is stored locally anyway)."""
    _require_sensitive_local(request, "revealing API keys")
    try:
        return service.reveal_api_key(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/settings/api-keys/{name}")
def put_api_key(name: str, body: ApiKeyIn, request: Request) -> dict:
    _require_sensitive_local(request, "changing API keys")
    try:
        return service.set_api_key(name, body.value)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/settings/api-keys/{name}")
def delete_api_key(name: str, request: Request) -> dict:
    _require_sensitive_local(request, "changing API keys")
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
def create_workspace(body: WorkspaceIn, request: Request) -> dict:
    _require_sensitive_local(request, "registering workspaces")
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
def fs_mkdir(body: MkdirIn, request: Request) -> dict:
    """Create a sub-folder (in-page browser's New-folder button)."""
    _require_sensitive_local(request, "creating local folders")
    return service.make_dir(body.path, body.name)


@app.put("/workspaces/active")
def set_active_workspace(body: ActiveWorkspaceIn, request: Request) -> dict:
    _require_sensitive_local(request, "changing the active workspace")
    try:
        service.set_active_workspace(body.id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(e))
    return service.list_workspaces()


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str, request: Request) -> dict:
    _require_sensitive_local(request, "removing workspaces")
    service.remove_workspace(workspace_id)
    return service.list_workspaces()


@app.post("/workspaces/empty")
def empty_workspace(request: Request) -> dict:
    """Delete the contents of the ACTIVE workspace (the council's own area).
    Does not touch any established folder."""
    _require_sensitive_local(request, "emptying a workspace")
    try:
        return service.empty_workspace()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
