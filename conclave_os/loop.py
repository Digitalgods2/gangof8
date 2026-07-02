"""Deliberation Loop — the 10-step coordinator loop (DESIGN.md section 3).

The Coordinator is code, not an agent. Every loop is bounded by the session
budgets; exceeding any cap force-stops with a partial answer.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from . import config, executor, rounds, skills
from .classifier import classify
from .composer import compose, fallback_final, parse_final
from .executor import ExecutionError
from .governance import ApprovalRequired, BudgetExceeded, Governance
from .logstore import LogStore
from .models import (
    Contribution,
    Council,
    CouncilMember,
    FinalAnswer,
    InputRequest,
    ProposedAction,
    RoundSpec,
    Session,
    SessionStatus,
    Role,
    TaskType,
)
from .registry import AdapterResult, AgentError, AgentInputRequired, AgentRegistry
from .roles import build_council
from .rounds import (
    _GOVERNANCE_CONTEXT,
    _output_contract,
    _recent_context,
    _skill_hints,
    delegation_contract,
    lead_prompt,
    role_instruction,
)
from .sessions import SessionManager
from .skills import get_skill
from . import cancellation
from .cancellation import SessionCancelled  # re-exported for callers (service)
from .uploads import image_inputs


# Actionable draft proposals (writes/exec/stage/promote) — distinct from the
# read-skill actions (read_file/search_project/list_dir) that also land in
# proposed_actions for audit. The "did we collect / are we past deliberation"
# guards must count only these, not read requests. (Most now execute freely;
# only `promote` pauses for approval — but all are "the council produced work".)
_PROPOSAL_KINDS = {"write_file", "edit_file", "run_tests", "stage", "promote"}


def _has_proposals(session: Session) -> bool:
    return any(a.kind in _PROPOSAL_KINDS for a in session.proposed_actions)

AgentCall = Callable[[CouncilMember, str], Contribution]

# Guards mutable session state (budget counter, contributions, unresolved,
# council roster, log writes) so parallel sibling consults can't race. Held only
# for tiny bookkeeping critical sections — NEVER across an agent call.
_SESSION_LOCK = threading.Lock()
# Bounds concurrent agent subprocesses machine-wide (see config.MAX_PARALLEL_AGENTS).
_AGENT_SEMAPHORE = threading.Semaphore(config.MAX_PARALLEL_AGENTS)


def _agent_call(
    session: Session, registry: AgentRegistry, store: LogStore,
    member: CouncilMember, prompt: str, timeout_s: Optional[int] = None, reserve: int = 0,
    images: Optional[list[dict]] = None,
) -> Contribution:
    # Cooperative cancellation: every agent call passes through here, so this is
    # the one checkpoint that aborts a run the human cancelled mid-flight.
    if cancellation.is_requested(session.session_id):
        raise SessionCancelled()
    # `reserve` calls are held back for the composer; never reserve the
    # entire budget so tiny test budgets still allow one deliberation call.
    cap = session.budgets.max_agent_calls - max(0, min(reserve, session.budgets.max_agent_calls - 1))
    # Reserve a budget slot UP FRONT (under lock) so concurrent fan-out calls can't
    # slip past a check-then-increment gap and oversubscribe max_agent_calls. The
    # slot is rolled back if the call fails or pauses, preserving the sequential
    # semantics ("only a completed call counts").
    with _SESSION_LOCK:
        if session.agent_calls >= cap:
            raise BudgetExceeded(
                f"max_agent_calls={session.budgets.max_agent_calls} reached"
                + (f" (cap {cap} with {reserve} reserved for composition)" if reserve else "")
            )
        session.agent_calls += 1
    # Per-agent timeout: the gemini CLI needs more headroom than claude/codex.
    if timeout_s is None:
        timeout_s = config.agent_timeout(member.agent)
    # Tag this worker thread with the session so the CLI adapter can register its
    # subprocess for hard cancellation (kill on request). current_session is
    # thread-local, so each parallel worker tags itself independently.
    cancellation.set_current_session(session.session_id)
    try:
        # The semaphore bounds how many CLI subprocesses run at once (never held
        # across the budget lock, so bookkeeping never blocks on a slow call).
        with _AGENT_SEMAPHORE:
            result = registry.call(member.agent, member.role, prompt, timeout_s, images=images)
    except AgentInputRequired as e:
        e.role = member.role  # enrich with call-site context for the InputRequest
        e.agent_name = member.agent
        with _SESSION_LOCK:
            session.agent_calls -= 1  # paused, not completed — resume re-counts it
        raise
    except Exception:
        with _SESSION_LOCK:
            session.agent_calls -= 1  # failed — release the reserved slot
        raise
    finally:
        cancellation.set_current_session(None)
    contribution = Contribution(
        round=session.current_round,
        role=member.role,
        agent=member.agent,
        content=result.content,
        tokens=result.tokens,
        duration_ms=result.duration_ms,
    )
    with _SESSION_LOCK:
        session.contributions.append(contribution)
        store.log_event(
            session.session_id,
            "contribution",
            {"round": contribution.round, "role": member.role.value,
             "agent": member.agent, "chars": len(result.content)},
        )
    return contribution


def _readable_files(session: Session, data_dir) -> list[str]:
    """Files already saved in this session's artifacts sandbox — the only
    things an agent may pull mid-deliberation via the read_file skill."""
    d = executor.artifacts_dir(data_dir, session.session_id)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


# README / manifests give the app's self-description; source files give how it
# ACTUALLY works. The council needs BOTH or it produces README-grade advice.
_OVERVIEW_DOC_FILES = (
    "README.md", "README", "readme.md", "README.txt", "package.json", "go.mod",
    "pyproject.toml", "requirements.txt", "Cargo.toml", "wails.json", "CLAUDE.md",
)
# Entry points worth always reading if present (the app's "spine").
_OVERVIEW_ENTRY_FILES = (
    "main.go", "app.go", "main.py", "app.py", "cmd/main.go", "src/main.py",
    "src/index.ts", "src/index.js", "src/App.svelte", "src/App.tsx", "index.js",
)
_OVERVIEW_CODE_EXTS = {
    ".go", ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".java", ".rb", ".php",
    ".c", ".cpp", ".cs", ".svelte", ".vue", ".kt", ".swift",
}
_OVERVIEW_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode", ".next",
    "target", "vendor", "third_party", "testdata", "tests", "test", "__tests__",
}
# Files that define a CONTRACT/interface — usually small, so the largest-file
# heuristic misses them (missing registry.py is why the council "recommended" a
# Protocol that already exists). Always pulled in.
_OVERVIEW_CONTRACT_HINTS = (
    "registry", "protocol", "interface", "factory", "abc", "schema",
)
# Other architecturally CENTRAL files worth surfacing.
_OVERVIEW_CORE_HINTS = (
    "config", "settings", "models", "types", "router", "service", "app", "main",
    "core", "engine", "client", "api", "store", "database",
)


def _read_established(session: Session, data_dir, name: str) -> str:
    try:
        return skills.HANDLERS["read_file"](
            session,
            ProposedAction(session_id=session.session_id, kind="read_file",
                           role=Role.researcher, args={"filename": name, "target": "established"}),
            Path(data_dir))
    except Exception:  # noqa: BLE001 — absent/unreadable: skip
        return ""


def _established_overview(session: Session, data_dir) -> str:
    """Proactively read the established folder so EVERY agent starts with concrete
    content — NOT just the README/manifests (which yield generic advice) but the
    ACTUAL SOURCE: entry points plus the largest code files (the core logic). The
    council shouldn't depend on a flaky agent remembering to request a SKILL just
    to see the code it was asked to examine. Bounded; read-only."""
    if not session.established_root:
        return ""
    root = Path(session.established_root)
    if not root.is_dir():
        return ""
    parts: list[str] = []

    # 1. directory tree
    try:
        listing = skills.HANDLERS["list_dir"](
            session,
            ProposedAction(session_id=session.session_id, kind="list_dir",
                           role=Role.researcher, args={"path": ".", "target": "established"}),
            Path(data_dir))
        if listing:
            parts.append("Directory tree:\n" + listing[:2500])
    except Exception:  # noqa: BLE001
        pass

    # 2. README + manifests (the app's self-description) — trimmed
    docs = 0
    for name in _OVERVIEW_DOC_FILES:
        if docs >= 3:
            break
        body = _read_established(session, data_dir, name)
        if body and body.strip():
            parts.append(f"--- {name} ---\n{body[:1100]}")
            docs += 1

    # 3. ACTUAL SOURCE — entry points + architecturally CENTRAL files (registry/
    # protocol/config/models/…) + the largest code files, excluding tests/vendored.
    # Central files are often small (an interface/Protocol) so the largest-file
    # heuristic alone misses them — which made the council "recommend" things that
    # already exist. Shows HOW the app works.
    picked: list[Path] = []
    for name in _OVERVIEW_ENTRY_FILES:
        p = (root / name)
        if p.is_file():
            picked.append(p)
    candidates: list[tuple[int, Path]] = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part in _OVERVIEW_SKIP_DIRS for part in rel.parts):
            continue
        if not p.is_file() or p.suffix.lower() not in _OVERVIEW_CODE_EXTS:
            continue
        low = p.name.lower()
        if "test" in low or ".min." in low or low.endswith(".d.ts"):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > 250_000:  # skip generated/bundled
            continue
        candidates.append((size, p))
    cap = 8
    # First, CONTRACT/interface files (registry/protocol/factory/…) — usually
    # small, so the largest-file heuristic misses them; missing them is exactly
    # why the council "recommended" a Protocol that already exists. Then a couple
    # of central-by-name files, then FILL THE REST with the largest (core logic).
    by_size = sorted(candidates, key=lambda t: t[0], reverse=True)
    contracts = [p for _, p in by_size
                 if any(h in p.stem.lower() for h in _OVERVIEW_CONTRACT_HINTS)]
    central = [p for _, p in by_size
               if any(h in p.stem.lower() for h in _OVERVIEW_CORE_HINTS)]
    for p in contracts[:2] + central[:2]:
        if p not in picked:
            picked.append(p)
    for _, p in by_size:
        if len(picked) >= cap:
            break
        if p not in picked:
            picked.append(p)
    for p in picked[:cap]:
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if body.strip():
            parts.append(f"--- {p.relative_to(root).as_posix()} (source, head) ---\n{body[:1500]}")

    if not parts:
        return ""
    directive = (
        "\n\nHOW TO RECOMMEND:\n"
        "1. FIRST infer WHAT THIS APP IS and its constraints from its README/docs "
        "and code — e.g. a LOCAL single-user tool vs a production multi-user "
        "service. Recommendations MUST fit that. Do NOT propose production/scale/"
        "multi-user hardening (auth/JWT, rate-limiting, async ORM + connection "
        "pooling, Docker, OpenTelemetry/Prometheus, field encryption, 'concurrency "
        "under load') for a LOCAL single-user app — that is wrong-altitude advice.\n"
        "2. Do NOT recommend adding something that ALREADY EXISTS in the code shown "
        "(e.g. an adapter Protocol, a backend, tests) — check first; if it exists, "
        "suggest improving it, not introducing it.\n"
        "3. GROUND each recommendation in specific code — name the file/function/"
        "feature. Avoid generic best-practice filler ('add docs/tests/observability') "
        "unless the code visibly lacks it and you point to where.\n"
        "4. Prefer concrete, app-specific improvements a developer could start today."
    )
    return ("ESTABLISHED FOLDER (real content the coordinator read for you from "
            f"{session.established_root} — analyze THIS, not assumptions):\n"
            + "\n\n".join(parts))[:14000] + directive


def _conversation_overview(session: Session) -> str:
    """For a CONTINUED conversation: the prior turns plus the human's latest
    response, so the council builds on the discussion and engages with what the
    human said instead of re-stating its earlier conclusion. Empty on turn one."""
    turns = session.turns or []
    if not turns:
        return ""
    prior = turns[:-1]
    latest = turns[-1]
    parts: list[str] = []
    if prior:
        hist = "\n\n".join(
            f"{'YOU (human)' if t.get('role') == 'user' else 'COUNCIL'}: {str(t.get('text',''))[:1400]}"
            for t in prior)
        parts.append("CONVERSATION SO FAR:\n" + hist)
    if latest.get("role") == "user":
        parts.append(
            "THE HUMAN'S LATEST RESPONSE — address THIS directly. Engage with their "
            "point (agree, push back with evidence, or refine); do NOT just repeat your "
            f"previous conclusion:\n{latest.get('text','')}")
    return "\n\n".join(parts)


def _web_overview(session: Session) -> str:
    """Proactively look up current info ONCE up front for fact-needing tasks with
    no local source — so the council has REAL web data even if the researcher
    seat fails or no agent thinks to call web_search. (Having internet access
    available is not the same as using it; this uses it.) Best-effort; bounded."""
    cls = session.classification
    if not config.WEB_ENABLED or session.established_root:
        return ""
    if not (cls and cls.needs_facts):
        return ""
    # A from-scratch build (code/greenfield) doesn't need up-front web research —
    # skip the slow web call; the lead can still request web_search if it needs it.
    if cls.task_type == TaskType.code:
        return ""
    try:
        from . import web
        result = web.web_search(session.task.text)
    except Exception:  # noqa: BLE001 — web is best-effort context
        return ""
    if not result or not result.strip():
        return ""
    return ("WEB RESEARCH (current information the coordinator looked up on the live "
            "web for this task — trust and use it; you may request more with "
            "'SKILL: web_search <query>' / 'SKILL: web_fetch <url>'):\n" + result)


# An agent pulls a no-approval capability mid-round with a plain-text line
# 'SKILL: <name> <arg>' (bullets, bold, and :—–- separators tolerated — the
# same envelope-surviving style as ARTIFACT:/DISAGREEMENT:).
_SKILL_REQUEST_MARKER = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?SKILL(?:\*\*)?\s*[:—–-]\s*(?:\*\*)?\s*(\w+)\s+(.+?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# The lead pulls in a talent with a plain-text line 'CONSULT: <talent> - <q>' or
# 'DELEGATE: <talent> - <subtask>' (bullets/bold tolerated, : - or — separators).
_DELEGATION_MARKER = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?(?P<kind>CONSULT|DELEGATE)(?:\*\*)?\s*[:—–-]\s*"
    r"(?:\*\*)?(?P<talent>[a-z_]+)(?:\*\*)?\s*[—–:-]\s*"
    r"(?P<reason>.+?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_role(raw: str) -> Optional[Role]:
    key = (raw or "").strip().lower().replace("-", "_")
    for role in Role:
        if role.value == key:
            return role
    return None


def _delegation_decision(role: Optional[Role]) -> tuple[bool, str]:
    """A talent is grantable iff it's one of the advertised specialist roles."""
    if role is None:
        return False, "unknown talent"
    if role not in config.TALENTS:
        return False, f"'{role.value}' is not an available talent"
    return True, "granted"


