"""Parallel fan-out: independent sibling consults run concurrently, and the
agent-call budget stays exact under that concurrency (no oversubscription)."""

import threading
import time

from conclave_os import config, loop
from conclave_os.logstore import LogStore
from conclave_os.models import Complexity, Council, CouncilMember, Role
from conclave_os.registry import AdapterResult, AgentRegistry
from conclave_os.sessions import SessionManager


class ConcurrencyProbe:
    """One adapter serving every role; records the peak number of calls it was
    handling at once, so a test can prove the fan-out actually overlapped."""

    name = "mock"

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self._lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.calls = 0

    def call(self, role, prompt, timeout_s, images=None):
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.calls += 1
        try:
            time.sleep(self.delay)
            return AdapterResult(content=f"answer from {role.value}", duration_ms=1)
        finally:
            with self._lock:
                self.running -= 1


def _fixture(tmp_path, max_agent_calls: int):
    store = LogStore(tmp_path)
    session = SessionManager(store).create("fan out to several specialists", source="test")
    session.budgets = config.budgets_for(Complexity.standard)
    session.budgets.max_agent_calls = max_agent_calls
    lead = CouncilMember(role=Role.lead, agent="mock")
    council = Council(members=[lead])
    session.council = council
    registry = AgentRegistry()
    probe = ConcurrencyProbe()
    registry.register(probe)

    def call(member, prompt, timeout_s=None):
        return loop._agent_call(session, registry, store, member, prompt, timeout_s=timeout_s)

    return session, council, lead, store, probe, call


_THREE_CONSULTS = (
    "CONSULT: researcher - question one\n"
    "CONSULT: architect - question two\n"
    "CONSULT: red_team - question three\n"
)


def test_independent_consults_run_concurrently(tmp_path):
    session, council, lead, store, probe, call = _fixture(tmp_path, max_agent_calls=24)

    results = loop._run_delegations(session, council, lead, _THREE_CONSULTS,
                                    call, store, depth=1)

    assert len(results) == 3
    assert probe.calls == 3
    assert probe.max_running >= 2, "sibling consults should overlap, not serialize"
    # order preserved → stable folded output
    assert "researcher" in results[0]
    assert "architect" in results[1]
    assert "red_team" in results[2]
    # budget counted every completed call exactly once
    assert session.agent_calls == 3


def test_budget_exact_under_parallel_contention(tmp_path):
    # Only 2 slots for 3 concurrent consults: the reserve-under-lock must let
    # exactly 2 through and reject the 3rd — never oversubscribe to 3.
    session, council, lead, store, probe, call = _fixture(tmp_path, max_agent_calls=2)

    results = loop._run_delegations(session, council, lead, _THREE_CONSULTS,
                                    call, store, depth=1)

    assert len(results) == 3
    assert session.agent_calls == 2, "must not exceed the cap under a check-then-reserve race"
    assert probe.calls == 2, "only the two that won a slot reached the backend"
    assert sum("failed" in r for r in results) == 1, "the third is rejected on budget"
