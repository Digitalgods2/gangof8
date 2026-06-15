"""Deliberation Loop — the 10-step coordinator loop (DESIGN.md section 3).

The Coordinator is code, not an agent. Every loop is bounded by the session
budgets; exceeding any cap force-stops with a partial answer.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import config, executor, skills
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
    if session.agent_calls >= cap:
        raise BudgetExceeded(
            f"max_agent_calls={session.budgets.max_agent_calls} reached"
            + (f" (cap {cap} with {reserve} reserved for composition)" if reserve else "")
        )
    # Per-agent timeout: the gemini CLI needs more headroom than claude/codex.
    if timeout_s is None:
        timeout_s = config.agent_timeout(member.agent)
    # Tag this worker thread with the session so the CLI adapter can register its
    # subprocess for hard cancellation (kill on request).
    cancellation.set_current_session(session.session_id)
    try:
        result = registry.call(member.agent, member.role, prompt, timeout_s, images=images)
    except AgentInputRequired as e:
        e.role = member.role  # enrich with call-site context for the InputRequest
        e.agent_name = member.agent
        raise
    finally:
        cancellation.set_current_session(None)
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
    "READING FILES: you have NO direct filesystem access and do not need it — and "
    "this is NOT a restriction on what you can analyze. NEVER say you 'cannot "
    "access' a path or that a folder is 'outside your workspace'; that is false "
    "here. The coordinator reads files FOR you, including folders outside its own "
    "directory. To see a folder, emit 'SKILL: list_dir .'; to read a file, "
    "'SKILL: read_file <path>'; to search, 'SKILL: search_project <query>'. The "
    "results are handed back to you. Use these instead of refusing, and base your "
    "analysis on what they return — never invent file contents.\n"
)


def build_prompt(
    session: Session, spec: RoundSpec, role: Role, readable: list[str] = (),
    established_overview: str = "",
) -> str:
    # Advertise the no-approval discovery skills this role may pull mid-round,
    # but only when there's something to look at: an established folder being
    # examined, a workspace, or files already produced.
    has_dir = bool(session.established_root or session.workspace_root)
    where = "established folder" if session.established_root else "project"
    hints: list[str] = []
    ld = get_skill("list_dir")
    if has_dir and ld and role in ld.allowed_roles:
        hints.append(
            f"list the {where}'s files with a line 'SKILL: list_dir .' (use a subfolder to drill in)"
        )
    sp = get_skill("search_project")
    if has_dir and sp and role in sp.allowed_roles:
        hints.append(
            f"search the {where} with a line 'SKILL: search_project <query>'"
        )
    rf = get_skill("read_file")
    if (readable or has_dir) and rf and role in rf.allowed_roles:
        avail = f" Available now: {', '.join(readable)}." if readable else ""
        hints.append(f"read a file with a line 'SKILL: read_file <path>'.{avail}")
    ws = get_skill("web_search")
    if config.WEB_ENABLED and ws and role in ws.allowed_roles:
        hints.append("search the live web with 'SKILL: web_search <query>' "
                     "(and read a page with 'SKILL: web_fetch <url>')")
    cap = (
        "You may " + "; ".join(hints) + " (results are returned to you, no approval needed).\n"
        if hints else ""
    )
    overview = f"{established_overview}\n\n" if established_overview else ""
    return (
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"Round {spec.round} objective: {spec.goal}\n"
        f"Your role: {role.value}. Answer only from this role.\n"
        f"Output requirement: {spec.output_requirement}\n"
        f"{cap}"
        f"{overview}"
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
        f"Disagreement on: {d.topic}\n"
        f"Positions:\n{positions}\n"
        "Your role: critic — RULE this disagreement only. Reply in AT MOST 3 "
        "lines: line 1 is exactly 'VERDICT: uphold' or 'VERDICT: overturn'; then "
        "1–2 sentences naming the decisive evidence. Do NOT restate the task, do "
        "NOT list recommendations, do NOT write an essay — just the verdict and "
        "the reason."
    )


def draft_prompt(session: Session, established_overview: str = "") -> str:
    # When an established folder is in play, the implementer drafts freely in the
    # sandbox and PROMOTES the finished files into the real folder (the one gate).
    if session.established_root:
        promote = (
            f"\nThis task targets the EXISTING folder: {session.established_root}\n"
            "Your ARTIFACT/EDIT blocks are written into your own sandbox FREELY (no "
            "approval) so you can build and test. NOTHING reaches the real folder until "
            "you PROMOTE it AND the human approves. For each file that should land in "
            "the real folder, add a line:\n"
            "PROMOTE: <filename>   (one per file you want delivered)\n"
        )
    else:
        promote = (
            "\nYour ARTIFACT/EDIT blocks are written into your own sandbox FREELY (no "
            "approval needed) — you need no filesystem access yourself.\n"
        )
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
        "not describe the files in prose — write them out in full.\n"
        f"{promote}"
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
        "\nTo MODIFY an existing file (instead of overwriting it), emit a surgical "
        "edit — the OLD snippet must be unique in the file:\n"
        "EDIT: path/to/file.py\n"
        "<<<<<<< OLD\n"
        "<exact existing text to replace>\n"
        "=======\n"
        "<replacement text>\n"
        ">>>>>>> NEW\n"
        "To run the test suite in your sandbox (free, no approval), add a line:\n"
        "RUNTESTS: <command>   (e.g. RUNTESTS: pytest -q; omit the command to default)\n"
        f"{chr(10) + established_overview if established_overview else ''}"
        f"\nContext so far:\n{_recent_context(session, limit=5)}"
    )


def review_prompt(session: Session, draft: Contribution) -> str:
    return (
        f"Task: {session.task.text}\n"
        "Your role: critic. Review the draft below for flaws. Hold a real bar: "
        "only say the single word 'acceptable' when it genuinely meets the task; "
        "otherwise give specific, actionable objections (what is wrong and what to "
        "change) so it can be improved.\n"
        f"Draft:\n{draft.content[:1800]}"
    )


def _review_accepts(review: str) -> bool:
    """The critic accepts iff 'acceptable' appears AND is not negated — so
    'not acceptable yet' / 'unacceptable' correctly count as a REJECTION."""
    t = (review or "").lower()
    if "not acceptable" in t or "unacceptable" in t or "isn't acceptable" in t \
            or "not yet acceptable" in t:
        return False
    return "acceptable" in t


def refine_prompt(session: Session, established_overview: str, prior_review: str) -> str:
    """A redraft that must resolve the critic's objections — the engine of
    convergence. Falls back to a plain draft when there's nothing to address."""
    base = draft_prompt(session, established_overview)
    pr = (prior_review or "").strip()
    if not pr or pr.lower().startswith("acceptable"):
        return base
    return (
        base
        + "\n\nYour previous draft was NOT accepted. The critic's objections:\n"
        + pr[:1800]
        + "\nProduce a REVISED version that fully resolves these objections — keep "
        "what worked, fix what was flawed, and output the COMPLETE result again in "
        "the required format (do not describe the changes)."
    )