def delegate_prompt(session: Session, role: Role, kind: str, reason: str,
                    *, by: str = "lead", may_subconsult: bool = False) -> str:
    lines = [
        f"Task: {session.task.text}",
        f"The {by} has asked you ({role.value}) to {kind} on a focused point.",
        f"Request: {reason}",
        role_instruction(role),
        "Answer ONLY that request — concise, concrete, task-relevant. Do not produce "
        "final deliverables or restate the whole task.",
    ]
    if may_subconsult:
        others = ", ".join(r.value for r in config.TALENTS if r != role)
        lines.append(
            "If — and ONLY if — exactly one other specialist would materially sharpen "
            "your answer, you MAY pull in ONE with a single line "
            "'CONSULT: <talent> - <specific question>' (do not convene a panel; "
            f"usually just answer). Talents: {others}.")
    lines.append(rounds.RESULT_CONTRACT)
    lines.append(f"Context so far:\n{_recent_context(session, limit=5)}")
    return "\n".join(lines)


def _resolve_one_delegation(
    session: Session, council: Council, requester: CouncilMember, m: "re.Match",
    call: AgentCall, store: LogStore, depth: int, can_subconsult: bool,
) -> str:
    """Resolve ONE CONSULT:/DELEGATE: grant and return its folded result string.
    Runs on a worker thread when siblings fan out, so every mutation of shared
    session state is done under _SESSION_LOCK; the agent call and any deeper
    recursion happen OUTSIDE the lock so real work overlaps."""
    sid = session.session_id
    kind = m.group("kind").lower()
    role = _parse_role(m.group("talent"))
    reason = " ".join(m.group("reason").strip().split())
    with _SESSION_LOCK:
        store.log_event(sid, "delegation_requested",
                        {"kind": kind, "to": role.value if role else m.group("talent"),
                         "reason": reason, "depth": depth, "by": requester.role.value})
    ok, why = _delegation_decision(role)
    if ok and role == requester.role:
        ok, why = False, "a seat cannot consult itself"  # no trivial self-loop
    if not ok or role is None:
        with _SESSION_LOCK:
            store.log_event(sid, "delegation_denied",
                            {"to": m.group("talent"), "reason": reason,
                             "decision": why, "depth": depth})
        return f"{kind.upper()} {m.group('talent')}: unavailable - {why}"
    with _SESSION_LOCK:
        helper = council.get(role)
        if helper is None:
            helper = CouncilMember(role=role, agent=(config.ROLE_AGENTS.get(role) or requester.agent))
            council.members.append(helper)
        helper.active = True
        store.log_event(sid, "delegation_granted",
                        {"to": role.value, "agent": helper.agent, "kind": kind,
                         "reason": reason, "depth": depth})
    try:
        answer = call(helper, delegate_prompt(
            session, role, kind, reason,
            by=requester.role.value, may_subconsult=can_subconsult))
        # Fold the reply back with the CONCLUSION intact: the RESULT: block is
        # kept whole and the preamble absorbs the truncation. A reply without
        # the block falls back to plain head-truncation.
        cap = config.DELEGATION_RESULT_MAX_CHARS
        preamble, result_block = rounds.split_result_block(answer.content)
        if result_block:
            result_block = result_block[:cap]  # a runaway block is still bounded
            head = preamble.strip()[: max(0, cap - len(result_block))].rstrip()
            piece = f"{head}\n\n{result_block}" if head else result_block
        else:
            piece = answer.content[:cap]
        # The sub-agent tier: let the consulted specialist itself consult one level
        # deeper. Its (already-capped) sub-results are folded in below the
        # specialist's own answer, so the requester sees the whole chain.
        if can_subconsult:
            sub = _run_delegations(
                session, council, helper, answer.content, call, store, depth + 1)
            if sub:
                piece += "\n\nSub-consultations this seat pulled in:\n" + "\n\n".join(sub)
        with _SESSION_LOCK:
            store.log_event(sid, "delegation_resolved",
                            {"to": role.value, "chars": len(piece), "depth": depth})
        return f"{kind.upper()} {role.value}@{helper.agent}:\n{piece}"
    except (AgentError, BudgetExceeded) as e:
        with _SESSION_LOCK:
            session.unresolved.append(f"delegation to {role.value} failed: {e}")
            store.log_event(sid, "delegation_failed",
                            {"to": role.value, "error": str(e), "depth": depth})
        return f"{kind.upper()} {role.value}: failed - {e}"


