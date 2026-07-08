"""Core data models (DESIGN.md section 2). All JSON-serializable."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_id() -> str:
    return uuid.uuid4().hex[:8]


class TaskType(str, Enum):
    question = "question"
    research = "research"
    design = "design"
    code = "code"
    content = "content"
    action = "action"


class Complexity(str, Enum):
    trivial = "trivial"
    standard = "standard"
    complex = "complex"


class Risk(str, Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


RISK_ORDER = [Risk.none, Risk.low, Risk.medium, Risk.high]


def risk_gt(a: Risk, b: Risk) -> bool:
    return RISK_ORDER.index(a) > RISK_ORDER.index(b)


class SessionStatus(str, Enum):
    received = "received"
    classified = "classified"
    awaiting_approval = "awaiting_approval"  # human must approve/deny an action gate
    awaiting_input = "awaiting_input"        # an agent asked the human a question
    deliberating = "deliberating"
    composing = "composing"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class Role(str, Enum):
    coordinator = "coordinator"
    lead = "lead"  # the single driver of a task; pulls in the talents below on demand
    panelist = "panelist"  # a full-council seat that contributes every round
    knowledge_retriever = "knowledge_retriever"
    researcher = "researcher"
    architect = "architect"
    code_generator = "code_generator"
    api_integrator = "api_integrator"
    critic = "critic"
    red_team = "red_team"
    fact_validator = "fact_validator"
    implementer = "implementer"
    governance = "governance"
    summarizer = "summarizer"


class Task(BaseModel):
    task_id: str
    session_id: str
    source: str = "cli"
    text: str
    created_at: str = Field(default_factory=utcnow)


class Classification(BaseModel):
    task_type: TaskType
    complexity: Complexity
    risk: Risk
    skills_needed: list[str] = []
    agents_required: list[str] = []
    tools_allowed: bool = False
    human_approval_required: bool = False
    rationale: str = ""
    # need flags driving council selection (loop step 3)
    needs_facts: bool = True
    needs_design: bool = False
    produces_output: bool = False
    quality_matters: bool = True
    needs_governance: bool = False
    greenfield: bool = False  # builds something NEW — needs a target if none referenced


class CouncilMember(BaseModel):
    role: Role
    agent: Optional[str] = None
    active: bool = False


class Council(BaseModel):
    members: list[CouncilMember] = []

    def get(self, role: Role) -> Optional[CouncilMember]:
        return next((m for m in self.members if m.role == role), None)

    def is_active(self, role: Role) -> bool:
        m = self.get(role)
        return bool(m and m.active)

    def active_roles(self) -> set[Role]:
        return {m.role for m in self.members if m.active}


class RoundSpec(BaseModel):
    round: int
    goal: str
    agents: list[Role]
    max_turns: int = 1
    stop_condition: str = "all assigned agents returned, or timeout"
    output_requirement: str = "non-empty contribution"
    timeout_s: int = 120


class Contribution(BaseModel):
    round: int
    role: Role
    agent: str
    content: str
    model: Optional[str] = None  # the exact model that produced it, when known
    tokens: int = 0
    duration_ms: int = 0
    ts: str = Field(default_factory=utcnow)


class Disagreement(BaseModel):
    topic: str
    positions: list[dict[str, str]] = []
    critic_test: Optional[str] = None
    ruling: Optional[str] = None
    ruling_basis: Optional[str] = None  # evidence | constraint | user_goal
    rationale: Optional[str] = None


class TruthClaim(BaseModel):
    """One claim in the session's source-of-truth ledger.

    The ledger is deliberately conservative: unsupported claims remain
    assumptions until a validator or sourced contribution promotes them.
    """

    claim_id: str = Field(default_factory=lambda: f"tc_{short_id()}")
    claim: str
    source: Optional[str] = None
    confidence: float = 0.5
    asserted_by: Role
    asserted_agent: str = ""
    asserted_round: int = 0
    verified_by: list[str] = []
    refuted_by: list[str] = []
    status: str = "assumption"  # established | assumption | disputed | deprecated
    checked_at: str = Field(default_factory=utcnow)


class InputRequest(BaseModel):
    """An agent paused mid-call to ask the human a question. The answer resumes
    the same underlying agent task."""

    input_id: str = Field(default_factory=lambda: f"i_{short_id()}")
    session_id: str
    agent: str
    role: Role = Role.coordinator
    round: int = 0
    # deliberation | compose | continue_rounds | promote_target | establish_target (legacy)
    purpose: str = "deliberation"
    question: str
    resume_token: str  # backend handle for the paused call
    status: str = "pending"  # pending | answered | declined
    answer: Optional[str] = None
    asked_at: str = Field(default_factory=utcnow)
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: f"a_{short_id()}")
    session_id: str
    action: str
    category: str  # file_write, file_delete, code_exec, send_message, spend, settings, external
    risk: Risk = Risk.medium
    status: str = "pending"  # pending | approved | denied
    action_ref: Optional[str] = None  # ProposedAction id — denial skips the action, not the session
    details: Optional[str] = None  # extra review context shown in the approval (e.g. a promote diff)
    requested_at: str = Field(default_factory=utcnow)
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None


class ProposedAction(BaseModel):
    """A concrete side effect an agent wants — never executed without an
    explicitly approved ApprovalRequest (unless the skill's metadata declares
    no approval needed). The `kind` names a skill in the registry-driven
    permission kernel (conclave_os.skills); the kernel role-gates and gates on
    that skill's metadata rather than on hardcoded write_file literals.

    `args` carries the skill inputs (preferred); `filename`/`content` remain for
    back-compat with the write_file artifact path. `role` is the proposing role,
    so the kernel can role-gate the action."""

    action_id: str = Field(default_factory=lambda: f"act_{short_id()}")
    session_id: str
    kind: str = "write_file"
    role: Role = Role.implementer
    args: dict[str, str] = {}
    filename: str = ""
    content: str = ""
    status: str = "proposed"  # proposed | awaiting_approval | approved | denied | executed | failed
    approval_id: Optional[str] = None
    result_path: Optional[str] = None
    error: Optional[str] = None
    proposed_at: str = Field(default_factory=utcnow)


class Workspace(BaseModel):
    """An allowed work area — a real project directory the council may read and
    (with approval) write into, instead of the throwaway per-session sandbox."""

    id: str = Field(default_factory=lambda: f"ws_{short_id()}")
    name: str
    root: str  # absolute path to the workspace root
    created_at: str = Field(default_factory=utcnow)


class Budgets(BaseModel):
    """The hard bounds on a run. Deliberation terminates on ROUND: DONE, a
    declined consent, the call budget, or wall time — there is no round cap."""

    max_agent_calls: int = 12
    max_wall_seconds: int = 600
    # How deep the delegation tree may go (1 = lead → specialist only) and how
    # many CONSULT:/DELEGATE: grants one reply may fan out. Scaled by task
    # complexity in config.BUDGETS_BY_COMPLEXITY.
    max_delegation_depth: int = 2
    max_delegations: int = 4


class FinalAnswer(BaseModel):
    answer: str
    confidence: str = "low"  # high | medium | low
    assumptions: list[str] = []
    risks_unresolved: list[str] = []
    next_action: Optional[str] = None


class Session(BaseModel):
    session_id: str
    status: SessionStatus = SessionStatus.received
    backend: str = "mock"  # which adapter family this session runs on; resume must match
    workspace_root: Optional[str] = None  # the council's PERMANENT work area; None ⇒ sandbox only
    established_root: Optional[str] = None  # external real folder (per task): read-only; the
    #                                         approval-gated promote target. Never written directly.
    established_asked: bool = False  # the greenfield "where should this go?" gate has been resolved
    delivery_root: Optional[str] = None  # explicit "save it in <X>" DESTINATION the task
    #                                      named — distinct from the READ source above; where
    #                                      promote lands, so a "read A, save to B" task never
    #                                      overwrites its source. None ⇒ deliver to established_root.
    panel: list[str] = []  # seat names convened for this session (resume-stable)
    cli_timeouts: dict[str, int] = {}  # per-seat call timeout (s), from Settings; {} ⇒ config defaults
    # Approval categories the human granted a session-wide standing approval for
    # (e.g. "promote" via 'Approve all'): one deliberate grant instead of N
    # identical clicks. Session-scoped — a new task starts clean.
    standing_approvals: list[str] = []
    consent_extra_rounds: int = 0  # rounds the human granted beyond ROUNDS_PER_CONSENT
    compose_now: bool = False  # human said "finish" — skip further rounds, compose from the work so far
    test_fix_attempts: int = 0  # goal-loop repairs spent (persisted: a pause can't reset the clock)
    turns: list[dict] = []  # the conversation: [{role:'user'|'council', text}] — grows as the
    #                         human responds to a conclusion and the council deliberates again.
    attachments: list[dict] = []  # [{id, name, kind}] folded into the task text
    budgets: Budgets = Field(default_factory=Budgets)
    budgets_locked: bool = False  # True when caller supplied explicit budgets
    task: Task
    classification: Optional[Classification] = None
    council: Council = Field(default_factory=Council)
    rounds: list[RoundSpec] = []
    contributions: list[Contribution] = []
    disagreements: list[Disagreement] = []
    truth_claims: list[TruthClaim] = []
    approvals: list[ApprovalRequest] = []
    input_requests: list[InputRequest] = []
    proposed_actions: list[ProposedAction] = []
    tools_called: list[str] = []
    files_changed: list[str] = []
    unresolved: list[str] = []
    final: Optional[FinalAnswer] = None
    agent_calls: int = 0
    current_round: int = 0
    stop_reason: Optional[str] = None
    blocked_on_missing_info: bool = False
    risk_exceeds_boundary: bool = False
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    @property
    def has_pending_approval(self) -> bool:
        return any(a.status == "pending" for a in self.approvals)

    @property
    def has_pending_input(self) -> bool:
        return any(r.status == "pending" for r in self.input_requests)
