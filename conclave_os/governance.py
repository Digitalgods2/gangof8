"""Governance Layer + Tool Permission Manager.

Default-deny: the only capability that never needs approval is generate_text.
Every other capability raises ApprovalRequired, which pauses the session for
the human. This module is the ONLY path to side effects.
"""

from __future__ import annotations

from .config import ALWAYS_ALLOWED_CAPABILITIES
from .logstore import LogStore
from .models import ApprovalRequest, ProposedAction, Risk, Session, utcnow


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

    def authorize_action(
        self, session: Session, action: ProposedAction
    ) -> ApprovalRequest | None:
        """Permission kernel: decide whether a proposed action may execute,
        using the skill's metadata instead of hardcoded literals.

        - Unknown skill / role not allowed → mark the action denied (with a
          clear error) and return None. The action is skipped, not raised on,
          so a single bad proposal never kills the session.
        - requires_approval and no matching approved approval → create and
          return an ApprovalRequest (the caller must pause).
        - requires_approval False (or already approved) → return None; the
          action may proceed straight to execution.
        """
        from .skills import get_skill  # local import: skills imports nothing here

        skill = get_skill(action.kind)
        if skill is None:
            self._deny_action(session, action, f"unknown skill: {action.kind!r}")
            return None
        if action.role not in skill.allowed_roles:
            self._deny_action(
                session, action,
                f"role {action.role.value!r} may not use skill {skill.name!r}",
            )
            return None
        if not skill.requires_approval:
            return None
        for a in session.approvals:
            if a.action_ref == action.action_id and a.status == "approved":
                return None
        return self.request_approval(
            session,
            action=(
                f"{skill.name}: {action.filename or skill.description} "
                f"in data/artifacts/{session.session_id}/"
            ),
            category=skill.category,
            risk=skill.risk,
            action_ref=action.action_id,
        )

    def _deny_action(self, session: Session, action: ProposedAction, reason: str) -> None:
        action.status = "denied"
        action.error = reason
        self.store.log_event(
            session.session_id, "action_denied",
            {"action_id": action.action_id, "kind": action.kind, "reason": reason},
        )

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
