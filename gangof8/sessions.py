"""Session Manager — creation and the session state machine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .logstore import LogStore
from .models import (
    MAX_AGENT_CALLS, MAX_DELEGATION_DEPTH, MAX_DELEGATIONS, MAX_WALL_SECONDS,
    Budgets, SESSION_SCHEMA_VERSION, Session, SessionStatus, Task, short_id,
)

ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.received: {SessionStatus.classified, SessionStatus.failed, SessionStatus.cancelled},
    SessionStatus.classified: {SessionStatus.awaiting_approval, SessionStatus.awaiting_input, SessionStatus.deliberating, SessionStatus.failed, SessionStatus.cancelled},
    SessionStatus.awaiting_approval: {SessionStatus.deliberating, SessionStatus.composing, SessionStatus.cancelled, SessionStatus.failed},
    # awaiting_input may resolve straight back to classified — the greenfield
    # target gate fires pre-deliberation and re-runs classification on answer.
    SessionStatus.awaiting_input: {SessionStatus.classified, SessionStatus.deliberating, SessionStatus.composing, SessionStatus.cancelled, SessionStatus.failed},
    SessionStatus.deliberating: {SessionStatus.composing, SessionStatus.awaiting_approval, SessionStatus.awaiting_input, SessionStatus.failed, SessionStatus.cancelled},
    SessionStatus.composing: {SessionStatus.done, SessionStatus.awaiting_input, SessionStatus.failed, SessionStatus.cancelled},
    SessionStatus.done: set(),
    SessionStatus.failed: set(),
    SessionStatus.cancelled: set(),
}


class SessionManager:
    def __init__(self, store: LogStore):
        self.store = store

    def create(self, text: str, source: str = "cli", budgets: Optional[Budgets] = None) -> Session:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_id = f"s_{day}_{short_id()}"
        task = Task(
            task_id=f"t_{short_id()}",
            session_id=session_id,
            source=source,
            text=text,
            original_text=text,
        )
        session = Session(session_id=session_id, task=task)
        if budgets is not None:
            session.budgets = budgets
            session.budgets_locked = True
        self.store.log_event(session_id, "task_received", task.model_dump())
        self.store.save_session(session)
        return session

    def load(self, session_id: str) -> Optional[Session]:
        data = self.store.load_session(session_id)
        return Session.model_validate(migrate_session_data(data)) if data else None

    def transition(self, session: Session, new_status: SessionStatus) -> None:
        if new_status not in ALLOWED_TRANSITIONS[session.status]:
            raise ValueError(f"illegal transition {session.status.value} -> {new_status.value}")
        old = session.status
        session.status = new_status
        self.store.log_event(
            session.session_id, "status_change", {"from": old.value, "to": new_status.value}
        )
        self.store.save_session(session)


def migrate_session_data(data: dict) -> dict:
    """Normalize a persisted session dict before Pydantic validation."""
    out = dict(data)
    # Budgets were raised in place by steering (`+=` on the model field), which
    # pydantic does not re-validate, so a run could persist a ceiling above the
    # one Budgets declares. Such a session then failed EVERY load — including
    # the load `delete_all_history` does, which made the corrupt row impossible
    # to delete through the UI. Clamp instead of rejecting: the stored intent
    # ("as much budget as it takes") survives, and the row stays readable.
    budgets = out.get("budgets")
    if isinstance(budgets, dict):
        clamped = dict(budgets)
        for field, ceiling in (
            ("max_agent_calls", MAX_AGENT_CALLS),
            ("max_wall_seconds", MAX_WALL_SECONDS),
            ("max_delegation_depth", MAX_DELEGATION_DEPTH),
            ("max_delegations", MAX_DELEGATIONS),
        ):
            value = clamped.get(field)
            if isinstance(value, int) and value > ceiling:
                clamped[field] = ceiling
        out["budgets"] = clamped
    task = dict(out.get("task") or {})
    if not task.get("original_text"):
        task["original_text"] = str(task.get("text") or "").split(
            "\n\nAttachments provided by the user:", 1
        )[0].strip()
    out["task"] = task
    out.setdefault("approval_policy", "manual")
    out.setdefault("materialization_plan", None)
    out.setdefault("artifact_lineage", [])
    out.setdefault("failure_records", [])
    out.setdefault("defect_ledger", [])
    out.setdefault("repair_history", [])
    out.setdefault("recovery_state", "idle")
    out.setdefault("recovery_supervisor_events", [])
    out.setdefault("last_good_checkpoint", {})
    out.setdefault("criteria", [])
    out.setdefault("review_attempts", [])
    out.setdefault("phase", "contract_frozen")
    out.setdefault("repair_mode", False)
    out.setdefault("base_checkpoint_id", "")
    out.setdefault("candidate_checkpoint_id", "")
    out.setdefault("reused_participation_reports", [])
    out.setdefault("goal_model_call_reservation_ids", [])
    out.setdefault("candidate_fallback_groups", [])
    out.setdefault("working_set_manifest", {})
    out.setdefault("research_mode", "not_required")
    out.setdefault("research_provenance", [])
    out["schema_version"] = SESSION_SCHEMA_VERSION
    return out
