"""Human-readable reporting derived from a session and its event log:

- council_health(): which seats dropped or were substituted, so a degraded run
  says so plainly instead of burying it in 'unresolved'.
- format_timeline(): turn the raw JSONL events into a readable run timeline.
"""

from __future__ import annotations

import re

_DROP_RE = re.compile(r"(\w+) seat \(([\w.\-]+)\) dropped: (.*)", re.IGNORECASE)
_SUB_RE = re.compile(r"summarizer '([\w.\-]+)' failed.*recomposed with '([\w.\-]+)'", re.IGNORECASE)
_DEGRADE_HINTS = (
    "composer skipped", "stopped refining", "budget exhausted", "agent error",
    "could not build diff", "refinement cap", "refinement time limit",
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


# event name -> (icon, friendly label)
_TIMELINE = {
    "task_received": ("📥", "Task received"),
    "classified": ("🏷️", "Classified"),
    "council_formed": ("👥", "Council formed"),
    "round_start": ("🔄", "Round started"),
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
    "paused_for_approval": ("⏸️", "Paused for approval"),
    "input_requested": ("❓", "Question to you"),
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
    if event == "classified":
        return " · ".join(x for x in (p.get("task_type"), p.get("complexity"), f"risk {p.get('risk')}" if p.get("risk") else "") if x)
    if event == "round_start":
        return str(p.get("goal", ""))[:80]
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
    if event == "refine_round":
        return f"iteration {p.get('iteration','')}"
    if event == "converged":
        return f"after {p.get('iterations','')} round(s)"
    if event in ("delegation_requested", "delegation_granted", "delegation_resolved",
                 "delegation_denied", "delegation_failed"):
        return " ".join(str(x) for x in (p.get("to", ""), p.get("reason", "")) if x)[:80]
    if event in ("artifact_continuation", "artifact_continued", "artifact_continuation_failed"):
        return str(p.get("file", ""))[:60]
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