def _run_delegations(
    session: Session, council: Council, requester: CouncilMember, content: str,
    call: AgentCall, store: LogStore, depth: int,
) -> list[str]:
    """Resolve the CONSULT:/DELEGATE: lines in `content` (authored by `requester`),
    returning one folded result string per grant, in request order.

    Independent siblings (a seat emitting several CONSULT: lines at once) run
    CONCURRENTLY — each is a blocking CLI call, so this is the real wall-clock win.
    A single consult (the common case) skips the pool. Every consulted specialist's
    OWN answer is re-scanned one level deeper (up to budgets.max_delegation_depth,
    scaled by task complexity) — the primary lead → specialist → sub-agent
    hierarchy. Concurrency is bounded by _AGENT_SEMAPHORE (subprocess count) and
    the session agent-call budget; fan-out by budgets.max_delegations per scan.
    Per-level pools keep parents from waiting on children in the same pool, so
    nested fan-out can't deadlock."""
    reqs = list(_DELEGATION_MARKER.finditer(content))
    if not reqs:
        return []
    can_subconsult = depth < session.budgets.max_delegation_depth
    batch = reqs[: session.budgets.max_delegations]

    def resolve(m: "re.Match") -> str:
        return _resolve_one_delegation(
            session, council, requester, m, call, store, depth, can_subconsult)

    if len(batch) == 1:
        return [resolve(batch[0])]
    workers = min(len(batch), config.MAX_PARALLEL_AGENTS)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="consult") as ex:
        futures = [ex.submit(resolve, m) for m in batch]
        return [f.result() for f in futures]  # order preserved → stable folded output


def _resolve_delegations(
    session: Session, council: Council, lead: CouncilMember, prompt: str,
    contribution: Contribution, call: AgentCall, store: LogStore,
) -> Contribution:
    """Handle the lead's CONSULT:/DELEGATE: lines (level 1), letting each consulted
    specialist itself consult ONE bounded level deeper (the sub-agent tier — see
    _run_delegations), then re-call the lead ONCE with the folded results. Bounded
    by budgets.max_delegations per scan, budgets.max_delegation_depth levels, and
    the agent-call budget."""
    results = _run_delegations(session, council, lead, contribution.content,
                               call, store, depth=1)
    if not results:
        return contribution
    followup = (
        f"{prompt}\n\nResults from the talents you pulled in (use these; finish the "
        "task now — do not request the same help again):\n" + "\n\n".join(results)
    )
    return call(lead, followup)


def _is_analysis_task(session: Session) -> bool:
    cls = session.classification
    return bool(cls and cls.task_type in (TaskType.research, TaskType.question, TaskType.design))


def _skill_request_cap(session: Session) -> int:
    """How many SKILL: requests one turn may resolve. Analysis tasks get more —
    reading the material is the job; output tasks keep the tight bound."""
    return config.MAX_SKILL_REQUESTS_ANALYSIS if _is_analysis_task(session) \
        else config.MAX_SKILL_REQUESTS_PER_TURN


