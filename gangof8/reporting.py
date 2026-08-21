"""Human-readable reporting derived from a session and its event log:

- council_health(): which seats dropped or were substituted, so a degraded run
  says so plainly instead of burying it in 'unresolved'.
- format_timeline(): turn the raw JSONL events into a readable run timeline.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DROP_RE = re.compile(r"(\w+) seat \(([\w.\-]+)\) dropped: (.*)", re.IGNORECASE)
_SUB_RE = re.compile(r"summarizer '([\w.\-]+)' failed.*recomposed with '([\w.\-]+)'", re.IGNORECASE)
_DEGRADE_HINTS = (
    "composer skipped", "stopped refining", "budget exhausted", "agent error",
    "could not build diff", "refinement cap", "refinement time limit",
    "unavailable before run",
)


def council_health(unresolved: list[str]) -> dict:
    """The degradation story for a run: dropped seats, summarizer substitutions,
    and other partial-result notes. `degraded` is True when anything went wrong.
    Takes the session's `unresolved` list so it works on a Session or its dict."""
    dropped: list[dict] = []
    substitutions: list[dict] = []
    notes: list[str] = []
    for u in (unresolved or []):
        m = _DROP_RE.search(u)
        if m:
            dropped.append({"role": m.group(1), "agent": m.group(2), "error": m.group(3)[:200]})
            continue
        m = _SUB_RE.search(u)
        if m:
            substitutions.append({"failed": m.group(1), "replaced_by": m.group(2)})
            continue
        if any(h in u.lower() for h in _DEGRADE_HINTS):
            notes.append(u[:200])
    return {
        "degraded": bool(dropped or substitutions or notes),
        "dropped": dropped,
        "substitutions": substitutions,
        "notes": notes,
    }


def run_summary(session: Any) -> dict:
    """Return an audit-friendly, compact summary for the dashboard/API.

    Session records intentionally keep the complete execution trail. This
    derived view makes the facts a user needs while deciding whether to promote
    or inspect output available without making the browser parse every event.
    """
    data = session.model_dump() if hasattr(session, "model_dump") else dict(session or {})
    contributions = data.get("contributions") or []
    actions = data.get("proposed_actions") or []
    by_agent: dict[str, int] = {}
    by_model: dict[str, int] = {}
    duration_ms = 0
    for contribution in contributions:
        agent = contribution.get("agent") or "unknown"
        by_agent[agent] = by_agent.get(agent, 0) + 1
        model = contribution.get("model")
        if model:
            by_model[model] = by_model.get(model, 0) + 1
        duration_ms += int(contribution.get("duration_ms") or 0)
    action_statuses: dict[str, int] = {}
    for action in actions:
        status = action.get("status") or "unknown"
        action_statuses[status] = action_statuses.get(status, 0) + 1

    package_elapsed_ms = 0
    if data.get("package_started_at"):
        try:
            started = datetime.fromisoformat(
                str(data["package_started_at"]).replace("Z", "+00:00")
            )
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            raw_status = data.get("status")
            status = getattr(raw_status, "value", raw_status)
            if status in {"done", "failed", "cancelled"} and data.get("updated_at"):
                ended = datetime.fromisoformat(
                    str(data["updated_at"]).replace("Z", "+00:00")
                )
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
            else:
                ended = datetime.now(timezone.utc)
            package_elapsed_ms = max(0, int((ended - started).total_seconds() * 1000))
        except (TypeError, ValueError):
            package_elapsed_ms = 0

    files: list[dict] = []
    for raw_path in data.get("files_changed") or []:
        path = Path(raw_path)
        item = {"path": str(path), "exists": path.is_file()}
        if path.is_file():
            try:
                item["bytes"] = path.stat().st_size
                item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                item["exists"] = False
        files.append(item)

    return {
        "agent_calls": data.get("agent_calls", len(contributions)),
        "successful_agent_calls": data.get("successful_agent_calls") or {},
        "agent_call_attempts": data.get(
            "agent_call_attempts", data.get("agent_calls", len(contributions))
        ),
        "contribution_duration_ms": duration_ms,
        "agent_attempt_duration_ms": int(
            data.get("agent_attempt_duration_ms") or duration_ms
        ),
        "package_elapsed_ms": package_elapsed_ms,
        "contributions_by_agent": by_agent,
        "contributions_by_model": by_model,
        "actions_by_status": action_statuses,
        "test_fix_attempts": data.get("test_fix_attempts", 0),
        "candidate_metrics": data.get("candidate_metrics") or {},
        "quality_gate": data.get("quality_gate") or {},
        "assembly_result": data.get("assembly_result") or {},
        "frontier_author_recoveries": data.get("frontier_author_recoveries") or {},
        "candidate_author_recoveries": data.get("candidate_author_recoveries") or {},
        "package_output_authors": data.get("package_output_authors") or {},
        "package_output_attempts": data.get("package_output_attempts") or {},
        "package_output_history": data.get("package_output_history") or {},
        "package_call_failures": data.get("package_call_failures") or {},
        "package_deadline_at": data.get("package_deadline_at"),
        "files": files,
    }


