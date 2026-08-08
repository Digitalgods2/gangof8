"""Outcome contracts, reusable playbooks, steering, and artifact workbench.

This module deliberately sits at the edge of the orchestration graph.  Prompt
modules and the service import it, so imports of classifier/goals/rounds/paths
are kept local to :func:`infer_outcome_contract`.  The persistent store shares
``gangof8.db`` with sessions and goals, but owns only its three workbench tables.

Artifact discovery is read-only and capability based: a client receives an
opaque id derived from an exact path already recorded on the session, then
``resolve_artifact`` re-derives that manifest before returning a path.  Arbitrary
paths are never accepted from a request.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional
from urllib.parse import quote

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .models import Budgets


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _stable_id(prefix: str, value: str, length: int = 20) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"{prefix}_{digest[:length]}"


class _WorkbenchModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class OutcomeContract(_WorkbenchModel):
    """A deterministic, editable statement of what a run must accomplish."""

    version: int = 1
    outcome: str = Field(
        default="",
        validation_alias=AliasChoices("outcome", "objective", "desired_outcome"),
    )
    deliverables: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    established_root: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("established_root", "source_root"),
    )
    delivery_root: Optional[str] = None
    task_type: str = "question"
    complexity: str = "standard"
    risk: str = "none"
    execution_mode: str = Field(
        default="pair",
        validation_alias=AliasChoices("execution_mode", "suggested_mode"),
    )
    execution_profile: str = "balanced"
    budgets: Budgets = Field(default_factory=Budgets)
    auto_routed: bool = False
    has_attachments: bool = False
    rationale: str = ""

    @field_validator(
        "deliverables", "acceptance_criteria", "constraints", "exclusions",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return _unique_text(value)

    @property
    def source_root(self) -> Optional[str]:
        """Reader-friendly alias; the persisted field matches Session."""
        return self.established_root


class Playbook(_WorkbenchModel):
    """A portable template distilled from a successful run.

    Machine-specific source and delivery roots are removed by the store before
    persistence.  A playbook describes the work; a new run chooses its own
    project and destination.
    """

    playbook_id: str = Field(default_factory=lambda: _short_id("pb"))
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    task_template: str = Field(
        default="",
        validation_alias=AliasChoices("task_template", "task_text", "prompt"),
    )
    # Kept as JSON rather than a nested model because Session/Goal carry this
    # contract as a dict and playbook runs pass it straight back through intake.
    outcome_contract: dict[str, Any] = Field(default_factory=dict)
    execution_profile: str = "auto"
    participation_mode: str = "adaptive"
    role_agents: dict[str, str] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    delivery_behavior: str = "governed"
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)

    @field_validator("skills", "acceptance_checks", "tags", mode="before")
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return _unique_text(value if isinstance(value, (list, tuple)) else [])


class RunEvaluation(_WorkbenchModel):
    """The operator's idempotent verdict and economics for one session."""

    evaluation_id: str = ""
    session_id: str = Field(min_length=1)
    verdict: str = "unrated"
    rating: Optional[float] = Field(default=None, ge=1, le=5)
    promoted: bool = False
    rejection_reason: str = Field(
        default="",
        validation_alias=AliasChoices("rejection_reason", "reason"),
    )
    notes: str = ""
    cost_usd: Optional[float] = Field(default=None, ge=0)
    elapsed_seconds: Optional[float] = Field(default=None, ge=0)
    model_calls: int = Field(default=0, ge=0)
    agent_calls: int = Field(default=0, ge=0)
    artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _derive_id(self) -> "RunEvaluation":
        if not self.evaluation_id:
            self.evaluation_id = _stable_id("eval", self.session_id)
        self.artifact_ids = _unique_text(self.artifact_ids)
        return self


_DURABLE_STEERING = frozenset({"constraint", "focus"})
_ONE_SHOT_STEERING = frozenset({"finish_now", "increase_budget"})
_STEERING_KINDS = _DURABLE_STEERING | _ONE_SHOT_STEERING
_STEERING_STATUSES = frozenset(
    {"active", "pending", "claimed", "applied", "revoked"}
)