def _skill_result_cap(session: Session) -> int:
    """How much of each skill result is fed back. Analysis tasks get deeper
    reads — a 2000-char window on a large source file is a truncation the
    agent can't see, and it reasons wrongly from the fragment."""
    return config.SKILL_RESULT_ANALYSIS_MAX_CHARS if _is_analysis_task(session) \
        else config.SKILL_RESULT_MAX_CHARS


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
    for raw_name, arg in reqs[: _skill_request_cap(session)]:
        name, arg = raw_name.lower(), arg.strip()
        store.log_event(sid, "skill_requested",
                        {"skill": name, "role": member.role.value, "arg": arg})
        skill = get_skill(name)
        if skill is None:
            results.append(f"SKILL {name}: unknown skill")
            continue
        # Mid-deliberation SKILL: requests are for DISCOVERY only — reads and web
        # lookups. Writes/edits/tests/stage/promote carry structured content and
        # go through the draft's ARTIFACT/EDIT/RUNTESTS/PROMOTE contracts.
        if skill.category not in ("read", "web"):
            results.append(
                f"SKILL {name}: not available mid-deliberation (it changes state) — "
                "produce it as an ARTIFACT/EDIT/PROMOTE block in your draft instead")
            continue
        # map the single positional arg to the skill's first declared input
        # (read_file→filename, search_project→query)
        arg_key = skill.inputs[0] if skill.inputs else "filename"
        action = ProposedAction(session_id=sid, kind=name, args={arg_key: arg}, role=member.role)
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
            results.append(f"SKILL {name} '{arg}' result:\n{out[: _skill_result_cap(session)]}")
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


def _synthesis_final(session: Session) -> Optional[FinalAnswer]:
    """The lead's final synthesis as the FinalAnswer, when it can stand as one:
    substantial, not a stub, and declared DONE (a CONTINUE synthesis is
    explicitly unfinished — e.g. the human answered 'no' at the consent gate —
    so the summarizer aggregates instead). Confidence is high only when panel
    seats actually contributed to what the lead weighed."""
    synth = next((c for c in reversed(session.contributions) if c.role == Role.lead), None)
    if synth is None:
        return None
    decision, _ = rounds.parse_round_decision(synth.content)
    if decision != "DONE":
        return None
    text = rounds.strip_round_marker(synth.content)
    if len(text) < config.SYNTHESIS_FINAL_MIN_CHARS or rounds.reply_is_stub(text):
        return None
    had_panel = any(c.role == Role.panelist for c in session.contributions)
    return FinalAnswer(
        answer=text,
        confidence="high" if had_panel else "medium",
        assumptions=[],
        risks_unresolved=list(session.unresolved),
    )


def _panel_one(
    session: Session, member: CouncilMember, prompt: str,
    call: AgentCall, store: LogStore,
) -> Optional[Contribution]:
    """One panel seat's contribution, fan-out-safe: a failing seat is dropped
    for the round (logged, noted), never fatal. A panel seat asking the human a
    question is also treated as a drop — pausing mid-fan-out with sibling
    threads in flight is not sound; only the lead's calls may pause the run."""
    dropped_contribution = None
    try:
        c = call(member, prompt)
        # A stub take (tool-call debris / announced-but-not-done work) would
        # only pollute the synthesis and later context windows — drop the seat
        # for this round AND remove its debris from the transcript (the
        # panel_seat_dropped event + unresolved note keep the audit trail). No
        # retry: panel seats are best-effort voices, and the lead + composer
        # still have every healthy take.
        if rounds.reply_is_stub(c.content):
            reason = "stub reply (announced or attempted the work instead of doing it)"
            dropped_contribution = c
        else:
            return c
    except AgentInputRequired:
        reason = "asked for user input"
    except (AgentError, BudgetExceeded) as e:
        reason = str(e)
    with _SESSION_LOCK:
        if dropped_contribution is not None and dropped_contribution in session.contributions:
            session.contributions.remove(dropped_contribution)
        session.unresolved.append(f"panel seat '{member.agent}' dropped this round: {reason}")
        store.log_event(session.session_id, "panel_seat_dropped",
                        {"agent": member.agent, "round": session.current_round,
                         "error": reason[:300]})
    return None


def _pause_for_consent(session: Session, manager: SessionManager, store: LogStore) -> None:
    """The automatic rotation's one checkpoint: after a block of rounds without
    DONE, ask the human whether the council should keep going."""
    n = len(session.rounds)
    block = config.ROUNDS_PER_CONSENT
    req = InputRequest(
        session_id=session.session_id, agent="system", role=Role.coordinator,
        round=session.current_round, purpose="continue_rounds", resume_token="",
        question=(
            f"The council has deliberated {n} rounds without declaring the task done.\n"
            f"Round summaries:\n{rounds.round_summaries(session)}\n"
            f"Continue for another {block} rounds? Reply 'yes', a number of extra "
            "rounds, or 'no' to compose the final answer from the work so far."
        ),
    )
    session.input_requests.append(req)
    session.stop_reason = "waiting for go-ahead on more rounds"
    store.log_event(session.session_id, "input_requested", req.model_dump())
    manager.transition(session, SessionStatus.awaiting_input)


def _run_panel_rounds(
    session: Session,
    manager: SessionManager,
    council: Council,
    lead: CouncilMember,
    call: AgentCall,
    lead_call: AgentCall,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]],
    established_overview: str,
    start: float,
) -> bool:
    """Drive panel rounds until the lead declares DONE, the human declines more
    rounds, or budget/wall-time headroom runs out. Returns True when the session
    paused for round consent (the caller returns immediately). Round count and
    all carried context derive from persisted state (session.rounds /
    contributions), so a resumed session continues exactly where it paused."""
    sid = session.session_id
    panel = [m for m in council.members if m.role == Role.panelist and m.active]
    while not session.compose_now:
        r = len(session.rounds)
        if r > 0 and r >= config.ROUNDS_PER_CONSENT + session.consent_extra_rounds:
            _pause_for_consent(session, manager, store)
            return True
        remaining = (session.budgets.max_agent_calls - session.agent_calls
                     - config.COMPOSER_RESERVED_CALLS)
        if r > 0 and remaining < 2:
            session.unresolved.append("rounds stopped: agent-call budget headroom exhausted")
            break
        if r > 0 and time.monotonic() - start > session.budgets.max_wall_seconds:
            session.unresolved.append("rounds stopped: wall-time budget reached")
            break
        session.current_round = r
        spec = RoundSpec(
            round=r,
            goal=f"panel round {r + 1}: every seat contributes; lead synthesizes",
            agents=[Role.panelist, Role.lead] if panel else [Role.lead],
            stop_condition="lead declares ROUND: DONE",
            output_requirement="synthesis (and ARTIFACT/PROMOTE files when ready)",
        )
        session.rounds.append(spec)
        store.log_event(sid, "round_start", spec.model_dump())
        readable = _readable_files(session, store.data_dir)
        # the big up-front context is only worth its tokens once — round 1
        ov = established_overview if r == 0 else ""

        # (a) FAN-OUT — every panel seat answers in parallel (bounded by the
        # machine-wide semaphore inside _agent_call); a failing seat is dropped.
        results: list[Contribution] = []
        if panel:
            with ThreadPoolExecutor(
                max_workers=min(len(panel), config.MAX_PARALLEL_AGENTS),
                thread_name_prefix="panel",
            ) as ex:
                futures = [
                    ex.submit(_panel_one, session, m,
                              rounds.panel_prompt(session, m, r, ov, readable),
                              call, store)
                    for m in panel
                ]
                results = [f.result() for f in futures]
            results = [c for c in results if c]

        # (b) SYNTHESIS — the lead does the real work; CONSULT/DELEGATE and
        # SKILL requests remain available inside every round.
        governance.check(session, "generate_text")
        p = rounds.synthesis_prompt(session, council, role_agents, r, results, ov, readable)
        c = lead_call(lead, p)
        c = _resolve_skill_requests(session, lead, p, c, call, governance, store)
        c = _resolve_delegations(session, council, lead, p, c, call, store)
        # A synthesis that only ANNOUNCES or ATTEMPTS the work ("I'll read the
        # files, then deliver..." / blocked tool-call debris) must not be
        # accepted as DONE — re-call once demanding the result now. A second
        # stub is noted and the composer synthesizes from the panel views
        # instead (the proven rescue path).
        if rounds.reply_is_stub(c.content):
            store.log_event(sid, "synthesis_stub_retry",
                            {"round": r, "stub": c.content.strip()[:200]})
            nudge = (
                f"{p}\n\nIMPORTANT: your previous reply only ANNOUNCED or "
                f"ATTEMPTED what you were going to do — it began: "
                f"\"{c.content.strip()[:300]}\". You cannot go off and do "
                "anything outside this reply, and any tool-call syntax you emit "
                "is ignored — you have NO native tools here. Deliver the "
                "complete result NOW, in this reply, as plain text. If you need "
                "file contents first, emit 'SKILL: read_file <path>' lines and "
                "the results will be handed back to you."
            )
            c = lead_call(lead, nudge)
            c = _resolve_skill_requests(session, lead, nudge, c, call, governance, store)
            c = _resolve_delegations(session, council, lead, nudge, c, call, store)
            if rounds.reply_is_stub(c.content):
                session.unresolved.append(
                    "lead synthesis was a stub twice; final answer composed from "
                    "the panel views instead")
        decision, why = rounds.parse_round_decision(c.content)
        store.log_event(sid, "round_synthesized",
                        {"round": r, "decision": decision, "why": why[:200]})
        if decision == "DONE":
            break
    session.stop_reason = "council produced a result"
    return False


