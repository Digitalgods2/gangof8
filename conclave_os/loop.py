"""Deliberation Loop — the 10-step coordinator loop (DESIGN.md section 3).

The Coordinator is code, not an agent. Every loop is bounded by the session
budgets; exceeding any cap force-stops with a partial answer.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

from . import config, executor
from .classifier import classify
from .composer import compose, fallback_final, parse_final
from .executor import ExecutionError
from .governance import ApprovalRequired, BudgetExceeded, Governance
from .logstore import LogStore
from .models import (
    Contribution,
    CouncilMember,
    Disagreement,
    InputRequest,
    ProposedAction,
    RoundSpec,
    Session,
    SessionStatus,
    Role,
    risk_gt,
)
from .registry import AdapterResult, AgentError, AgentInputRequired, AgentRegistry
from .roles import build_council, plan_rounds
from .sessions import SessionManager
from .skills import get_skill

AgentCall = Callable[[CouncilMember, str], Contribution]


def _agent_call(
    session: Session, registry: AgentRegistry, store: LogStore,
    member: CouncilMember, prompt: str, timeout_s: int = 120, reserve: int = 0,
) -> Contribution:
    # `reserve` calls are held back for the composer; never reserve the
    # entire budget so tiny test budgets still allow one deliberation call.
    cap = session.budgets.max_agent_calls - max(0, min(reserve, session.budgets.max_agent_calls - 1))
    if session.agent_calls >= cap:
        raise BudgetExceeded(
            f"max_agent_calls={session.budgets.max_agent_calls} reached"
            + (f" (cap {cap} with {reserve} reserved for composition)" if reserve else "")
        )
    try:
        result = registry.call(member.agent, member.role, prompt, timeout_s)
    except AgentInputRequired as e:
        e.role = member.role  # enrich with call-site context for the InputRequest
        e.agent_name = member.agent
        raise
    session.agent_calls += 1
    contribution = Contribution(
        round=session.current_round,
        role=member.role,
        agent=member.agent,
        content=result.content,
        tokens=result.tokens,
        duration_ms=result.duration_ms,
    )
    session.contributions.append(contribution)
    store.log_event(
        session.session_id,
        "contribution",
        {"round": contribution.round, "role": member.role.value,
         "agent": member.agent, "chars": len(result.content)},
    )
    return contribution


def _recent_context(session: Session, limit: int = 3) -> str:
    parts = [
        f"[{c.role.value} r{c.round}] {c.content[:700]}"
        for c in session.contributions[-limit:]
    ]
    return "\n".join(parts) if parts else "(none yet)"


def _readable_files(session: Session, data_dir) -> list[str]:
    """Files already saved in this session's artifacts sandbox — the only
    things an agent may pull mid-deliberation via the read_file skill."""
    d = executor.artifacts_dir(data_dir, session.session_id)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


# Every role sees this so non-implementer roles stop treating can_write_files /
# can_run_commands as a blocker and stop asking the human to "enable" them: file
# production is governed (implementer emits ARTIFACT, human approves the write).
_GOVERNANCE_CONTEXT = (
    "You operate inside a governed coordinator. You cannot and do not need to "
    "perform file writes, shell commands, or network calls yourself — ignore any "
    "can_write_files / can_run_commands permission flags. When the task needs a "
    "file, the implementer emits an 'ARTIFACT: <filename>' block and the human "
    "approves the write. Do NOT ask whether to enable write permissions; assume "
    "the governed write path exists and proceed with your role.\n"
)


def build_prompt(
    session: Session, spec: RoundSpec, role: Role, readable: list[str] = ()
) -> str:
    cap = ""
    if readable and role in (Role.researcher, Role.implementer):
        cap = (
            "You may read a file already saved in this session by writing a line "
            "'SKILL: read_file <name>' (the file's contents are returned to you, no "
            f"approval needed). Available files: {', '.join(readable)}.\n"
        )
    return (
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"Round {spec.round} objective: {spec.goal}\n"
        f"Your role: {role.value}. Answer only from this role.\n"
        f"Output requirement: {spec.output_requirement}\n"
        f"{cap}"
        f"Context so far:\n{_recent_context(session)}"
    )


# An agent pulls a no-approval capability mid-round with a plain-text line
# 'SKILL: <name> <arg>' (bullets, bold, and :—–- separators tolerated — the
# same envelope-surviving style as ARTIFACT:/DISAGREEMENT:).
_SKILL_REQUEST_MARKER = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?SKILL(?:\*\*)?\s*[:—–-]\s*(?:\*\*)?\s*(\w+)\s+(.+?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _resolve_skill_requests(
    session: Session, member: CouncilMember, prompt: str, contribution: Contribution,
    call: AgentCall, governance: Governance, store: LogStore,
) -> Contribution:
    """If the agent requested no-approval skills (SKILL: <name> <arg>), run each
    through the permission kernel, execute the authorized ones, and re-call the
    agent ONCE with the results appended so it can use them. Approval-gated
    skills (write_file) are NOT honored here — those go through the ARTIFACT
    proposal path. Returns the (possibly re-called) contribution."""
    reqs = _SKILL_REQUEST_MARKER.findall(contribution.content)
    if not reqs:
        return contribution
    sid = session.session_id
    results: list[str] = []
    for raw_name, arg in reqs[: config.MAX_SKILL_REQUESTS_PER_TURN]:
        name, arg = raw_name.lower(), arg.strip()
        store.log_event(sid, "skill_requested",
                        {"skill": name, "role": member.role.value, "arg": arg})
        skill = get_skill(name)
        if skill is None:
            results.append(f"SKILL {name}: unknown skill")
            continue
        if skill.requires_approval:
            results.append(
                f"SKILL {name}: not available mid-deliberation (requires approval) — "
                "produce it as an ARTIFACT instead")
            continue
        action = ProposedAction(session_id=sid, kind=name, args={"filename": arg}, role=member.role)
        governance.authorize_action(session, action)  # no-approval skill → None; may deny on role
        session.proposed_actions.append(action)
        if action.status == "denied":
            results.append(f"SKILL {name}: denied — {action.error}")
            continue
        try:
            out = executor.execute(session, action, store.data_dir)
            action.status = "executed"
            session.tools_called.append(name)
            store.log_event(sid, "skill_resolved", {"skill": name, "arg": arg, "chars": len(out)})
            results.append(f"SKILL {name} '{arg}' result:\n{out[: config.SKILL_RESULT_MAX_CHARS]}")
        except ExecutionError as e:
            action.status = "failed"
            action.error = str(e)
            store.log_event(sid, "skill_failed", {"skill": name, "arg": arg, "error": str(e)})
            results.append(f"SKILL {name}: error — {e}")
    followup = (
        f"{prompt}\n\nSkill results (use these; do not request them again):\n"
        + "\n\n".join(results)
    )
    return call(member, followup)


def test_both_sides_prompt(session: Session, d: Disagreement) -> str:
    positions = "\n".join(f"- {p['role']}: {p['claim']}" for p in d.positions)
    return (
        f"Task: {session.task.text}\n"
        f"A disagreement was raised on: {d.topic}\n"
        f"Positions:\n{positions}\n"
        "Your role: critic. Test both sides against evidence, constraints, and "
        "the user's goal. Start your verdict with 'VERDICT: uphold' or "
        "'VERDICT: overturn'."
    )


def draft_prompt(session: Session) -> str:
    return (
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        "Your role: implementer. Produce the ACTUAL working result, not a description "
        "of it. If the task calls for files (code, docs, config), emit each file "
        "literally in this format, with its COMPLETE contents — never a summary of what "
        "the file would contain:\n"
        "ARTIFACT: <filename>\n"
        "<full file contents>\n"
        "ARTIFACT: <next filename>\n"
        "<full file contents>\n"
        "Use one ARTIFACT block per file and include EVERY file the task asks for. Do "
        "not describe the files in prose — write them out in full. They are saved for "
        "you only after human approval, so you need no filesystem access yourself.\n"
        "Example of the exact format:\n"
        "ARTIFACT: main.py\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        "def root():\n"
        "    return {'message': 'Hello, World!'}\n"
        "ARTIFACT: requirements.txt\n"
        "fastapi\n"
        "uvicorn\n"
        f"\nContext so far:\n{_recent_context(session, limit=5)}"
    )


def review_prompt(session: Session, draft: Contribution) -> str:
    return (
        f"Task: {session.task.text}\n"
        "Your role: critic. Review the draft below for flaws. If it is good "
        "enough, say 'acceptable'.\n"
        f"Draft:\n{draft.content[:1500]}"
    )


# Marker for an explicit conflict: optional bullet/numbering, then
# DISAGREEMENT:/DISAGREE: (any case, : — – or - as separator), then the body.
_DISAGREEMENT_MARKER = re.compile(
    r"^\s*(?:[-*•]|\d+[.)])?\s*disagree(?:ment)?\s*[:—–-]\s*(.+)$", re.IGNORECASE
)

_CLAIM_ROLES = (Role.researcher, Role.architect, Role.implementer)


def _claim_source(session: Session, spec: RoundSpec, challenger: Contribution) -> Optional[Contribution]:
    """The contribution whose claim is being challenged: prefer the most
    recent claim-making role, fall back to any other role."""
    candidates = [
        p for p in session.contributions
        if p.role != challenger.role and p.round <= spec.round
    ]
    for p in reversed(candidates):
        if p.role in _CLAIM_ROLES:
            return p
    return candidates[-1] if candidates else None


def detect_disagreements(session: Session, spec: RoundSpec) -> list[Disagreement]:
    """Scan this round's contributions for explicit disagreement markers
    (loop step 6). Supports bullets/numbering, any case, multiple markers per
    contribution, and multi-line claims (continuation lines up to a blank)."""
    found: list[Disagreement] = []
    seen = {d.topic for d in session.disagreements}
    for c in (x for x in session.contributions if x.round == spec.round):
        lines = c.content.splitlines()
        i = 0
        while i < len(lines):
            m = _DISAGREEMENT_MARKER.match(lines[i])
            i += 1
            if not m:
                continue
            parts = [m.group(1).strip()]
            while i < len(lines) and lines[i].strip() and not _DISAGREEMENT_MARKER.match(lines[i]):
                parts.append(lines[i].strip())
                i += 1
            body = " ".join(p for p in parts if p)
            topic = re.split(r"\s*[—–]\s*|\s+-\s+", body)[0].strip()[:80]
            if not topic or topic in seen:
                continue
            prior = _claim_source(session, spec, c)
            found.append(
                Disagreement(
                    topic=topic,
                    positions=[
                        {
                            "role": prior.role.value if prior else "coordinator",
                            "claim": (prior.content.splitlines()[0] if prior else session.task.text)[:200],
                        },
                        {"role": c.role.value, "claim": body[:300]},
                    ],
                )
            )
            seen.add(topic)
    return found


def coordinator_decide(d: Disagreement) -> tuple[str, str, str]:
    """Choose based on evidence > constraints > user goal; always record why."""
    test = (d.critic_test or "").lower()
    if "uphold" in test:
        return d.positions[0]["claim"], "evidence", d.critic_test or ""
    if "overturn" in test or "reject" in test:
        return d.positions[1]["claim"], "evidence", d.critic_test or ""
    return (
        d.positions[0]["claim"],
        "constraint",
        "no decisive critic verdict; kept the position consistent with task constraints",
    )


def _stop_check(
    session: Session, spec: RoundSpec, plan_len: int, verdict: Optional[str]
) -> tuple[bool, Optional[str]]:
    """Loop step 9 — stop when ANY condition is true."""
    if verdict == "accept":
        return True, "answer accepted"
    if spec.round + 1 >= min(plan_len, session.budgets.max_rounds):
        return True, "max rounds reached"
    if session.has_pending_approval:
        return True, "human approval needed"
    if session.blocked_on_missing_info:
        return True, "blocked on missing information"
    if session.risk_exceeds_boundary:
        return True, "risk exceeds allowed boundary"
    return False, None


def run_session(
    session: Session,
    manager: SessionManager,
    registry: AgentRegistry,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]] = None,
) -> Session:
    sid = session.session_id

    # 2. Classify
    cls = classify(session.task.text, role_agents)
    session.classification = cls
    if not session.budgets_locked:
        session.budgets = config.budgets_for(cls.complexity)
    store.log_event(sid, "classified", cls.model_dump())
    manager.transition(session, SessionStatus.classified)

    # 7 (pre-round gate): high-risk tasks pause for the human before anything runs
    if cls.human_approval_required:
        governance.request_approval(
            session,
            action=f"begin execution of task: {session.task.text[:120]}",
            category="external",
            risk=cls.risk,
        )
        session.risk_exceeds_boundary = risk_gt(cls.risk, config.RISK_BOUNDARY)
        session.stop_reason = "human approval needed"
        manager.transition(session, SessionStatus.awaiting_approval)
        return session

    return _deliberate(session, manager, registry, governance, store, role_agents)


def resume_session(
    session: Session,
    manager: SessionManager,
    registry: AgentRegistry,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]] = None,
) -> Session:
    """Continue a session paused in awaiting_approval once its gate is approved.
    Rounds already completed before the pause are not re-run (the plan is
    deterministic, so resumption skips the first len(session.rounds) entries)."""
    if session.status != SessionStatus.awaiting_approval:
        raise ValueError(f"cannot resume a session in status '{session.status.value}'")
    if session.has_pending_approval:
        raise ValueError("pending approvals remain; resolve them before resuming")
    if session.classification is None:
        raise ValueError("session has no classification; cannot resume")
    session.stop_reason = None
    session.risk_exceeds_boundary = False  # the human explicitly accepted the risk
    store.log_event(session.session_id, "session_resumed", {})
    return _deliberate(session, manager, registry, governance, store, role_agents)


def _deliberate(
    session: Session,
    manager: SessionManager,
    registry: AgentRegistry,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]] = None,
) -> Session:
    sid = session.session_id
    cls = session.classification

    # 3. Select agents (kept from the pre-pause run when resuming)
    if session.council.members:
        council = session.council
    else:
        council = build_council(cls, role_agents)
        session.council = council
        store.log_event(sid, "council_formed", council.model_dump())

    # 4. Round plan (declared before execution, hard-capped); on resume,
    # skip the rounds that already ran
    plan = plan_rounds(cls, council, session.budgets)
    completed_rounds = len(session.rounds)
    # If proposals were already collected, deliberation finished and we paused
    # in the action gate (step 7b). On resume, do NOT re-run the remaining
    # planned rounds (deliberation can early-stop on an accepted draft, leaving
    # rounds unrun) — fall straight through to execution/compose.
    if session.proposed_actions:
        completed_rounds = len(plan)
    manager.transition(session, SessionStatus.deliberating)

    start = time.monotonic()
    verdict: Optional[str] = None

    def call(member: CouncilMember, prompt: str) -> Contribution:
        return _agent_call(session, registry, store, member, prompt,
                           reserve=config.COMPOSER_RESERVED_CALLS)

    def compose_call(member: CouncilMember, prompt: str) -> Contribution:
        return _agent_call(session, registry, store, member, prompt)

    try:
        for spec in plan[completed_rounds:]:
            if time.monotonic() - start > session.budgets.max_wall_seconds:
                raise BudgetExceeded(f"max_wall_seconds={session.budgets.max_wall_seconds} reached")
            session.current_round = spec.round
            session.rounds.append(spec)
            store.log_event(sid, "round_start", spec.model_dump())

            # 5. Run agent round
            readable = _readable_files(session, store.data_dir)
            for role in spec.agents:
                member = council.get(role)
                if not (member and member.active):
                    continue
                for _ in range(spec.max_turns):
                    governance.check(session, "generate_text")
                    p = build_prompt(session, spec, role, readable)
                    c = call(member, p)
                    c = _resolve_skill_requests(session, member, p, c, call, governance, store)
                    if c.content.strip():  # output requirement met (Phase 0: non-empty)
                        break

            # 6. Conflict check: isolate → critic tests → coordinator rules → log
            critic = council.get(Role.critic)
            new_disagreements = detect_disagreements(session, spec)
            for i, d in enumerate(new_disagreements):
                if critic and critic.active and i < config.MAX_CRITIC_TESTS_PER_ROUND:
                    d.critic_test = call(critic, test_both_sides_prompt(session, d)).content
                elif i >= config.MAX_CRITIC_TESTS_PER_ROUND:
                    store.log_event(sid, "critic_test_skipped",
                                    {"round": spec.round, "topic": d.topic,
                                     "reason": f"per-round test cap ({config.MAX_CRITIC_TESTS_PER_ROUND})"})
                d.ruling, d.ruling_basis, d.rationale = coordinator_decide(d)
                session.disagreements.append(d)
                store.log_event(sid, "disagreement_ruled", d.model_dump())
            if not new_disagreements and any(
                x.role == Role.critic and x.round == spec.round
                and x.content.strip().upper().startswith("PASS")
                for x in session.contributions
            ):
                store.log_event(sid, "no_conflict", {"round": spec.round})

            # 7. Approval gate (Phase 0: agents cannot propose tool actions,
            # so this only trips if governance flagged something mid-round)
            if session.has_pending_approval:
                session.stop_reason = "human approval needed"
                manager.transition(session, SessionStatus.awaiting_approval)
                return session

            # 8. Produce working result: draft -> critique -> coordinator verdict
            implementer = council.get(Role.implementer)
            if implementer and implementer.active:
                draft = call(implementer, draft_prompt(session))
                if critic and critic.active:
                    review = call(critic, review_prompt(session, draft))
                    verdict = "accept" if "acceptable" in review.content.lower() else "revise"
                else:
                    verdict = "accept"

            # 9. Stop condition
            stop, reason = _stop_check(session, spec, len(plan), verdict)
            if stop:
                session.stop_reason = reason
                break

    except AgentInputRequired as e:
        return _pause_for_input(session, manager, store, e, purpose="deliberation")
    except BudgetExceeded as e:
        session.stop_reason = f"budget exhausted: {e}"
        session.unresolved.append(session.stop_reason)
        store.log_event(sid, "budget_exhausted", {"detail": str(e)})
    except AgentError as e:
        session.stop_reason = f"agent error: {e}"
        session.unresolved.append(session.stop_reason)
        store.log_event(sid, "agent_error", {"detail": str(e)})
    except ApprovalRequired as e:
        session.stop_reason = "human approval needed"
        store.log_event(sid, "paused_for_approval", e.approval.model_dump())
        manager.transition(session, SessionStatus.awaiting_approval)
        return session

    if session.stop_reason == "max rounds reached" and verdict == "revise":
        session.unresolved.append("round cap reached with an unaccepted draft")

    # 7b. Governed action execution: collect the implementer's artifact
    # proposals, gate every action on a human approval, execute approved ones.
    # If the task should produce files but the draft only described them,
    # materialize each file with a focused single-file call.
    _collect_proposals(session, store)
    if not session.proposed_actions and cls.produces_output:
        _materialize_artifacts(session, compose_call, store)
    if _execute_actions(session, manager, governance, store):
        return session  # paused in awaiting_approval

    # 10. Final response
    manager.transition(session, SessionStatus.composing)
    for d in session.disagreements:
        if not d.ruling:
            session.unresolved.append(f"unruled disagreement: {d.topic}")
    try:
        session.final = compose(session, council, compose_call)
    except AgentInputRequired as e:
        return _pause_for_input(session, manager, store, e, purpose="compose")
    manager.transition(session, SessionStatus.done)
    store.log_event(sid, "final_composed", session.final.model_dump())
    store.save_session(session)
    return session


# 'ARTIFACT: <filename>' heading the implementer's draft proposes saving the
# rest of the draft as that file. Plain-text contract — survives protocol
# envelopes (markdown bold tolerated, both styles).
_ARTIFACT_MARKER = re.compile(
    r"^\s*(?:\*\*)?ARTIFACT(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _collect_proposals(session: Session, store: LogStore) -> None:
    """Turn the implementer's final draft into one ProposedAction per
    'ARTIFACT: <filename>' block (loop step 7b). Each block's content runs from
    its marker to the next marker (or end of draft); leading framing prose
    before the first marker is dropped. Idempotent: proposals survive a pause
    and are not re-collected on resume."""
    if session.proposed_actions:
        return
    draft = next(
        (c for c in reversed(session.contributions) if c.role == Role.implementer), None
    )
    if draft is None:
        return
    markers = list(_ARTIFACT_MARKER.finditer(draft.content))
    if not markers:
        return
    for i, m in enumerate(markers):
        filename = m.group(1).strip()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(draft.content)
        content = _strip_code_fence(draft.content[m.end():end].strip())
        action = ProposedAction(
            session_id=session.session_id,
            kind="write_file",
            role=Role.implementer,
            filename=filename,
            content=content,
            args={"filename": filename, "content": content},
        )
        session.proposed_actions.append(action)
        store.log_event(
            session.session_id, "action_proposed",
            {"action_id": action.action_id, "kind": action.kind,
             "filename": action.filename, "chars": len(action.content)},
        )


# Filenames mentioned anywhere (main.py, requirements.txt) — used to recover the
# intended file set when the implementer described files instead of emitting them.
_FILENAME_RE = re.compile(
    r"\b([\w\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|md|txt|"
    r"json|ya?ml|toml|ini|cfg|csv|html|css|scss|sh|bat|ps1|sql))\b",
    re.IGNORECASE,
)


def materialize_prompt(session: Session, filename: str) -> str:
    return (
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"Output the COMPLETE contents of the file '{filename}' and NOTHING else: no "
        "explanation, no commentary, no markdown code fences — just the raw file body, "
        "ready to save verbatim, consistent with the agreed design below.\n"
        f"Agreed design / context:\n{_recent_context(session, limit=6)}"
    )


def _strip_code_fence(text: str) -> str:
    """Drop a single wrapping ``` / ```lang fence if the agent added one despite
    being asked for the raw body."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    lines = lines[1:]  # opening fence (``` or ```lang)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _intended_filenames(session: Session) -> list[str]:
    """The files this task means to produce: explicit ARTIFACT names first, then
    any filename-like tokens in the implementer's draft and the task text."""
    draft = next(
        (c for c in reversed(session.contributions) if c.role == Role.implementer), None
    )
    text = f"{draft.content if draft else ''}\n{session.task.text}"
    seen: set[str] = set()
    out: list[str] = []
    for m in list(_ARTIFACT_MARKER.finditer(text)) + list(_FILENAME_RE.finditer(text)):
        name = m.group(1).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out[: config.MAX_ARTIFACT_FILES]