# event name -> (icon, friendly label)
_TIMELINE = {
    "agent_call_queued": ("...", "Model call queued"),
    "agent_call_started": (">", "Model working"),
    "agent_call_finished": ("+", "Model response received"),
    "agent_call_failed": ("!", "Model call failed"),
    "agent_call_discarded": ("!", "Late model response discarded"),
    "package_deadline_started": (">", "Shared package deadline started"),
    "package_author_fanout_started": (">>", "Atomic package owner dispatched"),
    "package_output_reassigned": ("↻", "Exact output reassigned"),
    "package_artifact_author_rejected": ("!", "Wrong package author rejected"),
    "frontier_author_recovery_started": ("↻", "Frontier author recovering"),
    "candidate_author_recovery_started": ("↻", "Candidate author correcting output"),
    "candidate_response_rejected": ("!", "Model responded without a usable candidate"),
    "candidate_file_recovered": ("+", "Tool-written candidate captured"),
    "frontier_runtime_repaired": ("+", "Frontier author repaired its code"),
    "frontier_runtime_repair_failed": ("!", "Frontier author repair failed"),
    "frontier_implementation_gate_failed": ("!", "Frontier implementation gate failed"),
    "package_implementation_gate_failed": ("!", "Package implementation gate failed"),
    "assembly_materialized": ("+", "Deterministic assembly materialized"),
    "deterministic_release_verified": ("✓", "Deterministic release verified"),
    "assembly_failed": ("!", "Deterministic assembly failed"),
    "assembly_template_rejected": ("!", "Assembly template rejected"),
    "assembly_template_provider_invalidated": ("!", "Upstream template attempt invalidated"),
    "assembly_template_rebuild_scheduled": (">>", "Upstream template rebuild scheduled"),
    "assembly_dependency_provider_invalidated": ("!", "Upstream dependency attempt invalidated"),
    "assembly_dependency_rebuild_scheduled": (">>", "Upstream dependency rebuild scheduled"),
    "assembly_dependency_repair_applied": ("+", "Assembly dependencies repaired and reverified"),
    "assembly_skill_request_rejected": ("!", "Assembly dependency read rejected"),
    "assembly_repair_skipped": (">", "Expanded artifact kept out of model repair"),
    "package_attempt_failed": ("!", "Package attempt failed"),
    "frontier_release_verdict": ("✓", "Independent frontier release verdict"),
    "frontier_release_repair_applied": ("+", "Frontier release repair applied"),
    "frontier_final_batch_verdict": ("✓", "Final-batch frontier verdict"),
    "frontier_final_batch_repair_applied": ("+", "Final-batch frontier repair applied"),
    "chair_defect_closure_incomplete": ("!", "Chair left defect closures open"),
    "runtime_deferred": (">>", "Runtime check deferred to integration"),
    "panel_artifact_rejected": ("!", "Panel draft rejected"),
    "integration_decided": ("+", "Integration decision"),
    "panel_seat_preflight_failed": ("!", "Panel seat unavailable before run"),
    "task_received": ("📥", "Task received"),
    "classified": ("🏷️", "Classified"),
    "council_formed": ("👥", "Council formed"),
    "round_start": ("🔄", "Round started"),
    "round_synthesized": ("🧩", "Round synthesized"),
    "panel_seat_dropped": ("⚠️", "Panel seat dropped"),
    "synthesis_stub_retry": ("✏️", "Lead re-asked (stub reply)"),
    "synthesis_stub_escalated": ("⏫", "Synthesis escalated to another seat"),
    "synthesis_stub_escalation_failed": ("⚠️", "Escalated synthesis seat failed"),
    "synthesis_final": ("📜", "Lead synthesis is the answer"),
    "test_fix_attempt": ("🔧", "Fixing failing tests"),
    "contribution": ("💬", "Agent contributed"),
    "skill_requested": ("🔧", "Skill requested"),
    "skill_resolved": ("📄", "Skill result"),
    "skill_failed": ("⚠️", "Skill failed"),
    "context_overview": ("📂", "Context gathered"),
    "established_overview": ("📂", "Source read"),
    "disagreement_ruled": ("⚖️", "Disagreement ruled"),
    "no_conflict": ("✅", "No conflict"),
    "critic_test_skipped": ("⏭️", "Critic test skipped"),
    "refine_round": ("✏️", "Refinement round"),
    "converged": ("🎯", "Critic accepted"),
    # lead-driven model: on-demand delegation + artifact continuation
    "delegation_requested": ("🤝", "Talent requested"),
    "delegation_granted": ("🤝", "Talent pulled in"),
    "delegation_retry": ("🔁", "Talent retried after a failure"),
    "delegation_reseated": ("🔁", "Talent reseated on another model"),
    "delegate_artifacts_captured": ("📦", "Talent's files captured"),
    "panel_artifact_saved": ("📄", "Panel draft saved to sandbox"),
    "candidates_collected": ("🗳️", "Candidates collected"),
    "candidate_scored": ("⚖️", "Candidate scored"),
    "candidate_rejected_runtime": ("💥", "Candidate rejected — does not run"),
    "candidate_frozen": ("🧊", "Candidate renders a static/frozen screen"),
    "candidates_frozen_dropped": ("🧊", "Static candidates set aside for live ones"),
    "candidates_ungrouped": ("🔀", "Differently-named candidates not in the judged group"),
    "best_of_n_all_failed_runtime": ("💥", "All candidates failed to run"),
    "judge_dropped": ("⚠️", "Judge dropped"),
    "winner_selected": ("🏆", "Best-of-N winner"),
    "chair_ratified": ("🪑", "Chair ratified the vote"),
    "chair_overrode": ("🪑", "Chair overrode the vote"),
    "chair_recovered": ("🛟", "Chair recovered a failed candidate"),
    "chair_recover_failed": ("⚠️", "Chair could not recover"),
    "winner_fixes_applied": ("🔧", "Winner fixes applied"),
    "winner_fixes_reverted": ("↩️", "Winner fix reverted (broke the file)"),
    "runtime_ok": ("✅", "File runs (headless smoke test)"),
    "delegation_resolved": ("📥", "Talent answered"),
    "delegation_denied": ("🚫", "Talent unavailable"),
    "delegation_failed": ("⚠️", "Delegation failed"),
    "artifact_continuation": ("✂️", "Finishing cut-off file"),
    "artifact_continued": ("➕", "File continued"),
    "artifact_continuation_failed": ("⚠️", "Could not finish file"),
    "action_proposed": ("📝", "Action proposed"),
    "action_executed": ("⚙️", "Action executed"),
    "action_denied": ("🚫", "Action denied"),
    "action_failed": ("❌", "Action failed"),
    "approval_requested": ("⏸️", "Approval requested"),
    "approval_resolved": ("👍", "Approval resolved"),
    "standing_approval_granted": ("🔓", "Standing approval granted"),
    "standing_approval_used": ("🔓", "Standing approval used"),
    "paused_for_approval": ("⏸️", "Paused for approval"),
    "input_requested": ("❓", "Question to you"),
    "delivery_target_defaulted": ("📁", "Delivering into the active workspace"),
    "input_answered": ("💡", "You answered"),
    "input_declined": ("🚫", "Input declined"),
    "seat_dropped": ("⚠️", "Seat dropped"),
    "cancel_requested": ("🛑", "Cancel requested"),
    "session_cancelled": ("🛑", "Cancelled"),
    "budget_exhausted": ("⛔", "Budget exhausted"),
    "agent_error": ("❌", "Agent error"),
    "final_composed": ("🏁", "Final answer composed"),
    "conversation_continued": ("💬", "You responded — continuing"),
    "session_resumed": ("▶️", "Resumed"),
    "status_change": ("•", "Status"),
    "workspace_emptied": ("🗑️", "Workspace emptied"),
}


