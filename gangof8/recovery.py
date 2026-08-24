"""Evidence-backed recovery primitives shared by sessions and goals.

The coordinator used to encode failures in prose and then reopen work through
several unrelated branches.  This module keeps the feedback cycle small and
durable: observe a concrete failure, fingerprint it, choose a changed repair,
record the attempt, and stop when the same fault has exhausted its useful
strategies.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .models import (
    DefectRecord,
    FailureRecord,
    RecoveryState,
    RepairAttempt,
    utcnow,
)


def _stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fault_signature(
    stage: str,
    category: str,
    summary: str,
    *,
    artifact_path: str = "",
    artifact_hash: str = "",
    evidence: Optional[dict] = None,
) -> str:
    stable_evidence = {
        str(key): value for key, value in (evidence or {}).items()
        if str(key).lower() not in {
            "diagnostic", "recorded_at", "updated_at", "created_at",
            "session_id", "sandbox", "cwd", "attempt",
        }
    }
    payload = {
        "stage": stage,
        "category": category,
        "summary": " ".join((summary or "").split())[:1000],
        "artifact_path": artifact_path.replace("\\", "/"),
        "artifact_hash": artifact_hash,
        "evidence": stable_evidence,
    }
    return hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()


def record_failure(
    ledger_owner: Any,
    *,
    stage: str,
    category: str,
    summary: str,
    evidence: Optional[dict] = None,
    artifact_path: str = "",
    artifact_hash: str = "",
    responsible_owner: str = "",
    owner: str = "",
    recoverable: bool = True,
    validator_id: str = "",
    producer_paths: Optional[list[str]] = None,
    expected_hash: str = "",
    command_result: Optional[dict] = None,
    severity: str = "error",
    blocks_release: bool = True,
    action_id: str = "",
    failure_layer: str = "",
    caused_by_failure_id: str = "",
    retry_classification: str = "",
    criterion_id: str = "",
    observed_checkpoint_id: str = "",
) -> FailureRecord:
    # ``owner`` is the natural spelling at call sites and is also the persisted
    # FailureRecord field.  The first version named the ledger object ``owner``
    # too, so passing ``record_failure(session, owner="codex", ...)`` raised
    # "multiple values for argument 'owner'" while handling the original
    # failure.  Keep both keyword spellings and make the ledger parameter
    # unambiguous; recovery must never crash because diagnostics used an alias.
    accountable_owner = responsible_owner or owner
    signature = fault_signature(
        stage,
        category,
        summary,
        artifact_path=artifact_path,
        artifact_hash=artifact_hash,
        evidence=evidence,
    )
    record = next(
        (
            item for item in reversed(ledger_owner.failure_records)
            if item.fault_signature == signature
            # Exhaustion means the bounded automatic strategies stopped; it
            # does not make a repeated identical fault a new defect.  Reuse the
            # same ledger row until verification actually resolves it.
            and item.resolution_state != "resolved"
        ),
        None,
    )
    if record is None:
        record = FailureRecord(
            stage=stage,
            category=category,
            summary=summary,
            evidence=evidence or {},
            evidence_history=[evidence or {}],
            artifact_path=artifact_path,
            artifact_hash=artifact_hash,
            expected_hash=expected_hash,
            validator_id=validator_id,
            command_result=command_result or {},
            owner=accountable_owner,
            fault_signature=signature,
            recoverable=recoverable,
            severity=severity,
            blocks_release=blocks_release,
            repair_scope=list(producer_paths or []),
            action_id=action_id,
            failure_layer=failure_layer,
            caused_by_failure_id=caused_by_failure_id,
            retry_classification=retry_classification,
        )
        ledger_owner.failure_records.append(record)
    else:
        record.occurrences += 1
        record.updated_at = utcnow()
        record.evidence = evidence or {}
        record.evidence_history.append(evidence or {})
        record.command_result = command_result or record.command_result
        record.owner = accountable_owner or record.owner
        record.recoverable = bool(record.recoverable and recoverable)
        record.severity = severity or record.severity
        record.blocks_release = bool(record.blocks_release or blocks_release)
        record.repair_scope = list(dict.fromkeys(
            [*record.repair_scope, *(producer_paths or [])]
        ))
    if ((validator_id or artifact_path)
            and not any(
                defect.failure_id == record.failure_id
                and defect.validator_id == validator_id
                and defect.artifact_path == artifact_path
                and defect.status != "resolved"
                for defect in ledger_owner.defect_ledger
            )):
        ledger_owner.defect_ledger.append(DefectRecord(
            failure_id=record.failure_id,
            validator_id=validator_id,
            artifact_path=artifact_path,
            artifact_hash=artifact_hash,
            producer_paths=list(producer_paths or []),
            description=summary,
            criterion_id=criterion_id,
            severity=severity,
            blocks_release=blocks_release,
            observed_checkpoint_id=observed_checkpoint_id,
            target_producer_paths=list(producer_paths or []),
        ))
    ledger_owner.recovery_state = (
        RecoveryState.observing if recoverable
        else RecoveryState.manual_intervention_required
    )
    return record


def begin_repair(
    owner: Any,
    failure: FailureRecord,
    *,
    repair_owner: str,
    strategy: str,
    input_hashes: Optional[dict[str, str]] = None,
) -> Optional[RepairAttempt]:
    """Start a repair only when it changes a relevant retry variable."""
    hashes = dict(input_hashes or {})
    normalized_strategy = re.sub(
        r"(?:[_-]?(?:attempt|retry))?[_-]?\d+$", "", strategy.strip(),
        flags=re.IGNORECASE,
    ) or strategy.strip()
    fingerprint_payload = {
        "fault_signature": failure.fault_signature,
        "owner": repair_owner,
        "strategy": normalized_strategy,
        "input_hashes": hashes,
        "base_checkpoint_id": str(
            getattr(owner, "active_verified_checkpoint_id", "") or ""
        ),
    }
    attempt_fingerprint = hashlib.sha256(
        _stable(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    for prior in owner.repair_history:
        if (prior.attempt_fingerprint == attempt_fingerprint
                and prior.status != "verified"):
            return None
    attempt = RepairAttempt(
        failure_id=failure.failure_id,
        defect_ids=[
            defect.defect_id for defect in owner.defect_ledger
            if defect.failure_id == failure.failure_id and defect.status == "open"
        ],
        owner=repair_owner,
        strategy=strategy,
        input_hashes=hashes,
        fault_signature=failure.fault_signature,
        before_hashes=hashes,
        attempt_fingerprint=attempt_fingerprint,
        base_checkpoint_id=str(
            getattr(owner, "active_verified_checkpoint_id", "") or ""
        ),
    )
    owner.repair_history.append(attempt)
    owner.recovery_state = RecoveryState.repairing
    for defect in owner.defect_ledger:
        if defect.defect_id in attempt.defect_ids:
            defect.status = "repairing"
    return attempt


def finish_repair(
    owner: Any,
    attempt: RepairAttempt,
    *,
    verified: bool,
    changed_files: Optional[Iterable[str]] = None,
    after_hashes: Optional[dict[str, str]] = None,
    verification_evidence: Optional[dict] = None,
    result_checkpoint_id: str = "",
) -> None:
    attempt.changed_files = list(dict.fromkeys(changed_files or []))
    attempt.completed_at = utcnow()
    attempt.status = "verified" if verified else "failed"
    attempt.after_hashes = dict(after_hashes or {})
    attempt.verification_evidence = dict(verification_evidence or {})
    attempt.result_checkpoint_id = result_checkpoint_id
    owner.recovery_state = RecoveryState.recovered if verified else RecoveryState.observing
    failure = next(
        (item for item in owner.failure_records if item.failure_id == attempt.failure_id),
        None,
    )
    if failure is not None and verified:
        failure.resolution_state = "resolved"
        failure.updated_at = utcnow()
    for defect in owner.defect_ledger:
        if defect.defect_id not in attempt.defect_ids:
            continue
        defect.status = "resolved" if verified else "open"
        defect.resolved_at = utcnow() if verified else None


def seal_checkpoint(owner: Any, **evidence: Any) -> dict:
    checkpoint = {"sealed_at": utcnow(), **evidence}
    owner.last_good_checkpoint = checkpoint
    return checkpoint


@dataclass(frozen=True)
class RecoveryDecision:
    action: str  # retry_owner | frontier_takeover | exhausted
    owner: str
    attempt: int
    signature: str


def choose_goal_recovery(
    goal: Any,
    failure: FailureRecord,
    current_owner: str,
    frontier_seats: Iterable[str],
) -> RecoveryDecision:
    """Owner retry once, then one changed-owner frontier takeover, then stop."""
    signature = failure.fault_signature
    attempt = int(goal.recovery_attempts.get(signature, 0)) + 1
    goal.recovery_attempts[signature] = attempt
    if not failure.recoverable:
        return RecoveryDecision("exhausted", current_owner, attempt, signature)
    if attempt == 1:
        return RecoveryDecision("retry_owner", current_owner, attempt, signature)
    replacement = next(
        (seat for seat in frontier_seats if seat and seat != current_owner), ""
    )
    if attempt == 2 and replacement:
        return RecoveryDecision("frontier_takeover", replacement, attempt, signature)
    return RecoveryDecision("exhausted", current_owner, attempt, signature)


def mark_exhausted(owner: Any, failure: FailureRecord) -> None:
    owner.recovery_state = RecoveryState.manual_intervention_required
    failure.resolution_state = "exhausted"
    failure.updated_at = utcnow()
    for defect in owner.defect_ledger:
        if defect.failure_id == failure.failure_id and defect.status != "resolved":
            defect.status = "exhausted"