def resume_deliberation(
    session: Session,
    manager: SessionManager,
    registry: AgentRegistry,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]] = None,
) -> Session:
    """Re-enter deliberation after a system InputRequest (round consent /
    promote target) was answered. _deliberate is re-entrant: completed rounds,
    contributions, and executed actions are all persisted state it skips."""
    session.stop_reason = None
    store.log_event(session.session_id, "session_resumed", {"from": "input"})
    return _deliberate(session, manager, registry, governance, store, role_agents)


def run_session(
    session: Session,
    manager: SessionManager,
    registry: AgentRegistry,
    governance: Governance,
    store: LogStore,
    role_agents: Optional[dict[Role, str]] = None,
) -> Session:
    sid = session.session_id

    # 2. Classify. Classification still reports risk/greenfield (informational,
    # shown in the UI) but no longer gates the run: work happens freely in the
    # sandbox/workspace, and the ONE hard gate is the promote approval at
    # delivery time (where a missing target is also asked for — see
    # _execute_actions). No pre-run pauses.
    cls = classify(session.task.text, role_agents)
    session.classification = cls
    if not session.budgets_locked:
        session.budgets = config.budgets_for(cls.complexity)
    store.log_event(sid, "classified", cls.model_dump())
    manager.transition(session, SessionStatus.classified)

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
        council = build_council(cls, role_agents, panel=session.panel)
        session.council = council
        store.log_event(sid, "council_formed", council.model_dump())

    manager.transition(session, SessionStatus.deliberating)

    start = time.monotonic()
    # image attachments are shown to vision-capable agents on every call
    images = image_inputs(store.data_dir, session.attachments)
    # Up-front context the LEAD starts with, so it never depends on a flaky seat or
    # remembering to request a skill: prior conversation turns, the established
    # folder's real source, and/or live web research for fact-needing questions.
    established_overview = "\n\n".join(p for p in (
        _conversation_overview(session),
        _established_overview(session, store.data_dir),
        _web_overview(session),
    ) if p)
    if established_overview:
        store.log_event(sid, "context_overview", {"chars": len(established_overview)})

    def call(member: CouncilMember, prompt: str, timeout_s: Optional[int] = None) -> Contribution:
        return _agent_call(session, registry, store, member, prompt,
                           timeout_s=timeout_s, reserve=config.COMPOSER_RESERVED_CALLS, images=images)

    def compose_call(member: CouncilMember, prompt: str) -> Contribution:
        return _agent_call(session, registry, store, member, prompt, images=images)

    def lead_call(member: CouncilMember, prompt: str) -> Contribution:
        # The lead authors whole files in one shot — give it extra headroom.
        return _agent_call(session, registry, store, member, prompt,
                           timeout_s=config.LEAD_TIMEOUT,
                           reserve=config.COMPOSER_RESERVED_CALLS, images=images)

    lead = council.get(Role.lead)
    try:
        # 5. Panel rounds: every enabled seat contributes in parallel, the lead
        # synthesizes (pulling in talents on demand) and declares the round DONE
        # or CONTINUE. After ROUNDS_PER_CONSENT rounds the human is asked before
        # the council runs another block.
        if not _has_proposals(session) and lead and lead.active and not session.compose_now:
            if _run_panel_rounds(session, manager, council, lead, call, lead_call,
                                 governance, store, role_agents, established_overview,
                                 start):
                return session  # paused for round consent

            # 7. Mid-flight approval gate (only trips if governance flagged something).
            if session.has_pending_approval:
                session.stop_reason = "human approval needed"
                manager.transition(session, SessionStatus.awaiting_approval)
                return session

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

    # 7b. Governed action execution: collect the lead's artifact proposals, gate
    # every action on a human approval, execute approved ones. If the task should
    # produce files but the lead only described them, materialize each file with a
    # focused single-file call.
    _collect_proposals(session, store)
    if not _has_proposals(session) and cls.produces_output:
        _materialize_artifacts(session, compose_call, store)
    # Free council-space actions first (writes/edits/tests — never pause), so
    # the goal loop can repair failing tests BEFORE anything is delivered.
    _execute_actions(session, manager, governance, store, promotes=False)

    # A large single-file artifact can exceed one model response and be cut off.
    # Finish it from where it stopped (append) instead of re-drafting — the old
    # failure mode that produced empty/partial HTML over and over.
    if lead and lead.active:
        _complete_truncated_artifacts(session, lead_call, lead, store)
        # The goal loop: failing tests come back to the lead for bounded repair.
        _run_test_fix_loop(session, manager, governance, store, lead, call, lead_call)

    # Delivery last: promotes (the one approval gate) and, if the destination
    # is unknown, the delivery-target question.
    if _execute_actions(session, manager, governance, store):
        return session  # paused in awaiting_approval / awaiting_input
    # Verify whenever the task produced (or was required to produce) files — NOT
    # just code tasks. A content/design task that emitted an empty or truncated
    # file must NOT be reported as a confident success. A code task must produce a
    # real file even if it emitted none.
    _needs_file = cls.task_type == TaskType.code
    _has_file_actions = any(a.kind in _FILE_OUTPUT_KINDS for a in session.proposed_actions)
    if (_needs_file or _has_file_actions) and not _verify_artifact_outputs(
        session, store, require_file=_needs_file
    ):
        manager.transition(session, SessionStatus.composing)
        session.final = FinalAnswer(
            answer=(
                "The run failed artifact verification: the coordinator did not find "
                "a real, non-empty, complete file artifact on disk, so this result "
                "is NOT being reported as a success. See the unresolved risks below."
            ),
            confidence="low",
            assumptions=[],
            risks_unresolved=list(session.unresolved),
            next_action="Fix artifact generation and rerun the task.",
        )
        if not session.turns:
            session.turns.append({"role": "user", "text": session.task.text})
        session.turns.append({"role": "council", "text": session.final.answer})
        manager.transition(session, SessionStatus.done)
        store.log_event(sid, "final_composed", session.final.model_dump())
        store.save_session(session)
        return session

    # 10. Final response
    manager.transition(session, SessionStatus.composing)
    # A task that produced verified file artifacts gets a FAST, deterministic
    # summary built from the lead's own rationale + a real file manifest — no
    # second model call. This both halves latency for builds AND removes a whole
    # class of bugs (a summarizer fed a 12KB file would echo/continue it). The LLM
    # summarizer is reserved for pure-answer tasks (questions/research) where
    # synthesis genuinely adds value.
    delivered = [a for a in session.proposed_actions
                 if a.kind in _FILE_OUTPUT_KINDS and a.status == "executed" and a.result_path]
    # Deterministic manifest ONLY for genuine output/build tasks. A question or
    # research task that incidentally saved a file still gets real LLM synthesis
    # (its analysis would otherwise be lost — it lives in the deliberation, not in
    # a file manifest).
    if delivered and cls.produces_output:
        session.final = _build_summary_final(session, delivered)
        store.log_event(sid, "build_summary", {"files": [a.filename for a in delivered]})
    else:
        # For a pure-answer task, a substantial lead DONE-synthesis IS the
        # answer — deterministic, one call saved, nothing lost in
        # re-compression. Thin/CONTINUE/absent syntheses get real composition.
        session.final = _synthesis_final(session) if not cls.produces_output else None
        if session.final is not None:
            store.log_event(sid, "synthesis_final", {"chars": len(session.final.answer)})
        else:
            try:
                session.final = compose(session, council, compose_call)
            except AgentInputRequired as e:
                return _pause_for_input(session, manager, store, e, purpose="compose")
    # Record this turn in the conversation: the human's message (the original task
    # on turn one; on a follow-up it was appended by continue_session) and the
    # council's conclusion. The human can now respond and keep the thread going.
    if not session.turns:
        session.turns.append({"role": "user", "text": session.task.text})
    session.turns.append({"role": "council", "text": session.final.answer})
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