def _converge(
    session: Session, council: Council, call: AgentCall,
    established_overview: str, store: LogStore, start: float,
) -> tuple[str, str]:
    """Refine the draft until the critic ACCEPTS it — the real terminator for an
    output task. Repeats draft→critique, feeding each critique back, and stops on
    acceptance OR a backstop (refine-iteration cap, wall-time; the agent-call
    budget raises BudgetExceeded on its own). Returns (verdict, stop_reason)."""
    implementer = council.get(Role.implementer)
    critic = council.get(Role.critic)
    sid = session.session_id
    if not (implementer and implementer.active and critic and critic.active):
        return "revise", "max rounds reached"  # nothing to converge with
    last_review = next(
        (c.content for c in reversed(session.contributions) if c.role == Role.critic), "")
    for i in range(config.MAX_REFINE_ITERATIONS):
        if time.monotonic() - start > session.budgets.max_wall_seconds:
            session.unresolved.append("stopped refining: wall-time limit")
            return "revise", "refinement time limit"
        store.log_event(sid, "refine_round", {"iteration": i + 1})
        draft = call(implementer, refine_prompt(session, established_overview, last_review))
        review = call(critic, review_prompt(session, draft))
        if _review_accepts(review.content):
            store.log_event(sid, "converged", {"iterations": i + 1})
            return "accept", f"answer accepted (after {i + 1} refinement round(s))"
        last_review = review.content
    session.unresolved.append(
        f"stopped refining after {config.MAX_REFINE_ITERATIONS} rounds without critic acceptance")
    return "revise", "refinement cap reached without acceptance"


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

    # Greenfield gate: a build that creates something NEW, with no established
    # folder referenced, needs a destination — ASK rather than assume one.
    if cls.greenfield and not session.established_root and not session.established_asked:
        req = InputRequest(
            session_id=sid, agent="system", role=Role.coordinator,
            purpose="establish_target", resume_token="",
            question=(
                "This is a greenfield build, but you didn't reference a target folder. "
                "Where should the finished files go? Reply with a folder path to deliver "
                "there (you'll approve each file), or reply 'workspace' to keep them in "
                "the council's workspace/sandbox without delivering anywhere."
            ),
        )
        session.input_requests.append(req)
        session.stop_reason = "needs a build target"
        store.log_event(sid, "input_requested", req.model_dump())
        manager.transition(session, SessionStatus.awaiting_input)
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
    # If draft proposals were already collected, deliberation finished and we
    # paused in the action gate (step 7b). On resume, do NOT re-run the remaining
    # planned rounds (deliberation can early-stop on an accepted draft, leaving
    # rounds unrun) — fall straight through to execution/compose.
    if _has_proposals(session):
        completed_rounds = len(plan)
    manager.transition(session, SessionStatus.deliberating)

    start = time.monotonic()
    verdict: Optional[str] = None
    # image attachments are shown to vision-capable agents on every call
    images = image_inputs(store.data_dir, session.attachments)
    # Read the established folder ONCE up front so every agent starts with the
    # real code it was asked to examine (no dependence on an agent requesting it).
    # Up-front context the council starts with, so it never depends on a flaky
    # seat or an agent remembering to request a skill: the established folder's
    # real source AND/OR live web research for fact-needing questions.
    established_overview = "\n\n".join(p for p in (
        _established_overview(session, store.data_dir),
        _web_overview(session),
    ) if p)
    if established_overview:
        store.log_event(sid, "context_overview", {"chars": len(established_overview)})

    def call(member: CouncilMember, prompt: str) -> Contribution:
        return _agent_call(session, registry, store, member, prompt,
                           reserve=config.COMPOSER_RESERVED_CALLS, images=images)

    def compose_call(member: CouncilMember, prompt: str) -> Contribution:
        return _agent_call(session, registry, store, member, prompt, images=images)

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
                    p = build_prompt(session, spec, role, readable, established_overview)
                    try:
                        c = call(member, p)
                        c = _resolve_skill_requests(session, member, p, c, call, governance, store)
                    except AgentError as e:
                        # One flaky seat (e.g. the gemini CLI stalling) must not
                        # abort the whole council. Drop it for the rest of the run
                        # and continue with the others — graceful degradation.
                        store.log_event(sid, "seat_dropped",
                                        {"round": spec.round, "role": role.value,
                                         "agent": member.agent, "error": str(e)})
                        session.unresolved.append(
                            f"{role.value} seat ({member.agent}) dropped: {e}")
                        member.active = False
                        break
                    if c.content.strip():  # output requirement met (Phase 0: non-empty)
                        break

            # 6. Conflict check: isolate → critic tests → coordinator rules → log
            critic = council.get(Role.critic)
            new_disagreements = detect_disagreements(session, spec)
            for i, d in enumerate(new_disagreements):
                if critic and critic.active and i < config.MAX_CRITIC_TESTS_PER_ROUND:
                    d.critic_test = call(critic, test_both_sides_prompt(session, d)).content[: config.CRITIC_TEST_MAX_CHARS]
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
                draft = call(implementer, draft_prompt(session, established_overview))
                if critic and critic.active:
                    review = call(critic, review_prompt(session, draft))
                    verdict = "accept" if _review_accepts(review.content) else "revise"
                else:
                    verdict = "accept"

            # 9. Stop condition
            stop, reason = _stop_check(session, spec, len(plan), verdict)
            if stop:
                session.stop_reason = reason
                break

        # 9b. Convergence: if the planned phases ended with the draft NOT accepted
        # by the critic, keep refining (draft↔critique) until it IS accepted, or a
        # backstop trips (refine cap / agent-call budget / wall-time). The round
        # count is a safety ceiling now, no longer the normal terminator.
        if session.stop_reason == "max rounds reached" and verdict == "revise":
            verdict, session.stop_reason = _converge(
                session, council, call, established_overview, store, start)

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

    # 7b. Governed action execution: collect the implementer's artifact
    # proposals, gate every action on a human approval, execute approved ones.
    # If the task should produce files but the draft only described them,
    # materialize each file with a focused single-file call.
    _collect_proposals(session, store)
    if not _has_proposals(session) and cls.produces_output:
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