def _materialize_artifacts(session: Session, call: AgentCall, store: LogStore) -> None:
    """Recover multi-file output that a draft only described: fetch each
    intended file with its own focused single-file call, one write_file
    ProposedAction per file. Idempotent (skips if proposals already exist);
    degrades gracefully on budget/agent errors."""
    if session.proposed_actions:
        return
    implementer = session.council.get(Role.implementer)
    if not (implementer and implementer.active):
        return
    filenames = _intended_filenames(session)
    if not filenames:
        return
    sid = session.session_id
    store.log_event(sid, "materialize_start", {"files": filenames})
    for fn in filenames:
        try:
            result = call(implementer, materialize_prompt(session, fn))
        except (BudgetExceeded, AgentError) as e:
            session.unresolved.append(f"could not materialize '{fn}': {e}")
            store.log_event(sid, "materialize_skipped", {"file": fn, "error": str(e)})
            continue
        except AgentInputRequired:
            session.unresolved.append(f"materialization of '{fn}' needed input; skipped")
            store.log_event(sid, "materialize_skipped", {"file": fn, "reason": "input_required"})
            continue
        content = _strip_code_fence(result.content)
        if not content:
            session.unresolved.append(f"materialized '{fn}' was empty; skipped")
            continue
        action = ProposedAction(
            session_id=sid, kind="write_file", role=Role.implementer,
            filename=fn, content=content, args={"filename": fn, "content": content},
        )
        session.proposed_actions.append(action)
        store.log_event(
            sid, "action_proposed",
            {"action_id": action.action_id, "kind": action.kind,
             "filename": fn, "chars": len(content)},
        )


