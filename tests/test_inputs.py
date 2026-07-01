"""Phase 3: agent-question passthrough.

When an agent pauses to ask the human a question, the session pauses in
awaiting_input; answering resumes the same underlying call and the session
continues to completion; declining cancels it.
"""

import json

import pytest

from conclave_os.adapters.mock import FINAL_JSON, MockAdapter
from conclave_os.models import Role, SessionStatus
from conclave_os.registry import AdapterResult, AgentInputRequired
from conclave_os.service import ConclaveService

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)


class PausingAdapter:
    """Pauses once on a chosen role's first call, then behaves like the mock."""

    name = "mock"

    def __init__(self, pause_role: Role):
        self.pause_role = pause_role
        self.paused = False
        self.cancelled = []
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == self.pause_role and not self.paused:
            self.paused = True
            raise AgentInputRequired(
                "What is the expected write volume?", resume_token="sb_task_42"
            )
        return self._inner.call(role, prompt, timeout_s)

    def resume(self, resume_token, answer, timeout_s):
        assert resume_token == "sb_task_42"
        if self.pause_role == Role.summarizer:
            return AdapterResult(content=FINAL_JSON, duration_ms=1)
        return AdapterResult(
            content=f"- Updated facts incorporating the user's answer: {answer}",
            duration_ms=1,
        )

    def cancel_resume(self, resume_token):
        self.cancelled.append(resume_token)


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


def _pause(service, role=Role.lead):
    service.registry.register(PausingAdapter(role))
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.awaiting_input
    assert len(session.input_requests) == 1
    return session


def test_pause_records_the_question(service):
    session = _pause(service)
    req = session.input_requests[0]
    assert req.question == "What is the expected write volume?"
    assert req.role == Role.lead
    assert req.purpose == "deliberation"
    assert session.final is None
    assert session.stop_reason == "agent needs user input"
    listed = service.pending_inputs()
    assert len(listed) == 1 and listed[0]["input_id"] == req.input_id


def test_answer_resumes_to_done(service):
    session = _pause(service)
    req = session.input_requests[0]
    resumed = service.answer(session.session_id, req.input_id, "Low volume, single user.")
    assert resumed.status == SessionStatus.done
    assert resumed.final is not None and resumed.final.answer
    assert resumed.input_requests[0].status == "answered"
    assert any(
        "incorporating the user's answer" in c.content for c in resumed.contributions
    ), "the resumed call's output must join the deliberation"
    # the pre-pause round plus the finishing round after the answer landed
    assert len(resumed.rounds) == 2
    assert resumed.disagreements == [], "the lead model has no disagreement machinery"
    path = service.store.session_log_path(session.session_id)
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "input_requested" in events and "input_answered" in events
    assert events.index("input_requested") < events.index("input_answered")
    assert service.pending_inputs() == []


def test_compose_pause_parses_answer_directly(service):
    session = _pause(service, role=Role.summarizer)
    req = session.input_requests[0]
    assert req.purpose == "compose"
    resumed = service.answer(session.session_id, req.input_id, "Low volume.")
    assert resumed.status == SessionStatus.done
    assert resumed.final.confidence == "high", "answered compose output is parsed as the final answer"


def test_decline_cancels_and_cancels_backend(service):
    session = _pause(service)
    adapter = service.registry._adapters["mock"]
    declined = service.decline_input(session.session_id, session.input_requests[0].input_id)
    assert declined.status == SessionStatus.cancelled
    assert declined.stop_reason == "input declined"
    assert declined.input_requests[0].status == "declined"
    assert adapter.cancelled == ["sb_task_42"], "the paused backend task must be cancelled"


def test_answer_requires_text_and_valid_ids(service):
    session = _pause(service)
    req = session.input_requests[0]
    with pytest.raises(ValueError):
        service.answer(session.session_id, req.input_id, "   ")
    with pytest.raises(KeyError):
        service.answer(session.session_id, "i_nope", "hello")
    with pytest.raises(KeyError):
        service.answer("s_nope", req.input_id, "hello")


def test_api_input_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from conclave_os import main as main_mod

    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(PausingAdapter(Role.lead))
    main_mod.service = service
    client = TestClient(main_mod.app)

    created = client.post("/tasks", json={"text": TASK}).json()
    assert created["status"] == "awaiting_input"
    iid = created["pending_inputs"][0]["input_id"]
    sid = created["session_id"]
    assert client.get("/inputs").json()[0]["input_id"] == iid

    resolved = client.post(
        f"/sessions/{sid}/inputs/{iid}", json={"answer": "Low volume."}
    ).json()
    assert resolved["status"] == "done"
    assert resolved["final"]["answer"]
    assert client.get("/inputs").json() == []

    r422 = client.post(f"/sessions/{sid}/inputs/{iid}", json={})
    assert r422.status_code in (409, 422)  # no answer / already resolved
