"""Governance Layer + Tool Permission Manager.

Default-deny: the only capability that never needs approval is generate_text.
Every other capability raises ApprovalRequired, which pauses the session for
the human. This module is the ONLY path to side effects.
"""

from __future__ import annotations

from pathlib import Path

from .config import ALWAYS_ALLOWED_CAPABILITIES
from .logstore import LogStore
from .models import ApprovalRequest, ProposedAction, Risk, Session, utcnow


class ApprovalRequired(Exception):
    def __init__(self, approval: ApprovalRequest):
        super().__init__(f"approval required: {approval.action} [{approval.category}]")
        self.approval = approval


class BudgetExceeded(Exception):
    pass


def _is_destructive_promote(session: Session, data_dir: Path, action, skill) -> bool:
    """Whether this promote would gut a file the user already has.

    Kept outside the standing-approval shortcut on purpose. "Approve all
    promote" is meant to spare the human N identical clicks on a routine
    delivery; it must never become blanket consent to replace an existing file
    with a fraction of itself. A live run did exactly that — a truncated council
    copy landed over a good 49KB deliverable — so this class of promote always
    stops for its own decision, standing grant or not.
    """
    if skill.category != "promote" or action.kind != "promote":
        return False
    try:
        from .skills import promote_shrink
        return promote_shrink(
            session, data_dir, action.args.get("filename") or action.filename
        ) is not None
    except Exception:  # noqa: BLE001 — a preview failure must never open the gate
        return False


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
        # Only parse/compile-only checks can run automatically.  A cwd is not
        # an OS sandbox, so every functional RUNTESTS command must be visible
        # to and approved by the human before the coordinator executes it.
        automatic_static_test = False
        if action.kind == "run_tests":
            from .skills import is_automatic_static_test
            automatic_static_test = is_automatic_static_test(session, action, self.store.data_dir)
        if not skill.requires_approval or automatic_static_test:
            return None
        for a in session.approvals:
            if a.action_ref == action.action_id and a.status == "approved":
                return None
        # A standing grant ("approve all <category>" on an earlier approval in
        # THIS session) clears the action without another pause — one deliberate
        # human decision instead of N identical clicks.
        if skill.category in session.standing_approvals and not _is_destructive_promote(
                session, self.store.data_dir, action, skill):
            self.store.log_event(
                session.session_id, "standing_approval_used",
                {"action_id": action.action_id, "kind": action.kind,
                 "category": skill.category, "filename": action.filename},
            )
            return None
        # promote is the one gate that writes real user code: name the target
        # and attach the diff so the human approves with full sight of the change.
        details = None
        if skill.category == "promote":
            if action.kind == "promote_batch":
                import json
                try:
                    files = json.loads(action.args.get("files", "[]"))
                except (json.JSONDecodeError, TypeError):
                    files = []
                dest = session.delivery_root or session.established_root
                summary = (
                    f"APPROVE FINAL BATCH: release {len(files)} verified file(s) "
                    f"from goal staging → {dest} as one transaction"
                )
                try:
                    from .skills import batch_promote_diff
                    details = batch_promote_diff(session, self.store.data_dir, action)
                except Exception as e:  # noqa: BLE001
                    details = f"(could not build final-batch preview: {e})"
                return self.request_approval(
                    session, action=summary, category=skill.category,
                    risk=skill.risk, action_ref=action.action_id, details=details)
            fname = action.args.get("filename") or action.filename
            # Deliver to the explicit save target if the task named one, else the
            # established folder. Flag OVERWRITE vs new so a standing "approve all
            # promote" never silently clobbers a pre-existing file the human
            # didn't realise was there.
            dest = session.delivery_root or session.established_root
            where = (f"folder {dest}" if session.delivery_root
                     else f"established folder {dest}")
            overwrite = False
            try:
                from .executor import ExecutionError, resolve_in_workspace
                overwrite = bool(dest and fname and resolve_in_workspace(
                    Path(dest), fname).is_file())
            except (OSError, ExecutionError):
                overwrite = False
            summary = (f"promote: {fname} → {where}"
                       + (" (OVERWRITES an existing file)" if overwrite else " (new file)"))
            try:
                from .skills import promote_shrink
                shrink = promote_shrink(session, self.store.data_dir, fname)
            except Exception:  # noqa: BLE001
                shrink = None
            if shrink is not None:
                old_size, new_size, removed = shrink
                # Say the number. A 99% deletion rendered as a wall of red diff
                # lines reads like any other large edit.
                summary = (
                    f"DESTRUCTIVE promote: {fname} → {where} — REPLACES an existing "
                    f"{old_size:,}-byte file with {new_size:,} bytes, removing "
                    f"{removed:.1%} of it. Confirm the council's copy is complete "
                    "before approving."
                )
                self.store.log_event(
                    session.session_id, "destructive_promote_flagged",
                    {"filename": fname, "old_bytes": old_size,
                     "new_bytes": new_size, "removed_fraction": round(removed, 4)},
                )
            try:
                from .skills import promote_diff
                details = promote_diff(session, self.store.data_dir, fname)
            except Exception as e:  # noqa: BLE001 — never let preview failure block the gate
                details = f"(could not build diff preview: {e})"
        elif skill.category == "install":
            # Never describe an install as happening "in the workspace": the card
            # is the whole gate, and what it must convey is that this fetches and
            # runs third-party code from the network — scoped to this session,
            # not the coordinator's environment.
            summary = (
                f"install packages from PyPI into session {session.session_id} only: "
                f"{action.args.get('packages') or action.filename}"
            )
        else:
            where = (
                f"workspace {session.workspace_root}" if session.workspace_root
                else f"data/artifacts/{session.session_id}/"
            )
            summary = f"{skill.name}: {action.filename or skill.description} in {where}"
        return self.request_approval(
            session,
            action=summary,
            category=skill.category,
            risk=skill.risk,
            action_ref=action.action_id,
            details=details,
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
        details: str | None = None,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            session_id=session.session_id, action=action, category=category,
            risk=risk, action_ref=action_ref, details=details,
        )
        session.approvals.append(approval)
        self.store.log_event(session.session_id, "approval_requested", approval.model_dump())
        return approval

    def resolve(
        self, session: Session, approval_id: str, approved: bool, by: str = "user",
        approve_all: bool = False,
    ) -> ApprovalRequest:
        """Resolve one approval. `approve_all` (with approved=True) additionally
        grants a session-wide standing approval for the approval's category and
        clears its sibling pending approvals of the same category — so promoting
        six files is one deliberate decision, not six identical clicks."""
        approval = next((a for a in session.approvals if a.approval_id == approval_id), None)
        if approval is None:
            raise KeyError(f"no approval {approval_id} on session {session.session_id}")
        approval.status = "approved" if approved else "denied"
        approval.resolved_at = utcnow()
        approval.resolved_by = by
        self.store.log_event(session.session_id, "approval_resolved", approval.model_dump())
        if approved and approve_all and approval.category:
            if approval.category not in session.standing_approvals:
                session.standing_approvals.append(approval.category)
            self.store.log_event(
                session.session_id, "standing_approval_granted",
                {"category": approval.category, "by": by},
            )
            for a in session.approvals:
                if a.status == "pending" and a.category == approval.category:
                    a.status = "approved"
                    a.resolved_at = utcnow()
                    a.resolved_by = by
                    self.store.log_event(session.session_id, "approval_resolved", a.model_dump())
        self.store.save_session(session)
        return approval
