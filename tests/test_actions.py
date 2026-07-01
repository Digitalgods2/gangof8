"""Governed tool execution (spaces pipeline).

The implementer proposes artifacts (ARTIFACT: <filename> heading its draft);
ARTIFACT/EDIT/RUNTESTS now write FREELY into the per-session sandbox
(data/artifacts/<session_id>/) with NO approval — they only touch the council's
own scratch area. The ONE approval-gated boundary is `promote`, which copies a
council file into the external established folder (real user code); a PROMOTE:
line in the draft proposes it, and only a human approval executes the copy.
"""

import json
from pathlib import Path

import pytest

from conclave_os import executor
from conclave_os.executor import ExecutionError
from conclave_os.logstore import LogStore
from conclave_os.loop import _collect_proposals
from conclave_os.models import Contribution, Role, SessionStatus
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService
from conclave_os.adapters.mock import MockAdapter
from conclave_os.sessions import SessionManager

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
        if role == Role.lead:
            return AdapterResult(content=self.draft, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def service(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.registry.register(ArtifactAdapter())
    return svc


# --- ARTIFACT writes are now FREE (no approval gate) --------------------------


def test_artifact_writes_freely_into_sandbox(service, tmp_path):
    """An ARTIFACT block is written into the sandbox with no approval; the
    session completes directly."""
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert len(session.proposed_actions) == 1
    action = session.proposed_actions[0]
    assert action.kind == "write_file"
    assert action.status == "executed"
    assert action.filename == "report.md"
    assert not [a for a in session.approvals if a.status == "pending"], "no approval gate"

    path = Path(action.result_path)
    assert path.exists()
    assert path.parent == executor.artifacts_dir(tmp_path, session.session_id).resolve()
    assert "Use SQLite for session logs" in path.read_text(encoding="utf-8")
    assert "ARTIFACT:" not in path.read_text(encoding="utf-8"), "marker line is stripped"
    assert session.files_changed == [str(path)]
    assert session.tools_called == ["write_file"]
    assert session.final is not None and session.final.answer
    events = [
        json.loads(line)["event"]
        for line in service.store.session_log_path(session.session_id).read_text(encoding="utf-8").splitlines()
    ]
    assert "action_proposed" in events and "action_executed" in events


def test_unsafe_filenames_fail_without_writing(tmp_path):
    """An ESCAPING artifact filename is rejected by containment (charset checks
    are gone) — the action fails, nothing is written, the session still completes."""
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(ArtifactAdapter(draft="ARTIFACT: ..\\..\\evil.md\ncontent"))
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.proposed_actions[0].status == "failed"
    assert session.files_changed == []
    assert any("failed" in u for u in session.unresolved)
    assert not (tmp_path / "evil.md").exists()


def test_escaping_path_raises_execution_error(tmp_path):
    """Direct executor check: escaping paths raise (no more silent flattening)."""
    from conclave_os.executor import execute
    from conclave_os.logstore import LogStore
    from conclave_os.models import ProposedAction
    from conclave_os.sessions import SessionManager

    session = SessionManager(LogStore(tmp_path)).create(TASK, source="test")
    action = ProposedAction(
        session_id=session.session_id, kind="write_file",
        args={"filename": "..\\..\\evil.md", "content": "x"},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


def test_artifact_content_strips_code_fence(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(
        ArtifactAdapter(draft="ARTIFACT: app.py\n```python\nprint('x')\n```\n")
    )
    session = service.run(TASK, source="test")
    assert session.proposed_actions[0].content == "print('x')", "wrapping fence is stripped"


def test_collects_multifile_artifacts_after_blank_lines(tmp_path):
    store = LogStore(tmp_path)
    session = SessionManager(store).create("write two files", source="test")
    session.contributions.append(Contribution(
        round=0,
        role=Role.implementer,
        agent="mock",
        content=(
            "ARTIFACT: first.txt\n"
            "first body\n\n"
            "ARTIFACT: second.txt\n"
            "second body\n\n"
            "RUNTESTS: echo ok\n"
        ),
    ))

    _collect_proposals(session, store)

    writes = [a for a in session.proposed_actions if a.kind == "write_file"]
    assert [a.filename for a in writes] == ["first.txt", "second.txt"]
    assert writes[0].content == "first body"
    assert writes[1].content == "second body"


def test_draft_without_marker_proposes_nothing(tmp_path):
    service = ConclaveService(data_dir=tmp_path)  # plain MockAdapter draft has no marker
    session = service.run(TASK, source="test")
    assert session.status == SessionStatus.done
    assert session.proposed_actions == []
    assert session.files_changed == []


# --- the approval gate now lives in PROMOTE (council → established folder) -----


class _EstablishedService(ConclaveService):
    """Test service that stamps an established folder onto every session, so the
    implementer's PROMOTE lines become approval-gated promote actions."""

    established_root: str | None = None

    def _open(self, *a, **k):
        session = super()._open(*a, **k)
        session.established_root = self.established_root
        self.store.save_session(session)
        return session


PROMOTE_DRAFT = (
    "ARTIFACT: report.md\n"
    "# Storage Recommendation\n\n"
    "Use SQLite for session logs.\n"
    "PROMOTE: report.md\n"
)


@pytest.fixture()
def promote_service(tmp_path):
    est = tmp_path / "established"
    est.mkdir()
    svc = _EstablishedService(data_dir=tmp_path / "data")
    svc.established_root = str(est)
    svc.registry.register(ArtifactAdapter(draft=PROMOTE_DRAFT))
    return svc, est


def _propose_promote(promote_service):
    svc, est = promote_service
    session = svc.run(TASK, source="test")
    # the ARTIFACT wrote freely; the session paused only on the PROMOTE gate
    assert session.status == SessionStatus.awaiting_approval
    promote = next(a for a in session.proposed_actions if a.kind == "promote")
    assert promote.status == "awaiting_approval"
    return svc, est, session, promote


def test_promote_pauses_without_touching_established(promote_service):
    svc, est, session, promote = _propose_promote(promote_service)
    approval = next(a for a in session.approvals if a.approval_id == promote.approval_id)
    assert approval.category == "promote"
    assert approval.action_ref == promote.action_id
    assert approval.details, "promote approval carries a diff preview"
    assert not (est / "report.md").exists(), "nothing reaches established before approval"


def test_promote_approval_copies_into_established(promote_service):
    svc, est, session, promote = _propose_promote(promote_service)
    done = svc.approve(session.session_id, promote.approval_id, approved=True)
    assert done.status == SessionStatus.done
    promote = next(a for a in done.proposed_actions if a.kind == "promote")
    assert promote.status == "executed"
    assert (est / "report.md").read_text(encoding="utf-8").startswith("# Storage Recommendation")
    assert any(str(est / "report.md") in f or "report.md" in f for f in done.files_changed)
    assert "promote" in done.tools_called
    assert done.final is not None and done.final.answer


def test_promote_denial_skips_but_completes_session(promote_service):
    svc, est, session, promote = _propose_promote(promote_service)
    done = svc.approve(session.session_id, promote.approval_id, approved=False)
    assert done.status == SessionStatus.done, "denying a promote must not cancel the session"
    promote = next(a for a in done.proposed_actions if a.kind == "promote")
    assert promote.status == "denied"
    assert not (est / "report.md").exists()
    assert any("denied" in u for u in done.unresolved)
    assert done.final is not None


# --- session-level risk gate still cancels / still gates -----------------------


def test_gate_denial_still_cancels(tmp_path):
    service = ConclaveService(data_dir=tmp_path)
    session = service.run("Delete all temp files in C:\\temp and email me the report", source="test")
    assert session.status == SessionStatus.awaiting_approval
    assert session.approvals[0].action_ref is None, "session gates carry no action_ref"
    cancelled = service.approve(session.session_id, session.approvals[0].approval_id, approved=False)
    assert cancelled.status == SessionStatus.cancelled


def test_gate_then_promote_double_approval(tmp_path):
    """Risky task with a promote: gate approval first, then the promote approval."""
    est = tmp_path / "established"
    est.mkdir()
    service = _EstablishedService(data_dir=tmp_path / "data")
    service.established_root = str(est)
    service.registry.register(ArtifactAdapter(draft=PROMOTE_DRAFT))
    session = service.run(
        "Delete all temp files in C:\\temp and email me the report", source="test"
    )
    assert session.status == SessionStatus.awaiting_approval  # gate
    after_gate = service.approve(session.session_id, session.approvals[0].approval_id, approved=True)
    assert after_gate.status == SessionStatus.awaiting_approval  # now the promote
    action_approval = next(a for a in after_gate.approvals if a.status == "pending")
    assert action_approval.action_ref is not None
    assert action_approval.category == "promote"
    done = service.approve(session.session_id, action_approval.approval_id, approved=True)
    assert done.status == SessionStatus.done
    promote = next(a for a in done.proposed_actions if a.kind == "promote")
    assert promote.status == "executed"
    assert (est / "report.md").exists()


# --- multi-file artifacts: all free, no approval ------------------------------

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
    deliberation early-stops and never raises a disagreement."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.lead:
            return AdapterResult(content=MULTI_DRAFT, duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def multi_service(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.registry.register(MultiArtifactAdapter())
    return svc


def test_multiple_artifacts_each_proposed_and_written(multi_service, tmp_path):
    session = multi_service.run("Produce a tiny app with main.py and a readme.", source="test")
    # all artifacts are free now — the session completes without any approval gate
    assert session.status == SessionStatus.done
    files = {a.filename for a in session.proposed_actions}
    assert files == {"main.py", "README.md", "requirements.txt"}
    assert not [a for a in session.approvals if a.status == "pending"], "no approval gates"
    contents = {a.filename: a.content for a in session.proposed_actions}
    assert contents["main.py"] == "print('hello world')"
    assert "Hello App" in contents["README.md"]
    assert "ARTIFACT:" not in contents["main.py"], "next marker is not swallowed into content"


def test_multi_artifact_writes_all_into_sandbox(multi_service, tmp_path):
    session = multi_service.run("Produce a tiny app with main.py and a readme.", source="test")
    assert session.status == SessionStatus.done
    sid = session.session_id
    sandbox = executor.artifacts_dir(tmp_path, sid)
    assert {p.name for p in sandbox.iterdir()} == {"main.py", "README.md", "requirements.txt"}
    assert (sandbox / "main.py").read_text(encoding="utf-8") == "print('hello world')"
    assert all(a.status == "executed" for a in session.proposed_actions)
    assert len(session.files_changed) == 3


def test_code_task_with_no_artifact_fails_verification(tmp_path):
    class NoArtifactAdapter:
        name = "mock"

        def __init__(self):
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s):
            if role == Role.lead:
                return AdapterResult(content="I would create an app here.", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    service = ConclaveService(data_dir=tmp_path)
    service.registry.register(NoArtifactAdapter())

    session = service.run("Create a simple app from scratch", source="test")
    if session.status == SessionStatus.awaiting_input:
        req = session.input_requests[-1]
        session = service.answer(session.session_id, req.input_id, "workspace")

    assert session.status == SessionStatus.done
    assert session.final is not None
    assert session.final.confidence == "low"
    assert "failed artifact verification" in session.final.answer
    assert any("no file artifact" in r for r in session.final.risks_unresolved)