def _execute_actions(
    session: Session, manager: SessionManager, governance: Governance, store: LogStore
) -> bool:
    """Drive every proposed action through its approval lifecycle; execute the
    approved ones. Returns True when the session must pause for the human.
    Deterministic and resume-safe — re-entered after each approval decision."""
    sid = session.session_id
    pending = False
    for action in session.proposed_actions:
        if action.status == "proposed":
            # Permission kernel: skill metadata drives the decision. It may
            # deny the action (unknown skill / role not allowed → status set
            # to 'denied' with an error), require approval (returns a pending
            # ApprovalRequest), or clear it to run straight through (None).
            approval = governance.authorize_action(session, action)
            if action.status == "denied":
                session.unresolved.append(
                    f"action '{action.kind}' denied: {action.error}"
                )
                continue
            if approval is not None:
                action.approval_id = approval.approval_id
                action.status = "awaiting_approval"
            else:
                action.status = "approved"
        if action.status == "awaiting_approval":
            approval = next(
                (a for a in session.approvals if a.approval_id == action.approval_id), None
            )
            if approval is not None and approval.status == "approved":
                action.status = "approved"
            elif approval is not None and approval.status == "denied":
                action.status = "denied"
                session.unresolved.append(
                    f"artifact '{action.filename}' not written: approval denied"
                )
                store.log_event(sid, "action_denied", {"action_id": action.action_id})
                continue
            else:
                pending = True
                continue
        if action.status == "approved":
            try:
                path = executor.execute(session, action, store.data_dir)
                action.status = "executed"
                action.result_path = str(path)
                session.files_changed.append(str(path))
                session.tools_called.append(action.kind)
                store.log_event(
                    sid, "action_executed",
                    {"action_id": action.action_id, "path": str(path)},
                )
            except ExecutionError as e:
                action.status = "failed"
                action.error = str(e)
                session.unresolved.append(f"artifact '{action.filename}' failed: {e}")
                store.log_event(
                    sid, "action_failed",
                    {"action_id": action.action_id, "error": str(e)},
                )
    if pending:
        session.stop_reason = "human approval needed"
        manager.transition(session, SessionStatus.awaiting_approval)
    return pending


