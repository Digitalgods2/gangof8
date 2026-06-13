"""Configuration: budgets, role→agent assignments, approval categories."""

from __future__ import annotations

import os
from pathlib import Path

from .models import Budgets, Complexity, Risk, Role

DATA_DIR = Path(os.environ.get("CONCLAVE_OS_DATA", str(Path(__file__).resolve().parent.parent / "data")))

BUDGETS_BY_COMPLEXITY: dict[Complexity, Budgets] = {
    Complexity.trivial: Budgets(max_rounds=1, max_turns_per_round=1, max_agent_calls=4, max_wall_seconds=180),
    Complexity.standard: Budgets(max_rounds=3, max_turns_per_round=2, max_agent_calls=12, max_wall_seconds=600),
    Complexity.complex: Budgets(max_rounds=4, max_turns_per_round=2, max_agent_calls=16, max_wall_seconds=900),
}


def budgets_for(complexity: Complexity) -> Budgets:
    return BUDGETS_BY_COMPLEXITY[complexity].model_copy()


# Backend selection: "mock" (offline, default) or "switchboard"
# (Conclave AI at 127.0.0.1:8787 driving the real codex/gemini/claude-code CLIs).
BACKEND = os.environ.get("CONCLAVE_OS_BACKEND", "mock")
SWITCHBOARD_URL = os.environ.get("CONCLAVE_OS_SWITCHBOARD_URL", "http://127.0.0.1:8787")

ROLE_AGENTS_MOCK: dict[Role, str] = {
    Role.researcher: "mock",
    Role.architect: "mock",
    Role.critic: "mock",
    Role.implementer: "mock",
    Role.summarizer: "mock",
}

# Switchboard agent ids are its registry names: "codex", "gemini", "claude-code".
ROLE_AGENTS_SWITCHBOARD: dict[Role, str] = {
    Role.researcher: "gemini",
    Role.architect: "claude-code",
    Role.critic: "codex",
    Role.implementer: "claude-code",
    Role.summarizer: "claude-code",
}

ROLE_AGENTS_BY_BACKEND: dict[str, dict[Role, str]] = {
    "mock": ROLE_AGENTS_MOCK,
    "switchboard": ROLE_AGENTS_SWITCHBOARD,
}

# Default mapping for the configured backend (modules that need a specific
# session's mapping receive it explicitly instead of reading this).
ROLE_AGENTS = ROLE_AGENTS_BY_BACKEND.get(BACKEND, ROLE_AGENTS_MOCK)

# Any classified risk ABOVE this boundary forces a human approval gate
# before round 1 (loop step 7 / DESIGN hard rules).
RISK_BOUNDARY = Risk.low

APPROVAL_CATEGORIES = [
    "file_write",
    "file_delete",
    "code_exec",
    "send_message",
    "spend",
    "settings",
    "external",
]

# The ONLY capability that never needs approval (default-deny everything else).
ALWAYS_ALLOWED_CAPABILITIES = {"generate_text"}

# Deliberation may not spend the last N agent calls — they are reserved so the
# composer (and its one retry) can always run. Learned from the first real run,
# where 8 critic tests starved the composer's retry.
COMPOSER_RESERVED_CALLS = 2

# A real critic can raise many conflicts per round; test the first N with the
# critic and rule the rest on constraints, instead of burning the budget.
MAX_CRITIC_TESTS_PER_ROUND = 3

# Protocol-wrapped backends often return the final answer as plain prose, not
# the labeled format. Substantial prose (>= this many chars) is accepted as
# the answer at medium confidence; shorter unparseable output gets one retry.
COMPOSER_PROSE_MIN_CHARS = 200
