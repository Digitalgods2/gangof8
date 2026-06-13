"""Phase 1: Switchboard backend.

Offline tests verify the session degrades gracefully when an agent backend
fails. Live tests run the real Switchboard pipeline via its zero-cost 'fake'
agent and auto-skip when the service is not running on 127.0.0.1:8787.
"""

import json
import urllib.request

import pytest

from conclave_os import config
from conclave_os.adapters.switchboard import SwitchboardAdapter
from conclave_os.models import Role, SessionStatus
from conclave_os.registry import AgentError
from conclave_os.service import ConclaveService

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)

LLM_ROLES = [Role.researcher, Role.architect, Role.critic, Role.implementer, Role.summarizer]


def _switchboard_up() -> bool:
    try:
        with urllib.request.urlopen(f"{config.SWITCHBOARD_URL}/api/health", timeout=3):
            return True
    except OSError:
        return False


live = pytest.mark.skipif(not _switchboard_up(), reason="Switchboard not running on 127.0.0.1:8787")


# ---------------------------------------------------------------- offline --

class ExplodingAdapter:
    name = "mock"  # replaces the mock adapter in the registry

    def call(self, role, prompt, timeout_s):
        raise AgentError("backend unreachable (simulated)")


def test_agent_error_degrades_to_partial_answer(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(ExplodingAdapter())
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done, "agent failure must not crash the session"
    assert "agent error" in session.stop_reason
    assert session.final is not None
    assert session.final.confidence == "low"
    assert any("agent error" in u for u in session.unresolved)


def test_switchboard_unreachable_is_agent_error(tmp_path):
    adapter = SwitchboardAdapter(agent="fake", base_url="http://127.0.0.1:1")  # nothing listens here
    with pytest.raises(AgentError):
        adapter.call(Role.researcher, "hello", timeout_s=10)


def test_unknown_backend_rejected(tmp_path):
    with pytest.raises(ValueError):
        ConclaveService(data_dir=tmp_path, backend="telepathy")


def test_switchboard_backend_registers_real_agents(tmp_path):
    service = ConclaveService(data_dir=tmp_path, backend="switchboard")
    assert service.registry.names() == ["claude-code", "codex", "gemini"]
    assert service.role_agents[Role.critic] == "codex"


def test_resume_works_across_service_instances(tmp_path):
    """A session paused on the switchboard backend must be resumable from a
    service constructed with the default (mock) backend — the CLI does exactly
    this. Regression: 'no adapter registered for agent gemini'."""
    from conclave_os.models import InputRequest

    service = ConclaveService(data_dir=tmp_path)  # default mock backend
    session = service.manager.create("test task", source="test")
    session.backend = "switchboard"
    session.input_requests.append(
        InputRequest(session_id=session.session_id, agent="gemini",
                     role=Role.researcher, question="q?", resume_token="tsk_x")
    )
    service._ensure_adapters(session)
    assert "gemini" in service.registry.names()


# ------------------------------------------------------------------- live --

@live
def test_fake_agent_single_call():
    adapter = SwitchboardAdapter(
        agent="fake",
        base_url=config.SWITCHBOARD_URL,
        context_extra={"fake_behavior": "resolve_immediately"},
        poll_interval=1.0,
    )
    result = adapter.call(Role.researcher, "Summarize: SQLite vs JSON files.", timeout_s=60)
    assert result.content.strip()


@live
def test_input_passthrough_through_real_switchboard(tmp_path):
    """The fake agent's ask_then_resolve behavior pauses every Switchboard
    task with a question; answering each one must carry the session through
    to completion (every role call pauses once, so we answer in a loop)."""
    from conclave_os.models import Budgets

    service = ConclaveService(
        data_dir=tmp_path,
        backend="switchboard",
        role_agents={r: "fake" for r in LLM_ROLES},
    )
    for name in service.registry.names():
        service.registry.register(
            SwitchboardAdapter(
                agent="fake",
                name=name,
                base_url=config.SWITCHBOARD_URL,
                context_extra={"fake_behavior": "ask_then_resolve"},
                poll_interval=1.0,
            )
        )
    session = service.run(
        TASK, source="test",
        budgets=Budgets(max_rounds=3, max_turns_per_round=1, max_agent_calls=30, max_wall_seconds=600),
    )
    answered = 0
    while session.status == SessionStatus.awaiting_input and answered < 12:
        req = next(r for r in session.input_requests if r.status == "pending")
        assert req.question.strip()
        session = service.answer(session.session_id, req.input_id, "Assume low write volume, single user.")
        answered += 1
    assert answered > 0, "ask_then_resolve must have paused at least once"
    assert session.status == SessionStatus.done
    assert session.final is not None and session.final.answer


@live
def test_full_pipeline_through_real_switchboard(tmp_path):
    service = ConclaveService(
        data_dir=tmp_path,
        backend="switchboard",
        role_agents={r: "fake" for r in LLM_ROLES},
    )
    # steer the fake agent to resolve on its first round
    for name in service.registry.names():
        service.registry.register(
            SwitchboardAdapter(
                agent="fake",
                name=name,
                base_url=config.SWITCHBOARD_URL,
                context_extra={"fake_behavior": "resolve_immediately"},
                poll_interval=1.0,
            )
        )
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.final is not None and session.final.answer
    assert len(session.rounds) >= 1
    assert session.agent_calls <= session.budgets.max_agent_calls
    # the reasoning trail is written regardless of backend
    path = service.store.session_log_path(session.session_id)
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "final_composed" in events
