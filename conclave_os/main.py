"""FastAPI surface for the Coordinator OS.

Run with:  uvicorn conclave_os.main:app --port 8790
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import __version__
from .service import ConclaveService

app = FastAPI(title="Conclave OS — Coordinator", version=__version__)
service = ConclaveService()


class TaskIn(BaseModel):
    text: str
    source: str = "api"


class ApprovalIn(BaseModel):
    approved: bool
    by: str = "user"


class InputAnswerIn(BaseModel):
    answer: str | None = None
    decline: bool = False
    by: str = "user"


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
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.post("/tasks")
def submit_task(body: TaskIn) -> dict:
    try:
        session = service.run(body.text, source=body.source)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _summary(session)


@app.post("/sessions/{session_id}/approvals/{approval_id}")
def resolve_approval(session_id: str, approval_id: str, body: ApprovalIn) -> dict:
    try:
        session = service.approve(session_id, approval_id, body.approved, by=body.by)
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
            session = service.answer(session_id, input_id, body.answer, by=body.by)
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


