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


# The talents that AUTHOR the deliverable itself. Delegation only buys a second
# model's take when the author is not the lead, so these are the roles kept off
# the lead's seat whenever inheritance has a choice.
AUTHORING_ROLES = (
    Role.code_generator,
    Role.implementer,
)


def separate_authoring_from_lead(mapping: dict, pool, movable) -> dict:
    """Keep INHERITED authoring roles off the lead's own seat.

    Disabling a seat moves its roles onto the remaining roster round-robin
    (``service._apply_seat_disables``). That split is even, but evenness is not
    the property that matters here. With claude and codex switched off, a real
    run put lead AND code_generator on the one surviving CLI seat: the lead —
    correctly told to delegate rather than do the work itself — dutifully
    delegated the authoring back to its own model. Seven calls, one model,
    while an enabled OpenRouter seat holding implementer never ran.

    So when an authoring role INHERITS onto the lead's seat and another enabled
    seat exists, hand it to one of the others instead. Only roles this
    inheritance actually moved are eligible: a role the user pinned to an
    ENABLED seat is their explicit choice and is never overridden.

    Pass the roster in takeover order (``service._enabled_role_fallbacks``).
    """
    lead_seat = mapping.get(Role.lead)
    alternatives = [s for s in (pool or []) if s and s != lead_seat]
    if not lead_seat or not alternatives:
        return mapping
    out = dict(mapping)
    i = 0
    for role in AUTHORING_ROLES:
        if role in movable and out.get(role) == lead_seat:
            out[role] = alternatives[i % len(alternatives)]
            i += 1
    return out


def resolve_frontier_authors(roster) -> list[str]:
    """Which seats count as FRONTIER-CLASS for a given enabled roster.

    ``config.FRONTIER_AUTHOR_SEATS`` names a preference (claude, codex by
    default), not a hard membership test. When none of those seats is available
    the role must PASS to the enabled roster, exactly as role assignment already
    passes a disabled seat's roles along (``service._apply_seat_disables``).

    Without this the two disagreed in a way that quietly removed capability:
    with claude and codex switched off, nothing satisfied the membership test,
    so ``required_frontier_authors`` was empty on every run and the independent
    release inspection returned "not required" — the third layer of the
    verification stack silently turned itself off rather than being carried out
    by the models that were actually enabled.

    The fallback keeps the SIZE of the configured frontier group so a small
    privileged author set stays small; it does not promote the whole roster.
    Pass the roster in takeover order (``service._enabled_role_fallbacks``).
    """
    from . import config

    seats = list(dict.fromkeys(s for s in (roster or []) if s))
    preferred = [s for s in config.FRONTIER_AUTHOR_SEATS if s in seats]
    if preferred:
        return preferred
    return seats[:max(1, len(config.FRONTIER_AUTHOR_SEATS))]


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