def _pause_for_input(
    session: Session, manager: SessionManager, store: LogStore,
    exc: AgentInputRequired, purpose: str,
) -> Session:
    """An agent asked the human a question mid-call: record it and pause."""
    req = InputRequest(
        session_id=session.session_id,
        agent=exc.agent_name or "unknown",
        role=exc.role or Role.coordinator,
        round=session.current_round,
        purpose=purpose,
        question=exc.question,
        resume_token=exc.resume_token,
    )
    session.input_requests.append(req)
    session.stop_reason = "agent needs user input"
    store.log_event(session.session_id, "input_requested", req.model_dump())
    manager.transition(session, SessionStatus.awaiting_input)
    return session


def resume_with_input(
    session: Session,
    manager: SessionManager,
    registry: AgentRegistry,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]],
    req: InputRequest,
    result: AdapterResult,
) -> Session:
    """Continue a session after the human answered an agent's question.
    `result` is the completed output of the previously paused call."""
    sid = session.session_id
    session.agent_calls += 1  # the resumed call was a real agent call
    # The human's answer joins the session context as a coordinator
    # contribution — later agent calls are fresh backend tasks and would
    # otherwise never see it (they'd re-ask the same question).
    session.contributions.append(
        Contribution(
            round=req.round, role=Role.coordinator, agent="user",
            content=f"User was asked: {req.question}\nUser answered: {req.answer}",
        )
    )
    contribution = Contribution(
        round=req.round, role=req.role, agent=req.agent,
        content=result.content, tokens=result.tokens, duration_ms=result.duration_ms,
    )
    session.contributions.append(contribution)
    store.log_event(
        sid, "contribution",
        {"round": req.round, "role": req.role.value, "agent": req.agent,
         "chars": len(result.content), "resumed_after_input": True},
    )

    if req.purpose == "compose":
        manager.transition(session, SessionStatus.composing)
        session.final = parse_final(session, result.content) or fallback_final(
            session, "summarizer answer after user input was unparseable"
        )
        manager.transition(session, SessionStatus.done)
        store.log_event(sid, "final_composed", session.final.model_dump())
        store.save_session(session)
        return session

    # Deliberation pause: run the conflict check the paused round never got,
    # then let _deliberate continue with the remaining rounds and compose.
    # (Step 8 of the paused round is skipped — a known, logged simplification.)
    session.current_round = req.round
    council = session.council
    critic = council.get(Role.critic)

    def call(member: CouncilMember, prompt: str) -> Contribution:
        return _agent_call(session, registry, store, member, prompt,
                           reserve=config.COMPOSER_RESERVED_CALLS)

    spec = next((r for r in session.rounds if r.round == req.round), None)
    if spec is not None:
        try:
            for i, d in enumerate(detect_disagreements(session, spec)):
                if critic and critic.active and i < config.MAX_CRITIC_TESTS_PER_ROUND:
                    d.critic_test = call(critic, test_both_sides_prompt(session, d)).content
                d.ruling, d.ruling_basis, d.rationale = coordinator_decide(d)
                session.disagreements.append(d)
                store.log_event(sid, "disagreement_ruled", d.model_dump())
        except AgentInputRequired as e:
            return _pause_for_input(session, manager, store, e, purpose="deliberation")
        except (BudgetExceeded, AgentError) as e:
            session.unresolved.append(f"conflict check skipped after input: {e}")

    return _deliberate(session, manager, registry, governance, store, role_agents)