# 'EDIT: <filename>' + a git-conflict-style OLD/NEW block proposes a surgical
# replace in an existing file. Self-delimited (>>>>>>> ends it). The conflict
# markers require 7+ chars (the emitted contract uses exactly 7) so ordinary
# content — a Python doctest '>>> f()', an RST '======' heading rule — inside
# OLD/NEW does NOT prematurely terminate the capture.
_EDIT_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?EDIT(?:\*\*)?[ \t]*:[ \t]*(?P<file>.+?)[ \t]*\n"
    r"[ \t]*<{7,}[^\n]*\n(?P<old>.*?)\n[ \t]*={7,}[^\n]*\n(?P<new>.*?)\n[ \t]*>{7,}[^\n]*",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

# 'RUNTESTS: <command>' proposes a (free) test run; command optional.
_RUNTESTS_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?RUN_?TESTS(?:\*\*)?[ \t]*:[ \t]*(?P<cmd>.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# 'PROMOTE: <filename>' proposes copying a council file into the established
# folder — the ONE approval-gated boundary that touches real user code.
_PROMOTE_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?PROMOTE(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?\s*(?P<file>.+?)\s*(?:\*\*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# any block start — bounds ARTIFACT content so a following EDIT/RUNTESTS/PROMOTE
# isn't swallowed into the file body. A COLON is required so ordinary prose inside
# a file body ("Edit the .env file", "Run tests before shipping") is NOT mistaken
# for a block boundary (which silently truncated the file at that line).
_BLOCK_START = re.compile(
    r"^[ \t]*(?:\*\*)?(?:ARTIFACT|EDIT|RUN_?TESTS|PROMOTE)(?:\*\*)?[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_proposals(sid: str, text: str) -> list[ProposedAction]:
    """Parse a draft's ARTIFACT/EDIT/RUNTESTS/PROMOTE blocks into ProposedActions,
    in document order (so writes/edits precede a test run)."""
    starts = sorted(m.start() for m in _BLOCK_START.finditer(text))

    def _content_end(after: int) -> int:
        return next((s for s in starts if s > after), len(text))

    found: list[tuple[int, ProposedAction]] = []
    for m in _ARTIFACT_MARKER.finditer(text):
        fn = m.group(1).strip()
        body = _clean_artifact_body(text[m.end():_content_end(m.end())], fn)
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="write_file", role=Role.implementer,
            filename=fn, content=body, args={"filename": fn, "content": body})))
    for m in _EDIT_MARKER.finditer(text):
        fn = m.group("file").strip()
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="edit_file", role=Role.implementer, filename=fn,
            args={"filename": fn, "old": m.group("old"), "new": m.group("new")})))
    for m in _RUNTESTS_MARKER.finditer(text):
        cmd = (m.group("cmd") or "").strip()
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="run_tests", role=Role.implementer,
            filename=cmd or "pytest -q", args={"command": cmd})))
    # PROMOTE is collected even without an established folder: the missing
    # delivery target is asked for at execution time (_execute_actions), not
    # assumed — and never asked up front when there may be nothing to deliver.
    for m in _PROMOTE_MARKER.finditer(text):
        fn = m.group("file").strip()
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="promote", role=Role.implementer,
            filename=fn, args={"filename": fn})))
    return [action for _, action in sorted(found, key=lambda t: t[0])]


def _append_proposals(session: Session, store: LogStore, actions: list[ProposedAction]) -> None:
    for action in actions:
        session.proposed_actions.append(action)
        store.log_event(
            session.session_id, "action_proposed",
            {"action_id": action.action_id, "kind": action.kind, "filename": action.filename},
        )


def _collect_proposals(session: Session, store: LogStore) -> None:
    """Turn the lead's final draft into ProposedActions (loop step 7b).
    Idempotent: not re-collected on resume."""
    if _has_proposals(session):
        return
    draft = next(
        (c for c in reversed(session.contributions) if c.role in (Role.lead, Role.implementer)),
        None,
    )
    if draft is None:
        return
    _append_proposals(session, store, _parse_proposals(session.session_id, draft.content))


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


def _html_doc_end(low: str) -> int:
    """Index just past the STRUCTURAL </html> in a lowercased HTML string, or -1.
    The structural close is the FIRST </html> that sits after </body>, so:
      - a </html> appearing earlier inside a <script> string isn't mistaken for it
        (that one precedes </body> → fall back to the last </html>), and
      - a </html> in TRAILING PROSE after the real document is NOT picked up
        (we take the first structural one and drop everything after it)."""
    first = low.find("</html>")
    if first == -1:
        return -1
    body_close = low.rfind("</body>")
    idx = low.rfind("</html>") if (body_close != -1 and first < body_close) else first
    return idx + len("</html>")


def _clean_artifact_body(raw: str, filename: str = "") -> str:
    """Extract the real file body from an agent's ARTIFACT content. Agents often
    (a) wrap the file in a ```fence and (b) append an explanation; a naive strip
    leaves the closing ``` + prose IN the file (it renders as junk after the
    document — the bug the owner hit). Strategy:
      - HTML/SVG: slice exactly the document by its own opening/closing tags
        (structural close, not a mention in trailing prose or a JS string).
      - A whole-file ``` wrap (EXACTLY one opening fence at the top + one closing
        fence, nothing more) is stripped. A body with MORE fences is its own
        content (e.g. a README documenting code blocks) — left untouched so it is
        never mangled.
      - Else: returned as-is (the prompt forbids fences/trailing prose at source)."""
    t = raw.strip()
    name = filename.lower()
    low = t.lower()
    if name.endswith((".html", ".htm")):
        starts = [i for i in (low.find("<!doctype"), low.find("<html")) if i != -1]
        e = _html_doc_end(low)
        if starts and e != -1 and e > min(starts):
            return t[min(starts):e].strip()
    elif name.endswith(".svg"):
        s = low.find("<svg")
        e = low.rfind("</svg>")
        if s != -1 and e != -1 and e + len("</svg>") > s:
            return t[s:e + len("</svg>")].strip()
    if t.startswith("```"):
        lines = t.splitlines()
        fences = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
        if len(fences) == 2 and fences[0] == 0:  # a clean whole-file wrapper
            return "\n".join(lines[1:fences[1]]).strip()
    return t


