"""Governance Layer + Tool Permission Manager.

Default-deny: the only capability that never needs approval is generate_text.
Every other capability raises ApprovalRequired, which pauses the session for
the human. This module is the ONLY path to side effects.
"""

from __future__ import annotations

from .config import ALWAYS_ALLOWED_CAPABILITIES
from .logstore import LogStore
from .models import ApprovalRequest, Risk, Session, utcnow


class ApprovalRequired(Exception):
    def __init__(self, approval: ApprovalRequest):
        super().__init__(f"approval required: {approval.action} [{approval.category}]")
        self.approval = approval


class BudgetExceeded(Exception):
    pass


class Governance:
    def __init__(self, store: LogStore):
        self.store = store

    def requires_approval(self, capability: str) -> bool:
        return capability not in ALWAYS_ALLOWED_CAPABILITIES

    def check(
        self,
        session: Session,
        capability: str,
        action: str = "",
        category: str = "external",
        risk: Risk = Risk.medium,
    ) -> None:
        """Allow the call to proceed, or raise ApprovalRequired (pausing the session)."""
        if not self.requires_approval(capability):
            return
        action = action or capability
        for a in session.approvals:
            if a.action == action and a.status == "approved":
                return
        raise ApprovalRequired(self.request_approval(session, action, category, risk))

    def request_approval(
        self, session: Session, action: str, category: str,
        risk: Risk = Risk.medium, action_ref: str | None = None,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            session_id=session.session_id, action=action, category=category,
            risk=risk, action_ref=action_ref,
        )
        session.approvals.append(approval)
        self.store.log_event(session.session_id, "approval_requested", approval.model_dump())
        return approval

    def resolve(
        self, session: Session, approval_id: str, approved: bool, by: str = "user"
    ) -> ApprovalRequest:
        approval = next((a for a in session.approvals if a.approval_id == approval_id), None)
        if approval is None:
            raise KeyError(f"no approval {approval_id} on session {session.session_id}")
        approval.status = "approved" if approved else "denied"
        approval.resolved_at = utcnow()
        approval.resolved_by = by
        self.store.log_event(session.session_id, "approval_resolved", approval.model_dump())
        self.store.save_session(session)
        return approval
