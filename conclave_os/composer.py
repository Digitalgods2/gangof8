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


def compose_prompt(session: Session) -> str:
    recent = "\n\n".join(
        f"[{c.role.value} r{c.round}] {c.content[:config.COMPOSER_CONTEXT_CHARS]}"
        for c in session.contributions[-config.COMPOSER_CONTEXT_CONTRIBUTIONS:]
    )
    rulings = "\n".join(
        f"- {d.topic}: ruled '{d.ruling}' ({d.ruling_basis})" for d in session.disagreements
    ) or "(none)"
    # Plain-text labeled sections, NOT JSON: asking an agent for a JSON object
    # whose keys overlap a wrapping protocol can contaminate its output
    # (learned from real runs). Text labels survive — like DISAGREEMENT:/VERDICT:.
    return (
        f"Task: {session.task.text}\n"
        "Your role: summarizer. Synthesize the final response from the "
        "deliberation below — the council has already done the work. Do NOT ask "
        "the user any questions and do NOT request more information; if anything "
        "is still uncertain, record it under ASSUMPTIONS or RISKS and give your "
        "best answer anyway.\n"
        "Use EXACTLY these labeled plain-text sections (no JSON):\n"
        "ANSWER: <the final answer; may span multiple lines>\n"
        "CONFIDENCE: <high, medium, or low>\n"
        "ASSUMPTIONS:\n- <one per line, or '- none'>\n"
        "RISKS:\n- <unresolved risks, one per line, or '- none'>\n"
        "NEXT_ACTION: <one line, or 'none'>\n"
        f"Disagreement rulings:\n{rulings}\n"
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


def compose(session: Session, council: Council, call: AgentCall) -> FinalAnswer:
    """One summarizer call, plus at most one stricter retry on unparseable
    output (still bounded by the session's agent-call budget).
    AgentInputRequired propagates — the loop pauses the session for the human."""
    member = council.get(Role.summarizer)
    if member is None or not member.active:
        return fallback_final(session, "no summarizer in council")
    try:
        contribution = call(member, compose_prompt(session))
    except BudgetExceeded:
        session.unresolved.append("composer skipped: budget exhausted")
        return fallback_final(session, "budget exhausted")
    except AgentError as e:
        session.unresolved.append(f"composer skipped: agent error: {e}")
        return fallback_final(session, "agent error")

    final = parse_final(session, contribution.content) or _accept_prose(session, contribution.content)
    if final:
        return final

    try:
        retry = call(
            member,
            compose_prompt(session)
            + "\n\nIMPORTANT: your previous reply could not be parsed. Use the labeled "
            "sections EXACTLY as specified, starting each label at the beginning of a "
            "line: ANSWER:, CONFIDENCE:, ASSUMPTIONS:, RISKS:, NEXT_ACTION:.",
        )
    except (BudgetExceeded, AgentError) as e:
        session.unresolved.append(f"composer retry skipped: {e}")
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
