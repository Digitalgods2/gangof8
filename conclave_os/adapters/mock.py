"""MockAdapter — deterministic canned responses for offline testing.

Responses are keyed on the role and on marker words the round planner puts in
each prompt's objective ("gather", "challenge", "reconcile", "test both
sides", "review", "compose", "draft"). No randomness, no network, no cost.
"""

from __future__ import annotations

from ..models import Role
from ..registry import AdapterResult

FACTS = (
    "- SQLite offers atomic transactions and crash safety, even with concurrent writers.\n"
    "- Plain JSON files are human-readable and need no driver or schema.\n"
    "- Query and filtering needs (by status, by date) grow over time; SQLite handles them natively.\n"
    "- (uncertainty: expected write volume is assumed to be low)"
)

CHALLENGE = (
    "DISAGREEMENT: storage backend — the researcher favors SQLite, but for a "
    "single-user local service plain JSON files are simpler, diff-able, and "
    "carry no binary-format lock-in."
)

VERDICT = (
    "VERDICT: uphold the original position. Evidence: session logs are "
    "append-heavy and queried by status; SQLite's transactions and indexed "
    "queries outweigh JSON simplicity, and the JSON-files position "
    "underestimates partial-write corruption risk."
)

RECONCILE = (
    "Reconciliation: keep SQLite as the primary store and mirror a "
    "human-readable JSONL trail per session — this captures the critic's "
    "readability concern without giving up transactional safety."
)

REVIEW = "Review: minor wording issues only; the draft is acceptable."

DRAFT = "DRAFT: Use SQLite as the primary store with a per-session JSONL mirror for readability."

DESIGN = "Design outline: one storage interface; SQLite implementation behind it; JSONL event mirror."

FINAL_JSON = (
    '{"answer": "Use SQLite for session logs, with a per-session JSONL mirror for human '
    'readability. SQLite gives atomic transactions, crash safety, and native querying; '
    'the JSONL mirror preserves the diff-ability that plain JSON files would have offered.", '
    '"confidence": "high", '
    '"assumptions": ["single local user", "modest write volume"], '
    '"risks_unresolved": ["JSONL mirror and DB could drift if writes are not paired"], '
    '"next_action": null}'
)


class MockAdapter:
    name = "mock"

    def call(self, role: Role, prompt: str, timeout_s: int) -> AdapterResult:
        lower = prompt.lower()
        if role == Role.summarizer:
            content = FINAL_JSON
        elif role == Role.critic:
            if "test both sides" in lower:
                content = VERDICT
            elif "review" in lower:
                content = REVIEW
            else:
                content = CHALLENGE
        elif role == Role.researcher:
            content = RECONCILE if "reconcile" in lower else FACTS
        elif role == Role.architect:
            content = DESIGN
        elif role == Role.implementer:
            content = DRAFT
        else:
            content = "(no contribution for this role)"
        return AdapterResult(content=content, duration_ms=1)

    def resume(self, resume_token: str, answer: str, timeout_s: int) -> AdapterResult:
        return AdapterResult(
            content=f"- Updated contribution incorporating the user's answer: {answer}",
            duration_ms=1,
        )
