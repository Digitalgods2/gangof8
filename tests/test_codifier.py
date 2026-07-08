"""The strong CODIFIER: the post-panel examine/finish stage runs on the
Summarizer seat (set it to a strong model) rather than the fast lead. The lead
only orchestrates stage 1 (kick off, feed the panel, pull in talents); the
codifier does stage 3 (select/review/fix the best-of-N winner, finish it).
"""

from types import SimpleNamespace

from gangof8 import loop
from gangof8.models import Council, CouncilMember, Role


def _session(members):
    return SimpleNamespace(council=Council(members=members))


def test_codifier_prefers_active_summarizer():
    who = loop._codifier(_session([
        CouncilMember(role=Role.lead, agent="claude", active=True),
        CouncilMember(role=Role.summarizer, agent="gemini", active=True),
    ]))
    assert who.role == Role.summarizer and who.agent == "gemini"


def test_codifier_falls_back_to_lead_without_summarizer():
    who = loop._codifier(_session([
        CouncilMember(role=Role.lead, agent="claude", active=True),
    ]))
    assert who.role == Role.lead


def test_codifier_falls_back_when_summarizer_inactive_or_seatless():
    # inactive summarizer → lead
    assert loop._codifier(_session([
        CouncilMember(role=Role.lead, agent="claude", active=True),
        CouncilMember(role=Role.summarizer, agent="gemini", active=False),
    ])).role == Role.lead
    # active but no seat → lead
    assert loop._codifier(_session([
        CouncilMember(role=Role.lead, agent="claude", active=True),
        CouncilMember(role=Role.summarizer, agent=None, active=True),
    ])).role == Role.lead
