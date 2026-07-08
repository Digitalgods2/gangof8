"""Truth ledger extraction for R&D deliberations.

The ledger is not a fact database that blindly trusts model output. It is a
conservative audit layer over the transcript: sourced claims can become
established, unsupported claims stay assumptions, validator refutations create
disputes, and everything remains traceable to the asserting seat.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import Contribution, Role, Session, TruthClaim, utcnow


_CLAIM_RE = re.compile(
    r"(?:^|\n)\s*(?:[-*]\s*)?(?:claim|fact)\s*:\s*(?P<claim>.+?)(?=\n\s*(?:[-*]\s*)?(?:claim|fact)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_SOURCE_RE = re.compile(
    r"\bsource\s*:\s*(?P<source>.+?)(?=(?:\.\s+confidence|\n|$))",
    re.IGNORECASE | re.DOTALL,
)
_CONFIDENCE_RE = re.compile(r"\bconfidence\s*:\s*(?P<confidence>0(?:\.\d+)?|1(?:\.0+)?)", re.IGNORECASE)
_VALIDATION_RE = re.compile(
    r"(?:^|\n)\s*(?:[-*]\s*)?(?P<status>CONFIRMED|REFUTED|UNVERIFIABLE)\s*:\s*(?P<claim>.+?)(?=\n\s*(?:[-*]\s*)?(?:CONFIRMED|REFUTED|UNVERIFIABLE)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_VALIDATOR_ROLES = {Role.fact_validator, Role.critic, Role.red_team}
_ASSERTING_ROLES = {
    Role.knowledge_retriever,
    Role.researcher,
    Role.architect,
    Role.api_integrator,
    Role.code_generator,
    Role.implementer,
}


def _clean(text: str) -> str:
    return " ".join((text or "").strip().split())


def _claim_key(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9 ]+", "", _clean(text).lower())
    return " ".join(lowered.split())[:180]


def _similar(a: str, b: str) -> bool:
    ka, kb = _claim_key(a), _claim_key(b)
    return bool(ka and kb) and (ka in kb or kb in ka or SequenceMatcher(None, ka, kb).ratio() >= 0.72)


def _source_from(block: str) -> str | None:
    m = _SOURCE_RE.search(block)
    if not m:
        return None
    source = _clean(m.group("source")).rstrip(".")
    if not source or source.lower().startswith("no source found"):
        return None
    return source[:500]


def _confidence_from(block: str, has_source: bool) -> float:
    m = _CONFIDENCE_RE.search(block)
    if m:
        try:
            return max(0.0, min(1.0, float(m.group("confidence"))))
        except ValueError:
            pass
    return 0.75 if has_source else 0.35


def _claim_text_from(block: str) -> str:
    text = _clean(block)
    text = re.split(r"\bsource\s*:", text, flags=re.IGNORECASE)[0]
    text = re.split(r"\bconfidence\s*:", text, flags=re.IGNORECASE)[0]
    return text.strip(" -.")


def _add_or_merge(claims: list[TruthClaim], claim: TruthClaim) -> None:
    for existing in claims:
        if _similar(existing.claim, claim.claim):
            if not existing.source and claim.source:
                existing.source = claim.source
            existing.confidence = max(existing.confidence, claim.confidence)
            if claim.verified_by:
                existing.verified_by.extend(x for x in claim.verified_by if x not in existing.verified_by)
            if claim.refuted_by:
                existing.refuted_by.extend(x for x in claim.refuted_by if x not in existing.refuted_by)
            existing.checked_at = utcnow()
            _resolve_status(existing)
            return
    _resolve_status(claim)
    claims.append(claim)


def _resolve_status(claim: TruthClaim) -> None:
    if claim.refuted_by:
        claim.status = "disputed"
    elif claim.source and claim.verified_by:
        claim.status = "established"
    elif claim.source and claim.confidence >= 0.8:
        claim.status = "established"
    else:
        claim.status = "assumption"


def _extract_claims(contribution: Contribution) -> list[TruthClaim]:
    if contribution.role not in _ASSERTING_ROLES:
        return []
    out: list[TruthClaim] = []
    for m in _CLAIM_RE.finditer(contribution.content):
        block = m.group("claim")
        claim_text = _claim_text_from(block)
        if not claim_text:
            continue
        source = _source_from(block)
        out.append(TruthClaim(
            claim=claim_text[:1000],
            source=source,
            confidence=_confidence_from(block, bool(source)),
            asserted_by=contribution.role,
            asserted_agent=contribution.agent,
            asserted_round=contribution.round,
        ))
    return out


def _apply_validation(claims: list[TruthClaim], contribution: Contribution) -> None:
    if contribution.role not in _VALIDATOR_ROLES:
        return
    who = f"{contribution.role.value}:{contribution.agent}:r{contribution.round}"
    for m in _VALIDATION_RE.finditer(contribution.content):
        status = m.group("status").upper()
        claim_text = _claim_text_from(m.group("claim"))
        if not claim_text:
            continue
        match = next((c for c in claims if _similar(c.claim, claim_text)), None)
        if match is None:
            match = TruthClaim(
                claim=claim_text[:1000],
                asserted_by=contribution.role,
                asserted_agent=contribution.agent,
                asserted_round=contribution.round,
                confidence=0.45,
            )
            claims.append(match)
        if status == "CONFIRMED" and who not in match.verified_by:
            match.verified_by.append(who)
        elif status == "REFUTED" and who not in match.refuted_by:
            match.refuted_by.append(who)
        _resolve_status(match)


def build_truth_ledger(session: Session) -> list[TruthClaim]:
    claims: list[TruthClaim] = []
    for contribution in session.contributions:
        for claim in _extract_claims(contribution):
            _add_or_merge(claims, claim)
        _apply_validation(claims, contribution)
    claims.sort(key=lambda c: (c.status != "established", c.asserted_round, c.claim.lower()))
    session.truth_claims = claims
    return claims


def ledger_prompt(session: Session, limit: int = 12) -> str:
    if not session.truth_claims:
        return "(none)"
    lines = []
    for c in session.truth_claims[:limit]:
        source = c.source or "NO SOURCE - assumption"
        checks = []
        if c.verified_by:
            checks.append("verified by " + ", ".join(c.verified_by[:2]))
        if c.refuted_by:
            checks.append("refuted by " + ", ".join(c.refuted_by[:2]))
        suffix = f" ({'; '.join(checks)})" if checks else ""
        lines.append(f"- [{c.status}] {c.claim} | source: {source} | confidence: {c.confidence:.2f}{suffix}")
    return "\n".join(lines)