class SteeringCommand(_WorkbenchModel):
    """A durable mid-run directive or an atomically claimed one-shot command."""

    command_id: str = Field(default_factory=lambda: _short_id("steer"))
    session_id: str = Field(min_length=1)
    kind: Literal["constraint", "focus", "finish_now", "increase_budget"]
    directive: str = ""
    amount: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = ""
    durable: bool = False
    claim_token: Optional[str] = None
    claimed_by: Optional[str] = None
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)
    claimed_at: Optional[str] = None
    applied_at: Optional[str] = None
    revoked_at: Optional[str] = None

    @model_validator(mode="after")
    def _normalize_lifecycle(self) -> "SteeringCommand":
        if self.kind not in _STEERING_KINDS:
            raise ValueError(f"unsupported steering kind: {self.kind}")
        self.durable = self.kind in _DURABLE_STEERING
        if not self.status:
            self.status = "active" if self.durable else "pending"
        if self.status not in _STEERING_STATUSES:
            raise ValueError(f"unsupported steering status: {self.status}")
        if self.durable and self.status == "pending":
            self.status = "active"
        if not self.durable and self.status == "active":
            self.status = "pending"
        if self.kind in _DURABLE_STEERING and not self.directive.strip():
            self.directive = str(self.payload.get("text") or "").strip()
        if self.kind in _DURABLE_STEERING and not self.directive.strip():
            raise ValueError(f"{self.kind} steering requires a directive")
        if self.kind == "increase_budget":
            increments: dict[str, Any] = dict(self.payload)
            if not increments:
                try:
                    decoded = json.loads(self.directive or "{}")
                    increments = decoded if isinstance(decoded, dict) else {}
                except (json.JSONDecodeError, TypeError):
                    increments = {}
            if not self.amount:
                if self.directive.strip().isdigit():
                    self.amount = int(self.directive.strip())
                else:
                    self.amount = max(
                        0, int(increments.get("agent_calls") or 0)
                    )
            if self.amount <= 0 and not any(
                max(0, int(increments.get(key) or 0))
                for key in ("rounds", "duration_seconds")
            ):
                raise ValueError("increase_budget requires a positive amount")
            if not self.payload:
                self.payload = increments or {"agent_calls": self.amount}
        elif self.kind in _DURABLE_STEERING and not self.payload:
            self.payload = {"text": self.directive}
        return self


