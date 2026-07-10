"""The goal loop: a build whose tests fail gets bounded lead repairs and a
re-run BEFORE anything is delivered — it ships passing its own tests, or says
exactly why not. Test commands really run (subprocess in the sandbox)."""

import json


from gangof8 import config
from gangof8.adapters.mock import MockAdapter
from gangof8.models import Role, SessionStatus
from gangof8.registry import AdapterResult
from gangof8.service import GangOf8Service

# classified as code (filename artifact) → file contract + verification
TASK = "Write check.py, a tiny script, and make sure it runs clean."

FAILING_DRAFT = (
    "ARTIFACT: check.py\n"
    "import sys\nsys.exit(1)\n"
    "RUNTESTS: python check.py\n"
)

FIXED_FILE = "ARTIFACT: check.py\nprint('ok')\n"


class FixingLead:
    """Emits a build whose test run fails; repairs it when shown the failure."""

    name = "mock"

    def __init__(self, fix_draft: str = FIXED_FILE, keep_failing: bool = False):
        self.fix_draft = fix_draft
        self.keep_failing = keep_failing
        self.fix_calls = 0
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            if "TEST OUTPUT:" in prompt:
                self.fix_calls += 1
                if self.keep_failing:
                    return AdapterResult(
                        content="ARTIFACT: check.py\nimport sys\nsys.exit(2)\n", duration_ms=1)
                return AdapterResult(content=self.fix_draft, duration_ms=1)
            return AdapterResult(content=FAILING_DRAFT, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def _events(svc, session):
    path = svc.store.session_log_path(session.session_id)
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_failing_tests_are_repaired_and_rerun(tmp_path):
    lead = FixingLead()
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(lead)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert lead.fix_calls == 1
    assert session.test_fix_attempts == 1
    runs = [a for a in session.proposed_actions if a.kind == "run_tests"]
    assert len(runs) == 2, "the original run plus the automatic re-run"
    assert "[passed]" in runs[-1].result_path, "the repaired build passes"
    assert "test_fix_attempt" in _events(svc, session)
    # the repaired file is what's on disk
    write = [a for a in session.proposed_actions
             if a.kind == "write_file" and a.status == "executed"][-1]
    from pathlib import Path
    assert "print('ok')" in Path(write.result_path).read_text(encoding="utf-8")
    assert not any("still failing" in u for u in session.unresolved)


def test_unfixable_failure_gives_up_with_the_reason(tmp_path):
    class GivingUpLead(FixingLead):
        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead and "TEST OUTPUT:" in prompt:
                self.fix_calls += 1
                return AdapterResult(
                    content="Cannot fix here: the sandbox lacks the required interpreter.",
                    duration_ms=1)
            return super().call(role, prompt, timeout_s, images)

    lead = GivingUpLead()
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(lead)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done, "an honest failure still completes"
    assert lead.fix_calls == 1, "no pointless retries after an explicit give-up"
    assert any("offered no fix" in u for u in session.unresolved)


def test_fix_attempts_are_bounded_and_reported(tmp_path):
    lead = FixingLead(keep_failing=True)
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(lead)
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.test_fix_attempts == config.MAX_TEST_FIX_ATTEMPTS
    assert lead.fix_calls == config.MAX_TEST_FIX_ATTEMPTS
    assert any("still failing after" in u for u in session.unresolved)


def test_passing_tests_never_enter_the_loop(tmp_path):
    class PassingLead(FixingLead):
        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                assert "TEST OUTPUT:" not in prompt, "no repair prompt for a passing build"
                return AdapterResult(
                    content="ARTIFACT: check.py\nprint('ok')\nRUNTESTS: python check.py\n",
                    duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(PassingLead())
    session = svc.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.test_fix_attempts == 0
    assert "test_fix_attempt" not in _events(svc, session)
