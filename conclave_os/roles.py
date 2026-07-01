"""Council assembly for the lead-driven model.

There is no court: one LEAD drives every task and pulls in specialist talents on
demand (see loop.py). build_council therefore activates only coordinator + lead +
summarizer (+ governance when the task needs it); the specialist roles are listed
but inactive so the UI roster still shows what the lead can reach for, and so a
delegation can flip one active. plan_rounds returns a single nominal round purely
so the timeline / live-status / budgets have something to anchor to.
"""

from __future__ import annotations

from .models import Budgets, Classification, Council, CouncilMember, Role, RoundSpec

# Roles the lead may delegate to — the "talents". Listed inactive in the council
# until the lead actually pulls one in.
SPECIALIST_ROLES = (
    Role.knowledge_retriever,
    Role.researcher,
    Role.architect,
    Role.code_generator,
    Role.api_integrator,
    Role.critic,
    Role.red_team,
    Role.fact_validator,
)


def build_council(cls: Classification, role_agents: dict[Role, str] | None = None) -> Council:
    """Coordinator + lead + summarizer active; specialist talents listed but
    inactive (available for on-demand delegation); governance active only when
    the task needs it. No fixed multi-seat court."""
    from . import config

    mapping = role_agents or config.ROLE_AGENTS

    def agent_for(role: Role) -> str:
        return mapping.get(role, "mock")

    members = [
        CouncilMember(role=Role.coordinator, agent="system", active=True),
        CouncilMember(role=Role.lead, agent=agent_for(Role.lead), active=True),
    ]
    members += [
        CouncilMember(role=role, agent=agent_for(role), active=False)
        for role in SPECIALIST_ROLES
    ]
    # implementer is kept listed (UI/back-compat) but the LEAD authors files now.
    members.append(CouncilMember(role=Role.implementer, agent=agent_for(Role.implementer), active=False))
    members.append(CouncilMember(role=Role.governance, agent="system", active=cls.needs_governance))
    members.append(CouncilMember(role=Role.summarizer, agent=agent_for(Role.summarizer), active=True))
    return Council(members=members)


def plan_rounds(cls: Classification, council: Council, budgets: Budgets) -> list[RoundSpec]:
    """A single nominal round: the lead drives the task. Kept so the timeline,
    live-status, and budget accounting have a round to anchor to — the real work
    and any delegation happen inside the lead flow, not across planned rounds."""
    return [
        RoundSpec(
            round=0,
            goal="lead drives the task (delegating to talents only if needed)",
            agents=[Role.lead],
            max_turns=1,
            stop_condition="lead produced a result; actions executed and verified",
            output_requirement="the actual answer or complete ARTIFACT/PROMOTE files",
        )
    ]
