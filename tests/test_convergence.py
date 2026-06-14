"""Convergence-driven deliberation: an output task is refined (draft↔critique)
until the critic ACCEPTS it, instead of being cut off when the round plan runs
out. The round count is a safety backstop, not the normal terminator.
"""

import pytest

from conclave_os.adapters.mock import MockAdapter
from conclave_os.models import Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService


class _ConvergingAdapter:
    """Critic rejects every draft until its Nth review, then accepts."""

    name = "mock"

    def __init__(self, accept_on_review: int):
        self._inner = MockAdapter()
        self.accept_on_review = accept_on_review
        self.reviews = 0
        self.drafts = 0

    def call(self, role, prompt, timeout_s):
        low = prompt.lower()
        if role == Role.implementer:
            self.drafts += 1
            return AdapterResult(content=f"ARTIFACT: app.py\nprint('v{self.drafts}')\n", duration_ms=1)
        if role == Role.critic and "review the draft" in low:
            self.reviews += 1
            if self.reviews >= self.accept_on_review:
                return AdapterResult(content="acceptable", duration_ms=1)
            return AdapterResult(
                content="Objection: missing error handling and a docstring; please revise.",
                duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


def test_refines_until_critic_accepts(service):
    adapter = _ConvergingAdapter(accept_on_review=5)  # must out-last the phase rounds
    service.registry.register(adapter)
    session = service.run("write the file app.py with a working main function", source="test")

    assert session.status == SessionStatus.done
    # the critic was re-reviewed past the planned phases — i.e. it iterated
    assert adapter.reviews >= 5, f"expected refinement past the phases, got {adapter.reviews}"
    assert adapter.drafts >= 5, "implementer redrafted each rejection"
    assert "accepted" in (session.stop_reason or "").lower()
    assert "refinement" in (session.stop_reason or "").lower()
    assert session.final is not None


def test_accepted_early_skips_refinement(service):
    # critic accepts on the first review → no extra refinement rounds
    adapter = _ConvergingAdapter(accept_on_review=1)
    service.registry.register(adapter)
    session = service.run("write the file app.py with a working main function", source="test")
    assert session.status == SessionStatus.done
    # accepted during the phases; stop reason is plain acceptance, not refinement
    assert "refinement" not in (session.stop_reason or "").lower()


def test_refinement_stops_at_backstop_when_never_accepted(service):
    # critic NEVER accepts → convergence stops at the refine cap, session still finishes
    adapter = _ConvergingAdapter(accept_on_review=9999)
    service.registry.register(adapter)
    session = service.run("write the file app.py with a working main function", source="test")
    assert session.status == SessionStatus.done  # always terminates
    assert session.final is not None
    # bounded: it did not loop forever
    from conclave_os import config
    assert adapter.reviews <= 50  # phases + capped refinement, not unbounded
    assert any("without" in u.lower() and "accept" in u.lower() for u in session.unresolved) \
        or "budget exhausted" in (session.stop_reason or "")
