"""Session Manager — creation and the session state machine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .logstore import LogStore
from .models import Budgets, SESSION_SCHEMA_VERSION, Session, SessionStatus, Task, short_id

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
        task = Task(task_id=f"t_{short_id()}", session_id=session_id, source=source, text=text)
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
    out["schema_version"] = SESSION_SCHEMA_VERSION
    return out
