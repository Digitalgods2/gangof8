"""Core data models (DESIGN.md section 2). All JSON-serializable."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

SESSION_SCHEMA_VERSION = 3

# Intake contracts are user-editable, so their execution budget needs a
# generous but finite safety envelope. Runtime steering remains the explicit,
# audited path for extending an active run.
MAX_AGENT_CALLS = 500
MAX_WALL_SECONDS = 86_400
MAX_DELEGATION_DEPTH = 8
MAX_DELEGATIONS = 64


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
    # The user's directive before attachment bodies or coordinator context are
    # folded into ``text``.  Run cloning and playbooks use this field so they
    # never duplicate extracted attachment text.
    original_text: str = ""
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
    match_source: bool = False  # output must MATCH a named source's structure/format
    #                             exactly (a "matched set") — structural fidelity is
    #                             a hard requirement the judges/finisher must weigh
    deliverable_formats: list[str] = []  # non-text output formats the task NAMES as
    #                             its deliverable ("pdf", "docx", "zip"). ARTIFACT
    #                             cannot carry these, so the delivery gate requires a
    #                             real produced file of that format — a generator
    #                             script is not the deliverable. Never set on a
    #                             `code` task, where the source IS the deliverable.


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
    permission kernel (gangof8.skills); the kernel role-gates and gates on
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

    max_agent_calls: int = Field(default=12, ge=1, le=MAX_AGENT_CALLS)
    max_wall_seconds: int = Field(default=600, ge=1, le=MAX_WALL_SECONDS)
    # How deep the delegation tree may go (1 = lead → specialist only) and how
    # many CONSULT:/DELEGATE: grants one reply may fan out. Scaled by task
    # complexity in config.BUDGETS_BY_COMPLEXITY.
    max_delegation_depth: int = Field(
        default=2, ge=1, le=MAX_DELEGATION_DEPTH
    )
    max_delegations: int = Field(default=4, ge=1, le=MAX_DELEGATIONS)


class FinalAnswer(BaseModel):
    answer: str
    confidence: str = "low"  # high | medium | low
    assumptions: list[str] = []
    risks_unresolved: list[str] = []
    next_action: Optional[str] = None


class IntegrationProposal(BaseModel):
    """A validated, optional merge of complementary panel candidates."""

    filename: str
    content: str
    rationale: str
    source_candidates: list[str] = []
    status: str = "pending"  # pending | adopted | kept_winner
    # Who the merge competes against — so the human decides between two NAMED
    # options with their credentials, not "winner vs integration" in the abstract.
    winner_agent: str = ""
    winner_score: Optional[int] = None   # aggregate blind-vote score
    winner_votes: Optional[int] = None   # first-place votes received
    judges: Optional[int] = None         # judges that scored (votes/judges)
    chair: str = ""                      # e.g. "chair ratified the vote"
    # tri-state: True/False = verified/not; None = unknown (pre-upgrade session),
    # so the UI omits the claim instead of mis-stating "not checked"
    runtime_checked: Optional[bool] = None


class GoalMilestone(BaseModel):
    """One bounded goal step, or one owned package in a build-team graph."""

    index: int
    title: str
    task_text: str
    # Build-team goals treat milestones as owned work packages.  Legacy goals
    # leave these empty and retain the original sequential council workflow.
    package_id: str = ""
    owner: str = ""
    # Hard dependencies block scheduling until verified bytes exist in shared
    # staging. Contract dependencies expose another package's declared
    # interface immediately and therefore do NOT block parallel authoring.
    depends_on: list[int] = []  # zero-based hard/artifact package indexes
    contract_depends_on: list[int] = []  # zero-based non-blocking interface indexes
    interface_contract: str = ""
    status: str = "pending"  # pending | running | done | failed | cancelled
    session_id: Optional[str] = None  # the deliberation that ran/is running it
    # The planner declares the files and validation context up front.  A goal
    # can only move forward after these exact outputs have been accepted.
    # ``contract_declared`` distinguishes an intentional analysis-only
    # ``OUTPUTS: NONE`` from a planner that simply forgot the contract.  The
    # latter must never silently advance a build goal.
    contract_declared: bool = False
    requires_delivery: bool = False
    contract_error: str = ""  # malformed/missing planner contract; never accepted
    required_files: list[str] = []
    # Internal package outputs stay in goal staging.  Only this explicit subset
    # may cross into the user's delivery folder during the one final release.
    # This prevents source modules, build tools, and smoke harnesses from being
    # shipped merely because the integration package needed them internally.
    release_files: list[str] = []
    release_declared: bool = False
    dependencies: list[str] = []
    # Deterministic final assembly. ``html_inline`` expands explicit template
    # directives from accepted staged dependencies without sending dependency
    # bodies through a model. ``assembly_template`` is either an accepted
    # relative template path or ``owner`` for one compact glue-only author call.
    assembly_mode: str = ""
    assembly_template: str = ""
    # Only coordinator-recognised static checks run automatically.  Functional
    # tests are represented by a governed RUNTESTS action instead.
    acceptance_commands: list[str] = []
    files: list[str] = []             # accepted, delivered files only (legacy name)
    accepted_files: list[str] = []    # explicit manifest; never sandbox drafts
    accepted_hashes: dict[str, str] = {}  # relative delivery path -> SHA-256
    # Accepted-byte provenance by relative output path. Deterministic transforms
    # identify source hashes and never falsely credit a zero-call owner as author.
    output_provenance: dict[str, dict] = {}
    # Downstream deterministic validation can prove that an otherwise completed
    # upstream attempt violated its template contract. Resume must not silently
    # recover that exact session as verified work again.
    invalidated_session_ids: list[str] = []
    acceptance_detail: str = ""
    summary: str = ""                 # snippet of its final answer, for context


class CollaborationAssignment(BaseModel):
    """One deterministic, artifact-aware contribution by an enabled resource.

    Package ownership remains atomic; this record tracks a peer model that was
    shown the owner's real baseline and asked for findings and concrete edits.
    It is persisted so cancellation/restart never turns a successful call into
    an invisible or duplicated contribution.
    """

    assignment_id: str = Field(default_factory=lambda: f"ca_{short_id()}")
    seat: str
    lens: str
    status: str = "pending"  # pending | running | contributed | failed | unavailable | stopped
    attempts: int = 0
    findings: list[str] = []
    patch_files: list[str] = []
    disposition: str = ""
    contribution_index: Optional[int] = None
    error: str = ""


class Goal(BaseModel):
    """A long-horizon objective with an explicitly versioned collaboration flow.

    New build-team goals schedule owned packages from a dependency graph into
    shared staging and release one final batch. Persisted legacy goals retain
    their sequential tournament/milestone behavior.
    """

    goal_id: str = Field(default_factory=lambda: f"g_{short_id()}")
    text: str
    # Product-level intent that remains stable while package prompts evolve.
    # Stored as a versioned JSON object so older Goal records remain valid.
    outcome_contract: dict = Field(default_factory=dict)
    execution_profile: str = "auto"
    routing_decision: dict = Field(default_factory=dict)
    playbook_id: Optional[str] = None
    parent_goal_id: Optional[str] = None
    status: str = "planning"  # planning | running | draining | paused | completed | failed | cancelled
    milestones: list[GoalMilestone] = []
    current_index: int = 0
    planned_by: str = ""      # agent that authored the milestone plan
    # Planning precedes the first milestone Session. Persist its live call here
    # so the dashboard can supervise and stop it just like any session call.
    active_agent_calls: list[dict] = []
    # Enabled build participants frozen when the goal is created. Settings may
    # change while a long goal runs; its promised collaboration roster may not.
    build_roster: list[str] = []
    # Package owners and collaboration resources are deliberately separate.
    # A resource may challenge/review real artifact bytes without owning any
    # file. This keeps enabled seats such as DeepSeek useful even when no named
    # specialist role currently maps to them.
    resource_roster: list[str] = []
    participation_mode: str = "focused"  # focused | adaptive | full_council
    plan_rationale: str = ""
    last_error: str = ""      # why the goal paused/failed, for the UI
    # Workflow versioning is explicit so goals persisted before the build-team
    # overhaul never change semantics halfway through a run.
    collaboration_mode: str = "tournament"  # tournament | build_team
    delivery_mode: str = "milestone"        # milestone | final_batch
    background: bool = False
    staging_root: str = ""
    established_root: Optional[str] = None
    delivery_root: Optional[str] = None
    release_session_id: Optional[str] = None
    release_status: str = "not_started"  # not_started | awaiting_target | awaiting_approval | released | denied | failed
    release_files: list[str] = []
    # Semantic defects and unapplied frontier repairs survive a release retry;
    # otherwise an inconsistent second reading can silently forget a known bug.
    release_defects: list[str] = []
    # A goal-level epoch invalidates a planner/milestone worker that belonged to
    # an earlier cancelled or retried run.  The short-lived lease is persisted
    # by GoalStore while planning/advancing metadata.
    epoch: int = 0
    worker_lease: str = ""
    # Counts consecutive times assembly-failure attribution has blamed the
    # SAME upstream package for the SAME fault, keyed by
    # "{provider_index}:{scope}:{path}". A defect that attribution keeps
    # misidentifying (or a genuinely unfixable one) would otherwise repeat
    # this rebuild-and-retry cycle indefinitely; see
    # ``Service._assembly_fault_streak_exceeded``.
    assembly_fault_streak: dict[str, int] = {}
    # Economics (ARCHITECTURE-REVIEW.md Phase 3): the goal's model-call ledger.
    # Every planning call and every counted session folds its spend in here so
    # a runaway goal is visible and budget-capped — never another silent 283.
    model_calls_used: int = 0
    model_calls_by_seat: dict[str, int] = {}
    # 0 = use config.GOAL_MAX_MODEL_CALLS. An explicit human resume of a
    # budget-paused goal raises this by another default block.
    model_calls_budget: int = 0
    # Sessions already folded into the ledger — spend is counted exactly once.
    counted_session_ids: list[str] = []
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    @property
    def current(self) -> Optional[GoalMilestone]:
        if 0 <= self.current_index < len(self.milestones):
            return self.milestones[self.current_index]
        return None


class Session(BaseModel):
    schema_version: int = SESSION_SCHEMA_VERSION
    session_id: str
    status: SessionStatus = SessionStatus.received
    # An editable, versioned goal-to-result contract inferred at intake (or
    # supplied by the caller after preview).  It is deliberately separate from
    # Task.text: the dashboard can show the original request while every model
    # receives the same explicit deliverables and success criteria.
    outcome_contract: dict = Field(default_factory=dict)
    execution_profile: str = "auto"
    routing_decision: dict = Field(default_factory=dict)
    playbook_id: Optional[str] = None
    parent_session_id: Optional[str] = None
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
    collaboration_mode: str = "tournament"
    delivery_mode: str = "immediate"
    work_package_id: str = ""
    work_package_owner: str = ""
    # Other enabled seats remain visible for roster/provenance reporting, while
    # the current owner authors the package's cohesive output set atomically.
    package_helpers: list[str] = []
    package_output_authors: dict[str, str] = {}
    package_output_attempts: dict[str, int] = {}
    # Append-only per-path provenance. ``package_output_authors`` identifies
    # the current/delivering author; this ledger preserves failed primaries and
    # every correction/failover without relabeling sibling work.
    package_output_history: dict[str, list[dict]] = {}
    package_call_failures: dict[str, str] = {}
    # Full-council collaboration is a post-baseline challenge wave. The owner
    # remains accountable for final bytes; peers contribute findings/EDITs and
    # the owner explicitly disposes of them during integration.
    resource_roster: list[str] = []
    participation_mode: str = "focused"
    collaboration_assignments: list[CollaborationAssignment] = []
    collaboration_baseline: dict[str, str] = {}
    collaboration_integrated_files: list[str] = []
    collaboration_integration_status: str = "not_started"
    package_started_at: Optional[str] = None
    package_deadline_at: Optional[str] = None
    # The LLM intent pass (intent.py): what a model read the task as asking for.
    # Kept as the raw payload for audit — the classification it produced is
    # already merged into `classification`.
    intent: dict = {}
    intent_reviewed: bool = False   # the pass ran (or was deliberately skipped)
    intent_clarified: bool = False  # the ambiguity question was asked ONCE
    intent_clarification: str = ""  # what the user answered, verbatim
    # The seats that count as FRONTIER-CLASS for THIS run, resolved from the
    # enabled roster at submit time (roles.resolve_frontier_authors). Frozen on
    # the session so a settings change mid-run cannot move the goalposts. Empty
    # on sessions persisted before this field existed — readers fall back to
    # config.FRONTIER_AUTHOR_SEATS so those resume exactly as they ran.
    frontier_author_seats: list[str] = []
    # Frontier models are implementation quorum, not optional late judges.
    required_frontier_authors: list[str] = []
    frontier_author_recoveries: dict[str, int] = {}
    # Best-of-all protocol corrections are tracked separately from frontier
    # runtime repairs: every enabled candidate author is treated equally.
    candidate_author_recoveries: dict[str, int] = {}
    # Persist the real author/runtime funnel and semantic release gate.
    candidate_metrics: dict = {}
    quality_gate: dict = {}
    # Package artifact hashes captured by the deterministic verifier. Recovery
    # may adopt a completed attempt only when its current result paths still
    # match this seal.
    verified_output_hashes: dict[str, str] = {}
    # Exact staging hashes sealed only after final release verification passes.
    # Final approval/promotion is bound to these bytes; any repair or drift must
    # clear/recompute the seal through the full release gate.
    release_verified_hashes: dict[str, str] = {}
    # Contract-linked JavaScript can be authored before its provider exists.
    # Runtime validation is deferred until integration for these exact pending
    # package outputs; static checks still run immediately.
    deferred_runtime_dependencies: list[str] = []
    # Persisted call activity gives the API/UI an honest heartbeat while a
    # blocking CLI or HTTP model call is in flight.
    active_agent_calls: list[dict] = []
    goal_background: bool = False
    goal_release: bool = False
    # Legacy settings snapshot; normal model calls ignore it and are supervised
    # by the operator instead of an elapsed-time cutoff.
    cli_timeouts: dict[str, int] = {}
    integration_review_enabled: bool = False
    integration_proposal: Optional[IntegrationProposal] = None
    # Approval categories the human granted a session-wide standing approval for
    # (e.g. "promote" via 'Approve all'): one deliberate grant instead of N
    # identical clicks. Session-scoped — a new task starts clean.
    standing_approvals: list[str] = []
    consent_extra_rounds: int = 0  # rounds the human granted beyond ROUNDS_PER_CONSENT
    compose_now: bool = False  # human said "finish" — skip further rounds, compose from the work so far
    test_fix_attempts: int = 0  # goal-loop repairs spent (persisted: a pause can't reset the clock)
    artifact_repair_attempts: int = 0  # deterministic validation-repair attempts
    turns: list[dict] = []  # the conversation: [{role:'user'|'council', text}] — grows as the
    #                         human responds to a conclusion and the council deliberates again.
    goal_id: Optional[str] = None       # set when this session runs one Goal milestone
    goal_milestone: Optional[int] = None  # which milestone (index) it runs
    goal_epoch: int = 0  # Goal.epoch captured when this milestone session started
    # Goal acceptance contract copied from GoalMilestone when the session is
    # opened.  Empty fields preserve the ordinary one-shot session behaviour.
    required_files: list[str] = []
    runtime_dependencies: list[str] = []
    dependency_hashes: dict[str, str] = {}
    assembly_mode: str = ""
    assembly_template: str = ""
    # Persisted audit data for deterministic assembly and read-only APIs.
    assembly_result: dict = {}
    # A revision target is both an input and an output (for example editing
    # ``arcade.html`` in place).  Its baseline is recorded for audit/conflict
    # detection, but it must not be treated as an immutable dependency after
    # the author has intentionally changed it.
    revision_targets: list[str] = []
    revision_base_hashes: dict[str, str] = {}
    # Where each revision target's authoritative pre-edit bytes came from.
    # Post-release repairs must use the delivered established copy, while
    # package-to-package revisions normally use goal staging.
    revision_source_spaces: dict[str, str] = {}
    revision_api_contract: dict[str, list[str]] = {}
    revision_assertions: dict[str, list[str]] = {}
    acceptance_commands: list[str] = []
    outcome: str = "pending"  # pending | succeeded | failed_verification | failed | cancelled
    # A background worker owns a short-lived lease.  Store writes from a
    # superseded worker are rejected after restart/cancel/retry.
    worker_lease: str = ""
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
    # Successful registry calls by real agent. Synthetic coordinator summaries
    # and zero-call deterministic transforms never count as AI participation.
    successful_agent_calls: dict[str, int] = {}
    # Unlike ``agent_calls`` (completed calls used by the budget), this audit
    # counter never rolls back when a call times out, errors, or asks for input.
    agent_call_attempts: int = 0
    # Actual elapsed time across every attempted call, including timeouts and
    # errors. This may exceed wall time because fan-out calls overlap.
    agent_attempt_duration_ms: int = 0
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
