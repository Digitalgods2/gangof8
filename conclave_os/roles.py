"""Role Assignment Engine — builds the council and plans bounded rounds."""

from __future__ import annotations

from . import config
from .models import Budgets, Classification, Council, CouncilMember, Role, RoundSpec


def build_council(cls: Classification, role_agents: dict[Role, str] | None = None) -> Council:
    """Coordinator always active; LLM roles strictly by need (loop step 3).
    Inactive roles stay listed so the log shows the choice explicitly."""
    mapping = role_agents or config.ROLE_AGENTS

    def agent_for(role: Role) -> str:
        return mapping.get(role, "mock")

    members = [
        CouncilMember(role=Role.coordinator, agent="system", active=True),
        CouncilMember(role=Role.researcher, agent=agent_for(Role.researcher), active=cls.needs_facts),
        CouncilMember(role=Role.architect, agent=agent_for(Role.architect), active=cls.needs_design),
        CouncilMember(role=Role.critic, agent=agent_for(Role.critic), active=cls.quality_matters),
        CouncilMember(role=Role.implementer, agent=agent_for(Role.implementer), active=cls.produces_output),
        CouncilMember(role=Role.governance, agent="system", active=cls.needs_governance),
        CouncilMember(role=Role.summarizer, agent=agent_for(Role.summarizer), active=True),
    ]
    return Council(members=members)


def plan_rounds(cls: Classification, council: Council, budgets: Budgets) -> list[RoundSpec]:
    """Deterministic round plan (loop step 4). Every round declares its
    objective, speakers, turn cap, stop condition, and output format BEFORE
    it runs. Hard-capped at budgets.max_rounds."""
    specs: list[RoundSpec] = []

    def add(goal: str, role: Role, requirement: str) -> None:
        specs.append(
            RoundSpec(
                round=len(specs),
                goal=goal,
                agents=[role],
                max_turns=budgets.max_turns_per_round,
                stop_condition="all assigned agents returned, or timeout 120s",
                output_requirement=requirement,
            )
        )

    if council.is_active(Role.researcher):
        add("gather the relevant facts and constraints", Role.researcher,
            "bullet list of facts with uncertainty noted")
    if council.is_active(Role.architect):
        add("design the solution approach", Role.architect, "short design outline")
    if council.is_active(Role.critic):
        add("challenge the strongest claims so far; surface any disagreement", Role.critic,
            "at most 3 lines starting 'DISAGREEMENT:', covering only the most material "
            "conflicts (not minor caveats), or the single word PASS if there are none")
    if council.is_active(Role.researcher) and council.is_active(Role.critic):
        add("reconcile the challenge with the established facts", Role.researcher,
            "short reconciliation note")

    return specs[: budgets.max_rounds]