# 'EDIT: <filename>' + a git-conflict-style OLD/NEW block proposes a surgical
# replace in an existing file. Self-delimited (>>>>>>> ends it).
_EDIT_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?EDIT(?:\*\*)?[ \t]*:[ \t]*(?P<file>.+?)[ \t]*\n"
    r"[ \t]*<{3,}[^\n]*\n(?P<old>.*?)\n[ \t]*={3,}[^\n]*\n(?P<new>.*?)\n[ \t]*>{3,}[^\n]*",
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
# isn't swallowed into the file body.
_BLOCK_START = re.compile(
    r"^[ \t]*(?:\*\*)?(?:ARTIFACT|EDIT|RUN_?TESTS|PROMOTE)\b", re.IGNORECASE | re.MULTILINE
)


def _collect_proposals(session: Session, store: LogStore) -> None:
    """Turn the implementer's final draft into ProposedActions (loop step 7b):
    'ARTIFACT: <file>' → write_file, an 'EDIT: <file>' OLD/NEW block →
    edit_file, and 'RUNTESTS: <cmd>' → run_tests. Collected in document order so
    writes/edits precede a test run. Idempotent: not re-collected on resume."""
    if _has_proposals(session):
        return
    draft = next(
        (c for c in reversed(session.contributions) if c.role == Role.implementer), None
    )
    if draft is None:
        return
    text = draft.content
    sid = session.session_id
    starts = sorted(m.start() for m in _BLOCK_START.finditer(text))

    def _content_end(after: int) -> int:
        return next((s for s in starts if s > after), len(text))

    found: list[tuple[int, ProposedAction]] = []
    for m in _ARTIFACT_MARKER.finditer(text):
        fn = m.group(1).strip()
        body = _strip_code_fence(text[m.end():_content_end(m.start())].strip())
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
    # PROMOTE only means anything with an established folder to deliver into.
    if session.established_root:
        for m in _PROMOTE_MARKER.finditer(text):
            fn = m.group("file").strip()
            found.append((m.start(), ProposedAction(
                session_id=sid, kind="promote", role=Role.implementer,
                filename=fn, args={"filename": fn})))

    for _, action in sorted(found, key=lambda t: t[0]):
        session.proposed_actions.append(action)
        store.log_event(
            session.session_id, "action_proposed",
            {"action_id": action.action_id, "kind": action.kind, "filename": action.filename},
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
    if _has_proposals(session):
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
                    d.critic_test = call(critic, test_both_sides_prompt(session, d)).content[: config.CRITIC_TEST_MAX_CHARS]
                d.ruling, d.ruling_basis, d.rationale = coordinator_decide(d)
                session.disagreements.append(d)
                store.log_event(sid, "disagreement_ruled", d.model_dump())
        except AgentInputRequired as e:
            return _pause_for_input(session, manager, store, e, purpose="deliberation")
        except (BudgetExceeded, AgentError) as e:
            session.unresolved.append(f"conflict check skipped after input: {e}")

    return _deliberate(session, manager, registry, governance, store, role_agents)