def _detail(event: str, p: dict) -> str:
    """A short human detail for an event, pulled from its payload."""
    if not isinstance(p, dict):
        return ""
    if event == "panel_artifact_rejected":
        return f"{p.get('agent','')}/{p.get('file','')}: {p.get('reason','')}"[:110]
    if event == "panel_seat_preflight_failed":
        return f"{p.get('agent','')}: {p.get('error','')}"[:110]
    if event == "integration_decided":
        decision = str(p.get("decision", "unavailable"))
        return {"adopted": "used the integrated candidate",
                "kept_winner": "kept the voted winner"}.get(decision, decision)
    if event == "classified":
        return " · ".join(x for x in (p.get("task_type"), p.get("complexity"), f"risk {p.get('risk')}" if p.get("risk") else "") if x)
    if event == "round_start":
        return str(p.get("goal", ""))[:80]
    if event in ("agent_call_queued", "agent_call_started"):
        timeout = p.get("timeout_s")
        limit = "no coordinator deadline" if timeout == 0 else f"limit {timeout or '?'}s"
        return f"{p.get('agent','')} / {p.get('role','')} / {limit}"
    if event == "agent_call_finished":
        elapsed = int(p.get("elapsed_ms") or p.get("duration_ms") or 0)
        return f"{p.get('agent','')} / {elapsed / 1000:.1f}s"
    if event == "agent_call_failed":
        elapsed = int(p.get("duration_ms") or 0)
        duration = f" / {elapsed / 1000:.1f}s" if elapsed else ""
        return f"{p.get('agent','')}{duration}: {str(p.get('error',''))[:70]}"
    if event == "package_output_reassigned":
        return (
            f"{p.get('file','')}: {p.get('from','')} → {p.get('to','')}"
        )[:110]
    if event == "runtime_deferred":
        return f"{p.get('file','')} / waiting for integration"
    if event == "contribution":
        return f"{p.get('role','')} · {p.get('agent','')}" + (f" ({p.get('chars')} chars)" if p.get("chars") else "")
    if event in ("skill_requested", "skill_resolved", "skill_failed"):
        return " ".join(str(x) for x in (p.get("skill", ""), p.get("arg", "")) if x)[:80]
    if event == "disagreement_ruled":
        return (f"{p.get('topic','')[:60]} → {p.get('ruling','')[:40]}").strip(" →")
    if event in ("action_proposed", "action_executed", "action_denied", "action_failed"):
        return " ".join(str(x) for x in (p.get("kind", ""), p.get("filename", "")) if x)[:80]
    if event == "approval_requested":
        return str(p.get("action", ""))[:80]
    if event == "approval_resolved":
        return f"{p.get('action','')[:50]} → {p.get('status','')}".strip(" →")
    if event in ("input_requested", "input_answered"):
        return str(p.get("question", p.get("answer", "")))[:80]
    if event == "seat_dropped":
        return f"{p.get('role','')} · {p.get('agent','')}: {str(p.get('error',''))[:60]}"
    if event == "round_synthesized":
        return f"round {p.get('round','')}: {p.get('decision','')}" + (
            f" — {str(p.get('why',''))[:60]}" if p.get("why") else "")
    if event == "panel_seat_dropped":
        return f"{p.get('agent','')}: {str(p.get('error',''))[:60]}"
    if event == "refine_round":
        return f"iteration {p.get('iteration','')}"
    if event == "converged":
        return f"after {p.get('iterations','')} round(s)"
    if event in ("delegation_requested", "delegation_granted", "delegation_resolved",
                 "delegation_denied", "delegation_failed", "delegation_retry",
                 "delegation_reseated"):
        head = str(p.get("role") or p.get("to") or "")
        if p.get("from"):
            head = f"{head}: {p['from']} → {p.get('to', '?')}"
        elif p.get("agent"):
            head += f" ← {p['agent']}"  # which model fills the talent seat
        if p.get("kind"):
            head += f" ({p['kind']})"
        tail = str(p.get("reason") or p.get("error") or "")
        return (f"{head}: {tail}" if tail else head)[:110]
    if event in ("artifact_continuation", "artifact_continued", "artifact_continuation_failed"):
        return str(p.get("file", ""))[:60]
    if event == "candidates_collected":
        return f"{p.get('n','')} candidates for {p.get('base','')}".strip()
    if event == "candidate_scored":
        return f"{p.get('judge','')} → winner Candidate {p.get('winner','?')}"
    if event == "candidate_rejected_runtime":
        return f"{p.get('agent','')}/{p.get('file','')}: {p.get('detail','')}"[:110]
    if event in ("runtime_ok",):
        return str(p.get("file", ""))
    if event == "winner_selected":
        score = p.get("score")
        how = f"score {score}" if score is not None else "sole runner"
        return (f"{p.get('agent','')}'s {p.get('file','')} — {how}"
                + (f", {p['chair']}" if p.get("chair") else ""))[:110]
    if event == "chair_overrode":
        return f"→ Candidate {p.get('to','')}: {p.get('reason','')}"[:110]
    if event == "chair_recovered":
        return f"{p.get('agent','')} {p.get('file','')} ({p.get('edits','')} edits)"
    if event == "winner_fixes_applied":
        return f"{p.get('file','')}: {p.get('applied','')} fix(es)"
    if event == "status_change":
        return f"{p.get('from','')} → {p.get('to','')}".strip(" →")
    if event in ("budget_exhausted", "agent_error"):
        return str(p.get("detail", ""))[:80]
    if event == "context_overview":
        return f"{p.get('chars','')} chars" if p.get("chars") else ""
    return ""


def format_timeline(events: list[dict]) -> list[dict]:
    """Map raw {ts,event,payload} log records to {ts,icon,label,detail} rows."""
    out: list[dict] = []
    for ev in events:
        name = ev.get("event", "")
        icon, label = _TIMELINE.get(name, ("•", name.replace("_", " ").title()))
        out.append({
            "ts": ev.get("ts", ""),
            "event": name,
            "icon": icon,
            "label": label,
            "detail": _detail(name, ev.get("payload") or {}),
        })
    return out
