"""Council assembly for the panel-round model.

One LEAD drives every task and pulls in specialist talents on demand (see
loop.py), while the PANEL — every enabled seat, one per origin model —
contributes an independent take each round before the lead synthesizes.
build_council activates coordinator + lead + panelists + summarizer
(+ governance when the task needs it); the specialist roles are listed but
inactive so the UI roster still shows what the lead can reach for, and so a
delegation can flip one active. RoundSpecs are appended per executed round by
the loop itself — there is no up-front round plan.
"""

from __future__ import annotations

from .models import Classification, Council, CouncilMember, Role

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


def build_council(
    cls: Classification,
    role_agents: dict[Role, str] | None = None,
    panel: list[str] | None = None,
) -> Council:
    """Coordinator + lead + panelists + summarizer active; specialist talents
    listed but inactive (available for on-demand delegation); governance active
    only when the task needs it."""
    from . import config

    mapping = role_agents or config.ROLE_AGENTS

    def agent_for(role: Role) -> str:
        return mapping.get(role, "mock")

    members = [
        CouncilMember(role=Role.coordinator, agent="system", active=True),
        CouncilMember(role=Role.lead, agent=agent_for(Role.lead), active=True),
    ]
    members += [
        CouncilMember(role=Role.panelist, agent=seat, active=True)
        for seat in (panel or [])
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