def _intended_filenames(session: Session) -> list[str]:
    """The files this task means to produce: explicit ARTIFACT names first, then
    any filename-like tokens in the lead's draft and the task text."""
    draft = next(
        (c for c in reversed(session.contributions) if c.role in (Role.lead, Role.implementer)),
        None,
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
    if _has_proposals(session):
        return
    lead = session.council.get(Role.lead)
    if not (lead and lead.active):
        return
    filenames = _intended_filenames(session)
    if not filenames:
        return
    sid = session.session_id
    store.log_event(sid, "materialize_start", {"files": filenames})
    for fn in filenames:
        try:
            result = call(lead, materialize_prompt(session, fn))
        except (BudgetExceeded, AgentError) as e:
            session.unresolved.append(f"could not materialize '{fn}': {e}")
            store.log_event(sid, "materialize_skipped", {"file": fn, "error": str(e)})
            continue
        except AgentInputRequired:
            session.unresolved.append(f"materialization of '{fn}' needed input; skipped")
            store.log_event(sid, "materialize_skipped", {"file": fn, "reason": "input_required"})
            continue
        content = _clean_artifact_body(result.content, fn)
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


def _looks_truncated(path: Path) -> bool:
    """Heuristic: was this written file cut off mid-generation? HTML must close
    its </html>; any other sizeable file that ends abruptly (no trailing newline)
    is treated as likely-cut. Conservative so complete files aren't touched."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.strip():
        return False
    low = text.lower()
    suf = path.suffix.lower()
    # Only flag a file that OPENED a full HTML/SVG document but never closed it —
    # a reliable truncation signal. Do NOT guess for code/data files (js/css/json/
    # py): the old '>2KB without a trailing newline' heuristic false-fired on
    # essentially every correct such file and appended junk via a bogus
    # continuation. A fragment/partial HTML (no <html> opening) is intentional.
    if suf in (".html", ".htm"):
        return ("<!doctype" in low or "<html" in low) and "</html>" not in low
    if suf == ".svg":
        return "<svg" in low and "</svg>" not in low
    return False


def continuation_prompt(session: Session, filename: str, tail: str) -> str:
    return (
        f"Task: {session.task.text}\n"
        f"You were writing the file '{filename}' but your output was CUT OFF before "
        "the file was complete. Here are the LAST characters you produced:\n"
        f"-----\n{tail}\n-----\n"
        "Output ONLY the remaining bytes of the file, continuing from EXACTLY where "
        "that left off to the true end of the file. Do not repeat any of the text "
        "above, do not add explanation or commentary, do not wrap it in code "
        "fences — just the raw continuation, ready to append verbatim."
    )


def _complete_truncated_artifacts(
    session: Session, call: AgentCall, lead: CouncilMember, store: LogStore
) -> None:
    """Finish any cut-off file by asking the lead to CONTINUE from where it
    stopped (appending), instead of re-drafting it from scratch. Bounded by
    MAX_ARTIFACT_CONTINUATIONS; degrades gracefully on budget/agent errors."""
    sid = session.session_id
    for action in session.proposed_actions:
        if action.kind not in ("write_file", "promote") or action.status != "executed":
            continue
        if not action.result_path:
            continue
        path = Path(action.result_path)
        for attempt in range(config.MAX_ARTIFACT_CONTINUATIONS):
            if not _looks_truncated(path):
                break
            try:
                tail = path.read_text(encoding="utf-8", errors="replace")[
                    -config.ARTIFACT_CONTINUATION_TAIL_CHARS:
                ]
            except OSError:
                break
            store.log_event(sid, "artifact_continuation",
                            {"file": action.filename, "attempt": attempt + 1})
            try:
                result = call(lead, continuation_prompt(session, action.filename, tail))
            except (BudgetExceeded, AgentError, AgentInputRequired) as e:
                session.unresolved.append(f"could not finish '{action.filename}': {e}")
                store.log_event(sid, "artifact_continuation_failed",
                                {"file": action.filename, "error": str(e)})
                break
            addition = _strip_code_fence(result.content)
            # If the continuation closed the document and then rambled, keep only
            # up to </html> so commentary never lands in the file.
            low_add = addition.lower()
            if "</html>" in low_add:
                addition = addition[:low_add.rfind("</html>") + len("</html>")] + "\n"
            if not addition.strip():
                break
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(addition)
            except OSError as e:
                session.unresolved.append(f"could not append to '{action.filename}': {e}")
                break
            store.log_event(sid, "artifact_continued",
                            {"file": action.filename, "added": len(addition)})


def _execute_actions(
    session: Session, manager: SessionManager, governance: Governance, store: LogStore,
    promotes: bool = True,
) -> bool:
    """Drive every proposed action through its approval lifecycle; execute the
    approved ones. Returns True when the session must pause for the human.
    Deterministic and resume-safe — re-entered after each approval decision.
    With promotes=False, only the FREE council-space actions run (writes/edits/
    tests) — the pass the goal loop repairs against before anything is
    delivered; promotes (and the delivery-target question) wait for the final
    pass."""
    sid = session.session_id
    # A promote with no delivery target: ask the human WHERE at delivery time
    # (never up front — there may have been nothing to deliver). One question,
    # once; if the human already answered 'workspace', the promotes are skipped.
    needs_target = [a for a in session.proposed_actions
                    if a.kind == "promote" and a.status == "proposed"]
    if not promotes:
        needs_target = []
    if needs_target and not session.established_root:
        if session.established_asked:
            for a in needs_target:
                a.status = "denied"
                a.error = "no delivery target (files kept in the council workspace)"
                session.unresolved.append(
                    f"'{a.filename}' not delivered: kept in the council workspace")
        else:
            req = InputRequest(
                session_id=sid, agent="system", role=Role.coordinator,
                round=session.current_round, purpose="promote_target", resume_token="",
                question=(
                    "The council wants to deliver: "
                    + ", ".join(a.filename for a in needs_target)
                    + ". Where should these files go? Reply with a folder path "
                      "(you'll approve each file with a diff), or 'workspace' to "
                      "keep them in the council's workspace/sandbox."
                ),
            )
            session.input_requests.append(req)
            session.stop_reason = "needs a delivery target"
            store.log_event(sid, "input_requested", req.model_dump())
            manager.transition(session, SessionStatus.awaiting_input)
            return True
    pending = False
    for action in session.proposed_actions:
        if action.kind == "promote" and not promotes:
            continue
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
                    f"'{action.filename}' not delivered ({action.kind}): approval denied"
                )
                store.log_event(sid, "action_denied", {"action_id": action.action_id})
                continue
            else:
                pending = True
                continue
        if action.status == "approved":
            try:
                result = executor.execute(session, action, store.data_dir)
                action.status = "executed"
                action.result_path = result  # path for writes/edits/promote; output for run_tests
                if action.kind in ("write_file", "edit_file", "promote", "stage"):
                    session.files_changed.append(result)
                else:  # run_tests (or other non-file actions) — keep the output visible
                    session.unresolved.append(f"{action.kind} '{action.filename}':\n{result}")
                session.tools_called.append(action.kind)
                store.log_event(
                    sid, "action_executed",
                    {"action_id": action.action_id, "kind": action.kind, "result": result[:500]},
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


_FILE_OUTPUT_KINDS = {"write_file", "edit_file", "promote", "stage"}


def _tests_failed(action: ProposedAction) -> Optional[str]:
    """The failure text of a run_tests action, or None when it passed. The
    handler tags line 2 of its output '[passed]' or '[exit N]'; timeouts and
    unrunnable commands surface as a failed action with an error."""
    if action.kind != "run_tests":
        return None
    if action.status == "failed":
        return f"test command could not run: {action.error}"
    if action.status == "executed" and action.result_path and "[passed]" not in action.result_path:
        return action.result_path
    return None


def _run_test_fix_loop(
    session: Session, manager: SessionManager, governance: Governance, store: LogStore,
    lead: CouncilMember, call: AgentCall, lead_call: AgentCall,
) -> None:
    """The goal loop: while the latest test run fails, show the lead the real
    failure output and apply its EDIT/ARTIFACT repairs, re-running the tests
    after each attempt — bounded by MAX_TEST_FIX_ATTEMPTS (persisted on the
    session) and the agent-call budget, and all BEFORE anything is promoted.
    A build either ships passing its own tests or says exactly why not."""
    sid = session.session_id
    while session.test_fix_attempts < config.MAX_TEST_FIX_ATTEMPTS:
        latest = next((a for a in reversed(session.proposed_actions)
                       if a.kind == "run_tests"), None)
        if latest is None:
            return
        failure = _tests_failed(latest)
        if failure is None:
            return
        remaining = (session.budgets.max_agent_calls - session.agent_calls
                     - config.COMPOSER_RESERVED_CALLS)
        if remaining < 1:
            session.unresolved.append("test-fix loop stopped: agent-call budget exhausted")
            return
        session.test_fix_attempts += 1
        attempt = session.test_fix_attempts
        store.log_event(sid, "test_fix_attempt",
                        {"attempt": attempt, "failure": failure[:300]})
        files = sorted({a.filename for a in session.proposed_actions
                        if a.kind in ("write_file", "edit_file") and a.filename})
        p = rounds.test_fix_prompt(session, failure, files, attempt,
                                   config.MAX_TEST_FIX_ATTEMPTS,
                                   _readable_files(session, store.data_dir))
        try:
            c = lead_call(lead, p)
            c = _resolve_skill_requests(session, lead, p, c, call, governance, store)
        except (AgentError, BudgetExceeded) as e:
            session.unresolved.append(f"test-fix attempt {attempt} failed: {e}")
            return
        new_actions = _parse_proposals(sid, c.content)
        fixes = [a for a in new_actions if a.kind in ("write_file", "edit_file")]
        reruns = [a for a in new_actions if a.kind == "run_tests"]
        if not fixes and not reruns:
            session.unresolved.append(
                f"tests still failing; the lead offered no fix on attempt {attempt}: "
                + " ".join(c.content.split())[:300])
            return
        # the same command re-runs unless the lead explicitly changed it
        if not reruns:
            reruns = [ProposedAction(session_id=sid, kind="run_tests",
                                     role=Role.implementer, filename=latest.filename,
                                     args=dict(latest.args))]
        _append_proposals(session, store, fixes + reruns)
        _execute_actions(session, manager, governance, store, promotes=False)
    latest = next((a for a in reversed(session.proposed_actions)
                   if a.kind == "run_tests"), None)
    if latest is not None and _tests_failed(latest) is not None:
        note = f"tests still failing after {config.MAX_TEST_FIX_ATTEMPTS} fix attempts"
        if note not in session.unresolved:
            session.unresolved.append(note)


def _lead_preamble(session: Session) -> str:
    """The lead's rationale text BEFORE its first ARTIFACT block — the 'what/why'
    of the build. Used for a fast deterministic summary without a second model
    call. Empty when the lead jumped straight into the file."""
    draft = next(
        (c for c in reversed(session.contributions) if c.role in (Role.lead, Role.implementer)),
        None,
    )
    if draft is None:
        return ""
    m = _ARTIFACT_MARKER.search(draft.content)
    pre = (draft.content[:m.start()] if m else draft.content).strip()
    # guard against the lead pasting file-ish content into the preamble
    return _clean_artifact_body(pre) if pre.startswith("```") else pre


def _human_size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "?"
    return f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} B"


_KIND_VERB = {"write_file": "written to", "edit_file": "edited at",
              "promote": "delivered to", "stage": "staged to"}
_KIND_RANK = {"promote": 3, "stage": 2, "edit_file": 1, "write_file": 0}


def _build_summary_final(session: Session, delivered: list[ProposedAction]) -> FinalAnswer:
    """A deterministic, corruption-proof final answer for a build: the lead's own
    rationale plus a real manifest of the files written. High confidence is
    EARNED — this runs only after artifact verification has already passed."""
    # one entry per filename, preferring its final destination (a file written to
    # the sandbox and then promoted should read as 'delivered', not listed twice)
    best: dict[str, ProposedAction] = {}
    for a in delivered:
        cur = best.get(a.filename)
        if cur is None or _KIND_RANK[a.kind] > _KIND_RANK[cur.kind]:
            best[a.filename] = a
    lines = [
        f"- {a.filename} ({_human_size(Path(a.result_path))}) — {_KIND_VERB[a.kind]} {a.result_path}"
        for a in best.values()
    ]
    manifest = "Files written:\n" + "\n".join(lines)
    html = next((a for a in best.values() if a.filename.lower().endswith((".html", ".htm"))), None)
    hint = f"\n\nOpen {html.filename} in a web browser to use it." if html else ""
    preamble = _lead_preamble(session)
    if len(preamble) > 1400:
        preamble = preamble[:1400].rstrip() + " …"
    answer = (f"{preamble}\n\n{manifest}{hint}" if preamble else f"{manifest}{hint}").strip()
    # don't surface the (passed) verification chatter as a risk
    risks = [u for u in session.unresolved if "verification" not in u.lower()]
    return FinalAnswer(answer=answer, confidence="high", assumptions=[], risks_unresolved=risks)


def _verify_artifact_outputs(session: Session, store: LogStore, require_file: bool = False) -> bool:
    """Deterministic guardrail run whenever a task produced (or had to produce)
    file artifacts. Every executed file must exist and be real — non-empty after
    stripping whitespace, and (for HTML) a complete document. A task that
    attempted file output but landed nothing, or a task that was REQUIRED to
    produce a file (require_file) but produced none, fails. A pure-answer task
    with no file actions and no requirement has nothing to verify and passes."""
    file_actions = [a for a in session.proposed_actions if a.kind in _FILE_OUTPUT_KINDS]
    executed = [a for a in file_actions if a.status == "executed" and a.result_path]
    failures: list[str] = []

    if not executed:
        if file_actions or require_file:
            # files were attempted (and all failed) or were mandatory — not a success
            failures.append("no file artifact was successfully written to disk")
        else:
            return True  # nothing was meant to be produced; nothing to verify

    for action in executed:
        path = Path(action.result_path)
        try:
            if not path.is_file():
                failures.append(f"{action.filename}: result path does not exist ({path})")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                failures.append(f"{action.filename}: file is empty/blank")
                continue
            if path.suffix.lower() in {".html", ".htm"}:
                low = text.lower()
                has_open = "<!doctype" in low or "<html" in low
                if has_open and "</html>" not in low:
                    failures.append(f"{action.filename}: HTML document is incomplete (no closing </html>)")
                elif has_open:
                    tail = text[_html_doc_end(low):].strip()
                    if len(tail) > 40:  # commentary/fence leaked in after the document
                        failures.append(
                            f"{action.filename}: {len(tail)} chars of stray content after </html> "
                            "(not a clean single file)")
                # a fragment/partial (no <html> opening) is valid — only the
                # non-empty check above applies to it
        except OSError as e:
            failures.append(f"{action.filename}: verification error: {e}")

    if failures:
        message = "artifact verification failed: " + "; ".join(failures)
        session.unresolved.append(message)
        store.log_event(session.session_id, "artifact_verification_failed", {"failures": failures})
        return False

    store.log_event(
        session.session_id,
        "artifact_verification_passed",
        {"files": [a.result_path for a in executed]},
    )
    return True


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

    # Deliberation pause: the human answered an agent's question mid-run; the
    # answer (and the resumed call's output) are now in the session context.
    # Collect any files the resumed answer produced, then re-enter the lead flow
    # to finish — execution, verification, continuation, and composition.
    session.current_round = req.round
    _collect_proposals(session, store)
    return _deliberate(session, manager, registry, governance, store, role_agents)