def _unique_text(values: Any, *, limit: Optional[int] = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if limit is not None and len(out) >= limit:
            break
    return out


_ATTACHMENTS_MARKER = "\n\nAttachments provided by the user:"
_DELIVERABLE_RE = re.compile(
    r"(?<![\w.-])(?:[\w.@+()'-]+[\\/])*[\w][\w.@+()'-]*\."
    r"(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|"
    r"md|markdown|txt|rst|rtf|tex|doc|docx|pdf|json|ya?ml|toml|ini|"
    r"cfg|csv|html?|css|scss|svg|png|jpe?g|gif|webp|sh|bat|ps1|sql|"
    r"xlsx?|pptx?|zip)\b",
    re.IGNORECASE,
)
_EXCLUSION_RE = re.compile(
    r"\b(?:do not|don't|must not|never|without|exclude|excluding|avoid)\b",
    re.IGNORECASE,
)
_CONSTRAINT_RE = re.compile(
    r"\b(?:must|shall|required|only|keep|preserve|using|within|limit|"
    r"single[- ]file|compatible|budget|no more than)\b",
    re.IGNORECASE,
)


def _directive_text(text: str) -> str:
    return (text or "").split(_ATTACHMENTS_MARKER, 1)[0].strip()


def _objective(text: str) -> str:
    directive = _directive_text(text)
    directive = re.sub(r"^\s*/goal\b\s*", "", directive, flags=re.IGNORECASE)
    compact = re.sub(r"\s+", " ", directive).strip()
    return compact[:800]


def _deliverables(text: str, task_type: str, produces_output: bool) -> list[str]:
    directive = _directive_text(text)
    names: list[str] = []
    for match in _DELIVERABLE_RE.finditer(directive):
        raw = match.group(0).strip(" `\"'.,;:()[]{}")
        if raw:
            normalized = raw.replace("\\", "/")
            if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/"):
                normalized = normalized.rsplit("/", 1)[-1]
            else:
                normalized = normalized.removeprefix("./")
            names.append(normalized)
    names = _unique_text(names, limit=20)
    if names:
        return names
    if not produces_output:
        return ["A clear, evidence-backed answer"]
    fallback = {
        "action": "A governed action result with an audit trail",
        "code": "The requested working implementation",
        "content": "The requested polished content artifact",
        "design": "The requested design artifact",
    }
    return [fallback.get(task_type, "The requested finished artifact")]


def _constraints_and_exclusions(text: str) -> tuple[list[str], list[str]]:
    directive = _directive_text(text)
    parts = [
        re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", item).strip()
        for item in re.split(r"\r?\n|(?<=[.!?])\s+|;\s+", directive)
    ]
    exclusions = _unique_text(
        (item for item in parts if 5 <= len(item) <= 500 and _EXCLUSION_RE.search(item)),
        limit=12,
    )
    excluded = {item.casefold() for item in exclusions}
    constraints = _unique_text(
        (
            item for item in parts
            if 5 <= len(item) <= 500
            and item.casefold() not in excluded
            and _CONSTRAINT_RE.search(item)
        ),
        limit=12,
    )
    return constraints, exclusions


def infer_outcome_contract(
    text: str,
    role_agents: dict | None = None,
    has_attachments: bool = False,
) -> OutcomeContract:
    """Infer an editable contract using the application's deterministic policy.

    No model call, clock, random id, or filesystem write participates, so equal
    inputs and configuration produce equal contracts.
    """

    # Local imports prevent rounds -> workbench -> rounds and
    # service -> workbench -> service dependency cycles.
    from . import config
    from .classifier import classify
    from .goals import should_auto_route
    from .paths import extract_delivery_target, extract_established_root
    from .rounds import acceptance_requirements

    task_text = str(text or "").strip()
    classification = classify(task_text, role_agents=role_agents)
    auto_routed = should_auto_route(task_text, has_attachments=has_attachments)
    requirements = _unique_text(
        acceptance_requirements(_directive_text(task_text)), limit=24
    )
    constraints, exclusions = _constraints_and_exclusions(task_text)
    complexity = classification.complexity.value
    risk = classification.risk.value

    if auto_routed or complexity == "complex" or risk == "high":
        execution_mode = "team"
        execution_profile = "best_quality"
    elif complexity == "trivial" and risk in {"none", "low"}:
        execution_mode = "solo"
        execution_profile = "fastest"
    else:
        execution_mode = "pair"
        execution_profile = "balanced"

    rationale = classification.rationale
    if auto_routed:
        rationale = f"{rationale}; substantial build auto-routed to an owned team"

    return OutcomeContract(
        outcome=_objective(task_text) or "Complete the user's request",
        deliverables=_deliverables(
            task_text,
            classification.task_type.value,
            classification.produces_output,
        ),
        acceptance_criteria=requirements,
        constraints=constraints,
        exclusions=exclusions,
        established_root=extract_established_root(task_text),
        delivery_root=extract_delivery_target(task_text),
        task_type=classification.task_type.value,
        complexity=complexity,
        risk=risk,
        execution_mode=execution_mode,
        execution_profile=execution_profile,
        budgets=config.budgets_for(classification.complexity),
        auto_routed=auto_routed,
        has_attachments=bool(has_attachments),
        rationale=rationale,
    )


def _prompt_item(value: Any, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def execution_text(
    task_text: str,
    contract: dict | OutcomeContract | None,
) -> str:
    """Append a compact, stable outcome contract to the original task text."""

    original = str(task_text or "").rstrip()
    if not contract:
        return original
    try:
        parsed = (
            contract
            if isinstance(contract, OutcomeContract)
            else OutcomeContract.model_validate(contract)
        )
    except (TypeError, ValueError):
        return original

    if not any(
        (
            parsed.outcome,
            parsed.deliverables,
            parsed.acceptance_criteria,
            parsed.constraints,
            parsed.exclusions,
        )
    ):
        return original

    lines = ["OUTCOME CONTRACT (authoritative; do not silently reinterpret):"]
    if parsed.outcome:
        lines.append(f"Outcome: {_prompt_item(parsed.outcome, 600)}")
    if parsed.deliverables:
        lines.append(
            "Deliverables: "
            + "; ".join(_prompt_item(item) for item in parsed.deliverables[:10])
        )
    if parsed.acceptance_criteria:
        lines.append("Acceptance criteria:")
        lines.extend(
            f"- {_prompt_item(item)}" for item in parsed.acceptance_criteria[:12]
        )
    if parsed.constraints:
        lines.append(
            "Constraints: "
            + "; ".join(_prompt_item(item) for item in parsed.constraints[:8])
        )
    if parsed.exclusions:
        lines.append(
            "Exclusions: "
            + "; ".join(_prompt_item(item) for item in parsed.exclusions[:8])
        )
    if parsed.delivery_root:
        lines.append(f"Delivery target: {_prompt_item(parsed.delivery_root, 500)}")
    lines.append(
        f"Execution: {parsed.execution_mode} / {parsed.execution_profile}"
    )
    suffix = "\n".join(lines)
    return f"{original}\n\n{suffix}" if original else suffix


class WorkbenchStore:
    """WAL-safe persistence for playbooks, evaluations, and steering commands."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "gangof8.db"
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS playbooks (
                       playbook_id TEXT PRIMARY KEY,
                       name        TEXT NOT NULL,
                       created_at  TEXT NOT NULL,
                       updated_at  TEXT NOT NULL,
                       json        TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evaluations (
                       session_id    TEXT PRIMARY KEY,
                       evaluation_id TEXT NOT NULL UNIQUE,
                       updated_at    TEXT NOT NULL,
                       json          TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS steering_commands (
                       command_id TEXT PRIMARY KEY,
                       session_id TEXT NOT NULL,
                       kind       TEXT NOT NULL,
                       status     TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       json       TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_playbooks_updated "
                "ON playbooks(updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evaluations_updated "
                "ON evaluations(updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steering_session_status "
                "ON steering_commands(session_id, status, created_at)"
            )

    @staticmethod
    def _decode(model: type[_WorkbenchModel], raw: str):
        try:
            return model.model_validate_json(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _bounded_limit(limit: Optional[int], default: int) -> int:
        if limit is None:
            return 1000
        try:
            return max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            return default

    # -- playbooks -----------------------------------------------------

    def create_playbook(self, playbook: Playbook | dict) -> Playbook:
        return self.upsert_playbook(playbook)

    def upsert_playbook(self, playbook: Playbook | dict) -> Playbook:
        item = (
            playbook
            if isinstance(playbook, Playbook)
            else Playbook.model_validate(playbook)
        ).model_copy(deep=True)
        # Playbooks are portable by definition.  Never persist a source machine's
        # absolute project or delivery roots in the reusable template.
        item.outcome_contract = dict(item.outcome_contract or {})
        item.outcome_contract["established_root"] = None
        item.outcome_contract["delivery_root"] = None
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM playbooks WHERE playbook_id = ?",
                (item.playbook_id,),
            ).fetchone()
            existing = self._decode(Playbook, row[0]) if row else None
            if existing is not None:
                item.created_at = existing.created_at
            item.updated_at = now
            conn.execute(
                """INSERT INTO playbooks
                       (playbook_id, name, created_at, updated_at, json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(playbook_id) DO UPDATE SET
                       name=excluded.name,
                       updated_at=excluded.updated_at,
                       json=excluded.json""",
                (
                    item.playbook_id,
                    item.name,
                    item.created_at,
                    item.updated_at,
                    item.model_dump_json(),
                ),
            )
        return item

    def get_playbook(self, playbook_id: str) -> Optional[Playbook]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM playbooks WHERE playbook_id = ?",
                (str(playbook_id),),
            ).fetchone()
        return self._decode(Playbook, row[0]) if row else None

    def list_playbooks(self, limit: int = 100) -> list[Playbook]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT json FROM playbooks ORDER BY updated_at DESC, playbook_id "
                "LIMIT ?",
                (self._bounded_limit(limit, 100),),
            ).fetchall()
        return [
            item for row in rows
            if (item := self._decode(Playbook, row[0])) is not None
        ]

    def delete_playbook(self, playbook_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM playbooks WHERE playbook_id = ?",
                (str(playbook_id),),
            )
        return cur.rowcount > 0

    save_playbook = upsert_playbook
    update_playbook = upsert_playbook

    # -- evaluations ---------------------------------------------------

    def upsert_evaluation(
        self, evaluation: RunEvaluation | dict
    ) -> RunEvaluation:
        item = (
            evaluation
            if isinstance(evaluation, RunEvaluation)
            else RunEvaluation.model_validate(evaluation)
        ).model_copy(deep=True)
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM evaluations WHERE session_id = ?",
                (item.session_id,),
            ).fetchone()
            existing = self._decode(RunEvaluation, row[0]) if row else None
            if existing is not None:
                item.created_at = existing.created_at
                item.evaluation_id = existing.evaluation_id
            item.updated_at = now
            conn.execute(
                """INSERT INTO evaluations
                       (session_id, evaluation_id, updated_at, json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       evaluation_id=excluded.evaluation_id,
                       updated_at=excluded.updated_at,
                       json=excluded.json""",
                (
                    item.session_id,
                    item.evaluation_id,
                    item.updated_at,
                    item.model_dump_json(),
                ),
            )
        return item

    def get_evaluation(self, session_id: str) -> Optional[RunEvaluation]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM evaluations WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return self._decode(RunEvaluation, row[0]) if row else None

    def list_evaluations(self, limit: int = 100) -> list[RunEvaluation]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT json FROM evaluations ORDER BY updated_at DESC, session_id "
                "LIMIT ?",
                (self._bounded_limit(limit, 100),),
            ).fetchall()
        return [
            item for row in rows
            if (item := self._decode(RunEvaluation, row[0])) is not None
        ]

    def delete_evaluation(self, session_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM evaluations WHERE session_id = ?",
                (str(session_id),),
            )
        return cur.rowcount > 0

    create_evaluation = upsert_evaluation
    save_evaluation = upsert_evaluation

    # -- steering ------------------------------------------------------

    def add_steering(
        self, command: SteeringCommand | dict
    ) -> SteeringCommand:
        return self.upsert_steering(command)

    def upsert_steering(
        self, command: SteeringCommand | dict
    ) -> SteeringCommand:
        item = (
            command
            if isinstance(command, SteeringCommand)
            else SteeringCommand.model_validate(command)
        ).model_copy(deep=True)
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM steering_commands WHERE command_id = ?",
                (item.command_id,),
            ).fetchone()
            existing = self._decode(SteeringCommand, row[0]) if row else None
            if existing is not None:
                item.created_at = existing.created_at
                # Generic updates may edit an active directive, but may never
                # replay or resurrect a claimed/terminal one-shot.
                if existing.status in {"claimed", "applied", "revoked"}:
                    item.status = existing.status
                    item.claim_token = existing.claim_token
                    item.claimed_by = existing.claimed_by
                    item.claimed_at = existing.claimed_at
                    item.applied_at = existing.applied_at
                    item.revoked_at = existing.revoked_at
            item.updated_at = now
            conn.execute(
                """INSERT INTO steering_commands
                       (command_id, session_id, kind, status, created_at,
                        updated_at, json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(command_id) DO UPDATE SET
                       session_id=excluded.session_id,
                       kind=excluded.kind,
                       status=excluded.status,
                       updated_at=excluded.updated_at,
                       json=excluded.json""",
                (
                    item.command_id,
                    item.session_id,
                    item.kind,
                    item.status,
                    item.created_at,
                    item.updated_at,
                    item.model_dump_json(),
                ),
            )
        return item

    def get_steering(self, command_id: str) -> Optional[SteeringCommand]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM steering_commands WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
        return self._decode(SteeringCommand, row[0]) if row else None

    def list_steering(
        self,
        session_id: Optional[str] = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> list[SteeringCommand]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if not include_inactive:
            clauses.append("status IN ('active', 'pending', 'claimed')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(self._bounded_limit(limit, 200))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT json FROM steering_commands {where} "
                "ORDER BY created_at ASC, command_id LIMIT ?",
                tuple(params),
            ).fetchall()
        return [
            item for row in rows
            if (item := self._decode(SteeringCommand, row[0])) is not None
        ]

    def list_active_directives(
        self, session_id: str
    ) -> list[SteeringCommand]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT json FROM steering_commands
                   WHERE session_id = ?
                     AND kind IN ('constraint', 'focus')
                     AND status = 'active'
                   ORDER BY created_at ASC, command_id""",
                (str(session_id),),
            ).fetchall()
        return [
            item for row in rows
            if (item := self._decode(SteeringCommand, row[0])) is not None
        ]

    def claim_steering(
        self,
        session_id: str,
        claimed_by: str = "worker",
        limit: int = 100,
    ) -> list[SteeringCommand]:
        """Atomically claim pending one-shots exactly once.

        Durable ``constraint`` and ``focus`` records remain active and are read
        through :meth:`list_active_directives`; claiming never consumes them.
        """

        claimed: list[SteeringCommand] = []
        now = _utcnow()
        batch_token = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT command_id, json FROM steering_commands
                   WHERE session_id = ?
                     AND kind IN ('finish_now', 'increase_budget')
                     AND status = 'pending'
                   ORDER BY created_at ASC, command_id
                   LIMIT ?""",
                (
                    str(session_id),
                    self._bounded_limit(limit, 100),
                ),
            ).fetchall()
            for command_id, raw in rows:
                item = self._decode(SteeringCommand, raw)
                if item is None:
                    continue
                item.status = "claimed"
                item.claim_token = batch_token
                item.claimed_by = str(claimed_by or "worker")
                item.claimed_at = now
                item.updated_at = now
                cur = conn.execute(
                    """UPDATE steering_commands
                       SET status = 'claimed', updated_at = ?, json = ?
                       WHERE command_id = ? AND status = 'pending'""",
                    (now, item.model_dump_json(), command_id),
                )
                if cur.rowcount:
                    claimed.append(item)
        return claimed

    def mark_steering_applied(
        self,
        command_id: str,
        claim_token: Optional[str] = None,
    ) -> Optional[SteeringCommand]:
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM steering_commands WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
            item = self._decode(SteeringCommand, row[0]) if row else None
            if item is None or item.status != "claimed":
                return None
            if claim_token is not None and item.claim_token != claim_token:
                return None
            item.status = "applied"
            item.applied_at = now
            item.updated_at = now
            conn.execute(
                """UPDATE steering_commands
                   SET status = 'applied', updated_at = ?, json = ?
                   WHERE command_id = ? AND status = 'claimed'""",
                (now, item.model_dump_json(), item.command_id),
            )
        return item

    def release_steering_claim(
        self, command_id: str, claim_token: str
    ) -> Optional[SteeringCommand]:
        """Return an un-applied one-shot to pending after a failed worker."""

        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM steering_commands WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
            item = self._decode(SteeringCommand, row[0]) if row else None
            if (
                item is None
                or item.status != "claimed"
                or item.claim_token != claim_token
            ):
                return None
            item.status = "pending"
            item.claim_token = None
            item.claimed_by = None
            item.claimed_at = None
            item.updated_at = now
            conn.execute(
                """UPDATE steering_commands
                   SET status = 'pending', updated_at = ?, json = ?
                   WHERE command_id = ? AND status = 'claimed'""",
                (now, item.model_dump_json(), item.command_id),
            )
        return item

    def revoke_steering(
        self, command_id: str
    ) -> Optional[SteeringCommand]:
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM steering_commands WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
            item = self._decode(SteeringCommand, row[0]) if row else None
            if item is None:
                return None
            if item.status == "applied":
                return item
            if item.status != "revoked":
                item.status = "revoked"
                item.revoked_at = now
                item.updated_at = now
                conn.execute(
                    """UPDATE steering_commands
                       SET status = 'revoked', updated_at = ?, json = ?
                       WHERE command_id = ? AND status != 'applied'""",
                    (now, item.model_dump_json(), item.command_id),
                )
        return item

    def revoke_session_steering(
        self,
        session_id: str,
        kinds: Optional[list[str]] = None,
    ) -> list[SteeringCommand]:
        selected = set(kinds or _STEERING_KINDS)
        unknown = selected - _STEERING_KINDS
        if unknown:
            raise ValueError(
                "unsupported steering kinds: " + ", ".join(sorted(unknown))
            )
        now = _utcnow()
        revoked: list[SteeringCommand] = []
        placeholders = ", ".join("?" for _ in selected)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""SELECT command_id, json FROM steering_commands
                    WHERE session_id = ?
                      AND kind IN ({placeholders})
                      AND status NOT IN ('applied', 'revoked')
                    ORDER BY created_at ASC, command_id""",
                (str(session_id), *sorted(selected)),
            ).fetchall()
            for command_id, raw in rows:
                item = self._decode(SteeringCommand, raw)
                if item is None:
                    continue
                item.status = "revoked"
                item.revoked_at = now
                item.updated_at = now
                cur = conn.execute(
                    """UPDATE steering_commands
                       SET status = 'revoked', updated_at = ?, json = ?
                       WHERE command_id = ?
                         AND status NOT IN ('applied', 'revoked')""",
                    (now, item.model_dump_json(), command_id),
                )
                if cur.rowcount:
                    revoked.append(item)
        return revoked

    def delete_steering(self, command_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM steering_commands WHERE command_id = ?",
                (str(command_id),),
            )
        return cur.rowcount > 0

    create_steering = add_steering
    save_steering = upsert_steering


_FILE_ACTION_KINDS = frozenset(
    {"write_file", "edit_file", "promote", "stage", "promote_batch"}
)
_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{24}$")


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {}


def _resolved_dir(raw: Any) -> Optional[Path]:
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        return None
    try:
        path = Path(raw)
        if not path.is_absolute():
            return None
        resolved = path.resolve(strict=True)
        return resolved if resolved.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _authorized_roots(session: Any) -> list[tuple[str, Path]]:
    # Local config import keeps the prompt-facing import graph shallow.
    from . import config

    session_id = str(_value(session, "session_id", "") or "").strip()
    candidates = [
        ("delivery", _value(session, "delivery_root")),
        ("established", _value(session, "established_root")),
        ("workspace", _value(session, "workspace_root")),
        (
            "sandbox",
            Path(config.SANDBOX_ROOT) / session_id if session_id else None,
        ),
    ]
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, raw in candidates:
        root = _resolved_dir(raw)
        if root is None:
            continue
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        roots.append((label, root))
    return roots


def _safe_recorded_file(
    raw: Any, roots: list[tuple[str, Path]]
) -> Optional[Path]:
    """Resolve only an absolute, existing file contained by an authorized root."""

    if not isinstance(raw, (str, os.PathLike)):
        return None
    text = str(raw)
    if not text.strip() or "\x00" in text or "\r" in text or "\n" in text:
        return None
    try:
        path = Path(text)
        if not path.is_absolute():
            return None
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if not any(root in resolved.parents for _, root in roots):
        return None
    return resolved


def _space_and_relative(
    path: Path, roots: list[tuple[str, Path]]
) -> tuple[str, str]:
    matches: list[tuple[int, int, str, str]] = []
    for priority, (label, root) in enumerate(roots):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        matches.append((len(root.parts), -priority, label, rel))
    if not matches:
        raise ValueError("artifact is outside authorized roots")
    _, _, label, relative = max(matches)
    return label, relative


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_kind(path: Path, media_type: str) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".md", ".markdown", ".rst"}:
        return "markdown"
    if media_type.startswith("image/"):
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".doc", ".docx", ".odt", ".rtf"}:
        return "document"
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "table"
    if suffix in {".ppt", ".pptx", ".odp"}:
        return "presentation"
    if suffix in {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
        ".java", ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".sh",
        ".bat", ".ps1", ".sql", ".css", ".scss",
    }:
        return "code"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        return "data"
    if media_type.startswith("text/"):
        return "text"
    return "binary"


def _action_file_paths(
    session: Any,
    roots: list[tuple[str, Path]],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    set[str],
    list[dict[str, Any]],
]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    delivered: set[str] = set()
    batch_actions: list[dict[str, Any]] = []
    for raw_action in _value(session, "proposed_actions", []) or []:
        action = _as_mapping(raw_action)
        kind = str(action.get("kind") or "")
        if kind not in _FILE_ACTION_KINDS:
            continue
        result = action.get("result_path")
        if kind == "promote_batch":
            if action.get("status") == "executed" and result:
                batch_actions.append(action)
            continue
        path = _safe_recorded_file(result, roots)
        if path is None:
            continue
        key = os.path.normcase(str(path))
        by_path.setdefault(key, []).append(action)
        if kind == "promote" and action.get("status") == "executed":
            delivered.add(key)

    # A batch action records its destination directory, while files_changed
    # records every exact released file.  Derive state only (never candidates)
    # from the batch's sealed relative manifest.
    for action in batch_actions:
        root = _resolved_dir(action.get("result_path"))
        if root is None:
            continue
        args = action.get("args") or {}
        try:
            files = json.loads(args.get("files", "[]"))
        except (json.JSONDecodeError, TypeError, AttributeError):
            files = []
        if not isinstance(files, list):
            continue
        for raw_name in files:
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            try:
                target = (root / raw_name).resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if root not in target.parents or not target.is_file():
                continue
            key = os.path.normcase(str(target))
            delivered.add(key)
            by_path.setdefault(key, []).append(action)
    return by_path, delivered, batch_actions


def _verified_hashes(session: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for field in ("verified_output_hashes", "release_verified_hashes"):
        values = _value(session, field, {}) or {}
        if not isinstance(values, dict):
            continue
        for raw_name, raw_digest in values.items():
            name = str(raw_name or "").strip().replace("\\", "/").casefold()
            digest = str(raw_digest or "").strip().lower()
            if name and re.fullmatch(r"[0-9a-f]{64}", digest):
                out[name] = digest
    return out


def _approval_record(approval: Any) -> Optional[dict[str, Any]]:
    data = _as_mapping(approval)
    details = str(data.get("details") or "").strip()
    if not details:
        return None
    return {
        "approval_id": data.get("approval_id"),
        "action_ref": data.get("action_ref"),
        "status": data.get("status"),
        "details": details,
        "requested_at": data.get("requested_at"),
        "resolved_at": data.get("resolved_at"),
        "resolved_by": data.get("resolved_by"),
    }


def _latest_approval(
    session: Any,
    action_ids: Optional[set[str]] = None,
    approval_ids: Optional[set[str]] = None,
) -> Optional[dict[str, Any]]:
    approvals = _value(session, "approvals", []) or []
    for raw in reversed(list(approvals)):
        record = _approval_record(raw)
        if record is None:
            continue
        if action_ids is None and approval_ids is None:
            return record
        if (
            record.get("action_ref") in (action_ids or set())
            or record.get("approval_id") in (approval_ids or set())
        ):
            return record
    return None


def artifact_manifest(session: Any) -> dict[str, Any]:
    """Return a read-only manifest of exact files recorded by ``session``.

    Only ``files_changed`` and file-bearing ``ProposedAction.result_path`` values
    can introduce a candidate.  Action args are used solely to associate a
    recorded file with batch delivery state; they can never authorize a path.
    """

    session_id = str(_value(session, "session_id", ""))
    roots = _authorized_roots(session)
    by_path, delivered_paths, _ = _action_file_paths(session, roots)

    raw_candidates: list[Any] = list(_value(session, "files_changed", []) or [])
    for raw_action in _value(session, "proposed_actions", []) or []:
        action = _as_mapping(raw_action)
        if action.get("kind") in _FILE_ACTION_KINDS:
            raw_candidates.append(action.get("result_path"))

    paths: list[Path] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        path = _safe_recorded_file(raw, roots)
        if path is None:
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    paths.sort(key=lambda item: os.path.normcase(str(item)))

    known_verified = _verified_hashes(session)
    artifacts: list[dict[str, Any]] = []
    encoded_sid = quote(session_id, safe="")
    for path in paths:
        key = os.path.normcase(str(path))
        try:
            size = path.stat().st_size
            digest = _file_digest(path)
            space, relative = _space_and_relative(path, roots)
        except (OSError, RuntimeError, ValueError):
            continue
        actions = by_path.get(key, [])
        action_names = {
            str(action.get("filename") or "")
            .strip()
            .replace("\\", "/")
            .casefold()
            for action in actions
            if action.get("filename")
        }
        names = {
            relative.casefold(),
            path.name.casefold(),
            *action_names,
        }
        verified = any(
            known_verified.get(name) == digest for name in names if name
        )
        delivered = key in delivered_paths
        state = "delivered" if delivered else ("verified" if verified else "candidate")
        normalized = os.path.normcase(str(path))
        artifact_id = _stable_id(
            "art", f"{session_id}\0{normalized}", length=24
        )
        encoded_artifact = quote(artifact_id, safe="")
        action_ids = {
            str(action.get("action_id"))
            for action in actions
            if action.get("action_id")
        }
        approval_ids = {
            str(action.get("approval_id"))
            for action in actions
            if action.get("approval_id")
        }
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        record = {
            "artifact_id": artifact_id,
            "name": path.name,
            "relative_path": relative,
            "path": str(path),
            "space": space,
            "state": state,
            "candidate": state == "candidate",
            "verified": verified,
            "delivered": delivered,
            "kind": _artifact_kind(path, media_type),
            "media_type": media_type,
            "size": size,
            "size_bytes": size,
            "hash": digest,
            "sha256": digest,
            "action_ids": sorted(action_ids),
            "latest_approval_diff": _latest_approval(
                session, action_ids, approval_ids
            ),
            "preview_url": (
                f"/sessions/{encoded_sid}/artifacts/{encoded_artifact}/preview"
            ),
            "download_url": (
                f"/sessions/{encoded_sid}/artifacts/{encoded_artifact}/download"
            ),
        }
        artifacts.append(record)

    counts = {
        state: sum(1 for artifact in artifacts if artifact["state"] == state)
        for state in ("candidate", "verified", "delivered")
    }
    counts["total"] = len(artifacts)
    return {
        "session_id": session_id,
        "artifacts": artifacts,
        "counts": counts,
        "latest_approval_diff": _latest_approval(session),
    }


def resolve_artifact(session: Any, artifact_id: str) -> Path:
    """Resolve an opaque artifact id to an exact, currently authorized file."""

    requested = str(artifact_id or "")
    if not _ARTIFACT_ID_RE.fullmatch(requested):
        raise KeyError("unknown artifact")
    manifest = artifact_manifest(session)
    record = next(
        (
            artifact
            for artifact in manifest["artifacts"]
            if artifact["artifact_id"] == requested
        ),
        None,
    )
    if record is None:
        raise KeyError("unknown artifact")
    roots = _authorized_roots(session)
    path = _safe_recorded_file(record["path"], roots)
    if path is None or os.path.normcase(str(path)) != os.path.normcase(record["path"]):
        raise KeyError("unknown artifact")
    return path


__all__ = [
    "OutcomeContract",
    "Playbook",
    "RunEvaluation",
    "SteeringCommand",
    "WorkbenchStore",
    "infer_outcome_contract",
    "execution_text",
    "artifact_manifest",
    "resolve_artifact",
]
