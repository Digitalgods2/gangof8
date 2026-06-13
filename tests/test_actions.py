"""Phase 4: governed tool execution.

The implementer proposes an artifact (ARTIFACT: <filename> heading its
draft); the proposal becomes a file_write approval; only a human approval
executes the write, confined to data/artifacts/<session_id>/. Denying the
action skips the artifact but the session still completes.
"""

import json
from pathlib import Path

import pytest

from conclave_os.adapters.mock import MockAdapter
from conclave_os.executor import ExecutionError, _safe_filename
from conclave_os.models import Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService

# 'write' + 'report' → content task → implementer active, no risk gate
TASK = "Write a short report recommending SQLite or plain JSON for session logs."

ARTIFACT_DRAFT = (
    "ARTIFACT: report.md\n"
    "# Storage Recommendation\n\n"
    "Use SQLite for session logs; mirror a JSONL trail for readability.\n"
)


class ArtifactAdapter:
    """Mock whose implementer proposes an artifact."""

    name = "mock"

    def __init__(self, draft: str = ARTIFACT_DRAFT):
        self.draft = draft
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.implementer:
            return AdapterResult(content=self.draft, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def service(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.registry.register(ArtifactAdapter())
    return svc


def _propose(service):
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.awaiting_approval
    assert len(session.proposed_actions) == 1
    return session


def test_proposal_pauses_without_writing(service, tmp_path):
    session = _propose(service)
    action = session.proposed_actions[0]
    assert action.status == "awaiting_approval"
    assert action.filename == "report.md"
    approval = session.approvals[0]
    assert approval.category == "file_write"
    assert approval.action_ref == action.action_id
    assert not (tmp_path / "artifacts" / session.session_id / "report.md").exists(), \
        "nothing may be written before approval"
    assert session.files_changed == []


def test_approval_executes_into_sandbox(service, tmp_path):
    session = _propose(service)
    done = service.approve(session.session_id, session.approvals[0].approval_id, approved=True)
    assert done.status == SessionStatus.done
    action = done.proposed_actions[0]
    assert action.status == "executed"
    path = Path(action.result_path)
    assert path.exists()
    assert path.parent == (tmp_path / "artifacts" / session.session_id).resolve()
    assert "Use SQLite for session logs" in path.read_text(encoding="utf-8")
    assert "ARTIFACT:" not in path.read_text(encoding="utf-8"), "marker line is stripped"
    assert done.files_changed == [str(path)]
    assert done.tools_called == ["write_file"]
    assert done.final is not None and done.final.answer
    events = [
        json.loads(line)["event"]
        for line in service.store.session_log_path(session.session_id).read_text(encoding="utf-8").splitlines()
    ]
    assert "action_proposed" in events and "action_executed" in events


def test_denial_skips_action_but_completes_session(service, tmp_path):
    session = _propose(service)
    done = service.approve(session.session_id, session.approvals[0].approval_id, approved=False)
    assert done.status == SessionStatus.done, "denying an action must not cancel the session"
    assert done.proposed_actions[0].status == "denied"
    assert not (tmp_path / "artifacts" / session.session_id / "report.md").exists()
    assert done.files_changed == []
    assert any("approval denied" in u for u in done.unresolved)
    assert done.final is not None


def test_gate_denial_still_cancels(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    session = service.run("Delete all temp files in C:\\temp and email me the report", source="test")
    assert session.status == SessionStatus.awaiting_approval
    assert session.approvals[0].action_ref is None, "session gates carry no action_ref"
    cancelled = service.approve(session.session_id, session.approvals[0].approval_id, approved=False)
    assert cancelled.status == SessionStatus.cancelled


def test_gate_then_action_double_approval(tmp_path):
    """Risky task with an artifact: gate approval first, then action approval."""
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(ArtifactAdapter())
    session = service.run(
        "Delete all temp files in C:\\temp and email me the report", source="test"
    )
    assert session.status == SessionStatus.awaiting_approval  # gate
    after_gate = service.approve(session.session_id, session.approvals[0].approval_id, approved=True)
    assert after_gate.status == SessionStatus.awaiting_approval  # now the action
    action_approval = next(a for a in after_gate.approvals if a.status == "pending")
    assert action_approval.action_ref is not None
    done = service.approve(session.session_id, action_approval.approval_id, approved=True)
    assert done.status == SessionStatus.done
    assert done.proposed_actions[0].status == "executed"
    assert len(done.files_changed) == 1


def test_unsafe_filenames_fail_without_writing(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(ArtifactAdapter(draft="ARTIFACT: ###\ncontent"))
    session = service.run(TASK, source="test")
    done = service.approve(session.session_id, session.approvals[0].approval_id, approved=True)
    assert done.status == SessionStatus.done
    assert done.proposed_actions[0].status == "failed"
    assert done.files_changed == []
    assert any("failed" in u for u in done.unresolved)


def test_safe_filename_rules():
    assert _safe_filename("report.md") == "report.md"
    assert _safe_filename("..\\..\\evil.md") == "evil.md"  # directories dropped
    assert _safe_filename("../escape.txt") == "escape.txt"
    for bad in ("..", ".", "...", "a/b\\c|d", "###", "  "):
        with pytest.raises(ExecutionError):
            _safe_filename(bad)


def test_artifact_content_strips_code_fence(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(
        ArtifactAdapter(draft="ARTIFACT: app.py\n```python\nprint('x')\n```\n")
    )
    session = service.run(TASK, source="test")
    assert session.proposed_actions[0].content == "print('x')", "wrapping fence is stripped"


def test_draft_without_marker_proposes_nothing(tmp_path):
    service = ConclaveService(data_dir=tmp_path)  # plain MockAdapter draft has no marker
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.proposed_actions == []
    assert session.files_changed == []


# --- multi-file artifacts + resume-after-approval -----------------------------

MULTI_DRAFT = (
    "Here is the app.\n"
    "ARTIFACT: main.py\n"
    "print('hello world')\n"
    "ARTIFACT: README.md\n"
    "# Hello App\nRun with python main.py\n"
    "ARTIFACT: requirements.txt\n"
    "fastapi\n"
)


class MultiArtifactAdapter:
    """Implementer proposes several files; critic accepts the draft so
    deliberation early-stops (exercising the resume guard) and never raises a
    disagreement."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.implementer:
            return AdapterResult(content=MULTI_DRAFT, duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def multi_service(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.registry.register(MultiArtifactAdapter())
    return svc


def test_multiple_artifacts_each_proposed(multi_service):
    session = multi_service.run("Build a tiny app with main.py and a readme.", source="test")
    assert session.status == SessionStatus.awaiting_approval
    files = {a.filename for a in session.proposed_actions}
    assert files == {"main.py", "README.md", "requirements.txt"}
    # one approval per action, each linked back to its action
    assert len([a for a in session.approvals if a.status == "pending"]) == 3
    contents = {a.filename: a.content for a in session.proposed_actions}
    assert contents["main.py"] == "print('hello world')"
    assert "Hello App" in contents["README.md"]
    assert "ARTIFACT:" not in contents["main.py"], "next marker is not swallowed into content"


def test_multi_artifact_writes_all_after_approvals(multi_service, tmp_path):
    session = multi_service.run("Build a tiny app with main.py and a readme.", source="test")
    rounds_at_pause = len(session.rounds)
    sid = session.session_id

    # approve every pending gate; the session resumes when the last one clears
    done = session
    for approval in [a for a in session.approvals if a.status == "pending"]:
        done = multi_service.approve(sid, approval.approval_id, approved=True)

    assert done.status == SessionStatus.done
    assert len(done.rounds) == rounds_at_pause, "resume must NOT re-run deliberation rounds"
    sandbox = tmp_path / "artifacts" / sid
    assert {p.name for p in sandbox.iterdir()} == {"main.py", "README.md", "requirements.txt"}
    assert (sandbox / "main.py").read_text(encoding="utf-8") == "print('hello world')"
    assert all(a.status == "executed" for a in done.proposed_actions)
    assert len(done.files_changed) == 3
