"""Final Response Composer — builds the FinalAnswer (loop step 10).

Asks the summarizer for a JSON FinalAnswer; degrades gracefully to a
low-confidence partial answer if the budget is exhausted or the JSON is bad.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from . import config
from .governance import BudgetExceeded
from .models import Contribution, Council, CouncilMember, FinalAnswer, Role, Session
from .registry import AgentError

AgentCall = Callable[[CouncilMember, str], Contribution]

VALID_CONFIDENCE = {"high", "medium", "low"}


def _actions_summary(session: Session) -> str:
    """What the coordinator actually DID — the authoritative outcome of every
    governed action, so the summarizer reports facts instead of guessing (or
    confabulating filesystem checks it can't run)."""
    lines = []
    for a in session.proposed_actions:
        if a.kind in ("write_file", "edit_file"):
            if a.status == "executed":
                lines.append(f"- {a.kind} '{a.filename}': APPLIED (written to {a.result_path})")
            elif a.status == "denied":
                lines.append(f"- {a.kind} '{a.filename}': DENIED by the human (not written)")
            elif a.status == "failed":
                lines.append(f"- {a.kind} '{a.filename}': FAILED — {a.error}")
            else:
                lines.append(f"- {a.kind} '{a.filename}': {a.status}")
        elif a.kind == "run_tests":
            out = (a.result_path or "").strip()
            lines.append(f"- run_tests ({a.status}): {out[:600]}" if out
                         else f"- run_tests: {a.status}")
        elif a.kind in ("read_file", "search_project") and a.status == "executed":
            lines.append(f"- {a.kind} '{a.filename or (a.args or {}).get('query','')}': ran")
    return "\n".join(lines)


# A contribution may BE a file artifact (the lead emits whole files as
# 'ARTIFACT: <name>\n<body>'). Feeding that raw body into the summarizer makes it
# CONTINUE the file instead of summarizing it (observed: a 12KB index.html turned
# the "answer" into a CSS dump). Condense any artifact-bearing contribution to its
# preamble + the list of files it produced — the bodies are already on disk and
# reported authoritatively under "Actions performed".
_ARTIFACT_LINE = re.compile(r"^[ \t]*(?:\*\*)?ARTIFACT(?:\*\*)?[ \t]*:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
# Fenced code block or a raw HTML document — collapse to a placeholder so the
# summarizer never receives a raw file body to continue (materialize/continuation
# contributions are pure bodies with no ARTIFACT: line).
_FENCED = re.compile(r"```.*?```", re.DOTALL)
_HTML_DOC = re.compile(r"<!doctype html.*?</html>|<html[ >].*?</html>", re.DOTALL | re.IGNORECASE)


def _condense_for_compose(content: str) -> str:
    matches = list(_ARTIFACT_LINE.finditer(content))
    if matches:
        names = ", ".join(m.group(1).strip() for m in matches)
        preamble = content[: matches[0].start()].strip()
        preamble = (preamble[:400] + " …") if len(preamble) > 400 else preamble
        note = f"[emitted {len(matches)} file artifact(s): {names} — full contents written to disk, not shown here]"
        return f"{preamble}\n{note}" if preamble else note
    # no ARTIFACT line, but the body may still be raw file content (a fenced code
    # block or a whole HTML document) — collapse those so they can't be continued.
    collapsed = _HTML_DOC.sub("[file content omitted]", content)
    collapsed = _FENCED.sub("[code omitted]", collapsed)
    return collapsed[: config.COMPOSER_CONTEXT_CHARS]


def compose_prompt(session: Session) -> str:
    recent = "\n\n".join(
        f"[{c.role.value} r{c.round}] {_condense_for_compose(c.content)}"
        for c in session.contributions[-config.COMPOSER_CONTEXT_CONTRIBUTIONS:]
    )
    actions = _actions_summary(session)
    # Plain-text labeled sections, NOT JSON: asking an agent for a JSON object
    # whose keys overlap a wrapping protocol can contaminate its output
    # (learned from real runs). Text labels survive — like DISAGREEMENT:/VERDICT:.
    grounding = (
        "This task concerns the real codebase at "
        f"{session.established_root}. Keep the final answer GROUNDED in specifics "
        "the council surfaced (files, functions, features). Do NOT pad it with "
        "generic advice ('add documentation', 'add tests', 'improve observability', "
        "'audit privacy') unless the council actually found that gap — prefer "
        "concrete, app-specific points a developer could act on.\n"
        if session.established_root else ""
    )
    return (
        f"Task: {session.task.text}\n"
        "Your role: summarizer. Synthesize the final response from the "
        "deliberation below — the council has already done the work. Do NOT ask "
        "the user any questions and do NOT request more information; if anything "
        "is still uncertain, record it under ASSUMPTIONS or RISKS and give your "
        "best answer anyway.\n"
        f"{grounding}"
        "The 'Actions performed' list is the AUTHORITATIVE record of what the "
        "coordinator did. Trust it as fact: if an action shows APPLIED, that file "
        "WAS written — report it as done with high confidence. You have NO "
        "filesystem access, so never claim a file is missing/unconfirmed and "
        "never claim that a file was written, edited, or delivered unless the "
        "Actions performed list records the corresponding action as APPLIED; "
        "never say you ran Glob/find/ls (you cannot) — rely on this record.\n"
        "Do NOT reproduce, paste, or continue any file contents — the files are "
        "already written. DESCRIBE what was built and how to use it (e.g. open the "
        "file in a browser), in a few sentences.\n"
        "Use EXACTLY these labeled plain-text sections (no JSON):\n"
        "ANSWER: <the final answer; may span multiple lines>\n"
        "CONFIDENCE: <high, medium, or low>\n"
        "ASSUMPTIONS:\n- <one per line, or '- none'>\n"
        "RISKS:\n- <unresolved risks, one per line, or '- none'>\n"
        "NEXT_ACTION: <one line, or 'none'>\n"
        f"Actions performed (authoritative):\n{actions or '(none)'}\n"
        "In ANSWER, separate established facts from proposals when the distinction "
        "matters. Do not promote an assumption to fact.\n"
        f"Deliberation:\n{recent}"
    )


_FINAL_MARKERS = re.compile(r"<final>(.*?)</final>", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Optional[dict]:
    # Prefer the explicit <final>...</final> markers; fall back to the
    # outermost brace span (handles raw JSON and fenced JSON).
    marked = _FINAL_MARKERS.search(text)
    candidates = [marked.group(1)] if marked else []
    candidates.append(text)
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def fallback_final(session: Session, note: str) -> FinalAnswer:
    """Deterministic low-confidence partial answer built from what the
    session has — used whenever the summarizer can't produce a clean one."""
    last = session.contributions[-1].content if session.contributions else "(no contributions)"
    return FinalAnswer(
        answer=f"Partial result ({note}): {last[:800]}",
        confidence="low",
        assumptions=[],
        risks_unresolved=list(session.unresolved),
    )


# Tolerates markdown bold in both styles: **ANSWER**: and **ANSWER:**
_SECTION_LABELS = re.compile(
    r"^\s*(?:\*\*)?(ANSWER|CONFIDENCE|ASSUMPTIONS|RISKS|NEXT_ACTION)(?:\*\*)?\s*:\s*(?:\*\*)?\s*",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_sections(text: str) -> Optional[dict]:
    """Parse 'ANSWER: ... CONFIDENCE: ...' labeled sections into a dict."""
    matches = list(_SECTION_LABELS.finditer(text))
    if not matches:
        return None
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.setdefault(m.group(1).upper(), text[m.end():end].strip())
    if not sections.get("ANSWER"):
        return None

    def bullets(label: str) -> list[str]:
        items = [
            line.strip().lstrip("-*• ").strip()
            for line in sections.get(label, "").splitlines()
            if line.strip()
        ]
        return [x for x in items if x and x.lower() not in ("none", "n/a")]

    confidence_match = re.search(r"\b(high|medium|low)\b", sections.get("CONFIDENCE", ""), re.I)
    next_action = sections.get("NEXT_ACTION", "").strip()
    return {
        "answer": sections["ANSWER"],
        "confidence": confidence_match.group(1).lower() if confidence_match else "low",
        "assumptions": bullets("ASSUMPTIONS"),
        "risks_unresolved": bullets("RISKS"),
        "next_action": None if next_action.lower() in ("", "none", "null", "n/a") else next_action,
    }


def parse_final(session: Session, content: str) -> Optional[FinalAnswer]:
    """Parse summarizer output into a FinalAnswer; None if unusable.
    JSON (markers, raw, or fenced) is preferred; labeled plain-text sections
    are the contract real protocol-wrapped agents actually honor."""
    data = _extract_json(content)
    if data is None or not str(data.get("answer", "")).strip():
        data = _parse_sections(content)
    if data is None or not str(data.get("answer", "")).strip():
        return None
    confidence = str(data.get("confidence", "low")).lower()
    next_action = data.get("next_action")
    next_action = str(next_action).strip() if next_action else None
    final = FinalAnswer(
        answer=str(data["answer"]).strip(),
        confidence=confidence if confidence in VALID_CONFIDENCE else "low",
        assumptions=[str(a) for a in data.get("assumptions") or []],
        risks_unresolved=[str(r) for r in data.get("risks_unresolved") or []],
        next_action=next_action or None,
    )
    final.risks_unresolved.extend(u for u in session.unresolved if u not in final.risks_unresolved)
    return final


def _fallback_agents(session: Session, failed_agent: str) -> list[str]:
    """Reliable agents to compose with instead of a failed summarizer: agents
    that already produced real contributions THIS run (so they're registered and
    working), most-used first, excluding the one that just failed."""
    from collections import Counter

    counts = Counter(
        c.agent for c in session.contributions
        if c.agent and c.agent not in ("system", "user")
        and c.agent != failed_agent and c.content.strip()
    )
    return [a for a, _ in counts.most_common()]


def _summarize(session: Session, call: AgentCall, prompt: str,
               member: CouncilMember) -> tuple[Optional[Contribution], CouncilMember]:
    """Call the summarizer; if its agent ERRORS (e.g. a gemini timeout), retry
    with a reliable fallback agent that already worked this run — so one flaky
    seat can't collapse the final answer. Returns (contribution|None, member_used).
    AgentInputRequired propagates so the loop can pause for the human."""
    try:
        return call(member, prompt), member
    except BudgetExceeded:
        session.unresolved.append("composer skipped: budget exhausted")
        return None, member
    except AgentError as e:
        for alt in _fallback_agents(session, member.agent):
            sub = CouncilMember(role=Role.summarizer, agent=alt, active=True)
            try:
                contribution = call(sub, prompt)
            except (AgentError, BudgetExceeded):
                continue
            session.unresolved.append(
                f"summarizer '{member.agent}' failed ({e}); recomposed with '{alt}'")
            return contribution, sub
        session.unresolved.append(f"composer skipped: agent error: {e}")
        return None, member


def compose(session: Session, council: Council, call: AgentCall) -> FinalAnswer:
    """Compose the final answer via the summarizer, falling back to a working
    agent if it errors, plus at most one stricter retry on unparseable output.
    AgentInputRequired propagates — the loop pauses the session for the human."""
    member = council.get(Role.summarizer)
    if member is None or not member.active:
        return fallback_final(session, "no summarizer in council")

    contribution, member = _summarize(session, call, compose_prompt(session), member)
    if contribution is None:
        return fallback_final(session, "agent error")

    final = parse_final(session, contribution.content) or _accept_prose(session, contribution.content)
    if final:
        return final

    retry, member = _summarize(
        session, call,
        compose_prompt(session)
        + "\n\nIMPORTANT: your previous reply could not be parsed. Use the labeled "
        "sections EXACTLY as specified, starting each label at the beginning of a "
        "line: ANSWER:, CONFIDENCE:, ASSUMPTIONS:, RISKS:, NEXT_ACTION:.",
        member)
    if retry is None:
        return fallback_final(session, "unparseable output, retry unavailable")
    return (
        parse_final(session, retry.content)
        or _accept_prose(session, retry.content)
        or fallback_final(session, "summarizer returned unparseable output twice")
    )


def _accept_prose(session: Session, content: str) -> Optional[FinalAnswer]:
    """Substantial unlabeled prose IS the answer — protocol-wrapped agents
    often can't honor the labeled format. Medium confidence reflects that the
    structure (assumptions, explicit confidence) had to be inferred."""
    text = content.strip()
    if len(text) < config.COMPOSER_PROSE_MIN_CHARS:
        return None
    return FinalAnswer(
        answer=text,
        confidence="medium",
        assumptions=[],
        risks_unresolved=list(session.unresolved),
    )
