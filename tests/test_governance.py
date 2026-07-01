"""Default-deny governance: side effects are always blocked without an
explicit human approval, and risky tasks pause before round 1."""

import pytest

from conclave_os.governance import ApprovalRequired, Governance
from conclave_os.logstore import LogStore
from conclave_os.models import Risk, SessionStatus
from conclave_os.service import ConclaveService
from conclave_os.sessions import SessionManager


@pytest.fixture()
def service(tmp_path):
    return ConclaveService(data_dir=tmp_path)


@pytest.fixture()
def governance(tmp_path):
    return Governance(LogStore(tmp_path))


@pytest.fixture()
def session(tmp_path):
    store = LogStore(tmp_path)
    return SessionManager(store).create("test task for governance", source="test")


def test_generate_text_always_allowed(governance, session):
    assert governance.requires_approval("generate_text") is False
    governance.check(session, "generate_text")  # must not raise
    assert session.approvals == []


def test_everything_else_requires_approval(governance, session):
    for capability, category in [
        ("file_write", "file_write"),
        ("file_delete", "file_delete"),
        ("code_exec", "code_exec"),
        ("send_message", "send_message"),
        ("spend", "spend"),
        ("settings", "settings"),
        ("external", "external"),
    ]:
        assert governance.requires_approval(capability) is True
        with pytest.raises(ApprovalRequired):
            governance.check(session, capability, action=f"do {capability}", category=category)
    assert len(session.approvals) == 7
    assert all(a.status == "pending" for a in session.approvals)


def test_approved_action_passes(governance, session):
    with pytest.raises(ApprovalRequired) as exc:
        governance.check(session, "file_write", action="write file out.md", category="file_write")
    approval = exc.value.approval
    governance.resolve(session, approval.approval_id, approved=True, by="test-user")
    governance.check(session, "file_write", action="write file out.md", category="file_write")  # no raise
    assert session.approvals[0].status == "approved"
    assert session.approvals[0].resolved_by == "test-user"


def test_denied_action_still_blocks(governance, session):
    with pytest.raises(ApprovalRequired) as exc:
        governance.check(session, "spend", action="buy credits", category="spend")
    governance.resolve(session, exc.value.approval.approval_id, approved=False)
    with pytest.raises(ApprovalRequired):
        governance.check(session, "spend", action="buy credits", category="spend")


def test_risky_task_runs_without_a_pre_run_gate(service):
    """Risk is classified for visibility but no longer pauses the run — the one
    hard gate is the promote approval at delivery time. Sandbox-only work on a
    risky-sounding task flows straight through."""
    session = service.run(
        "Delete all temp files in C:\\temp and email me the report", source="test"
    )
    assert session.status == SessionStatus.done
    assert session.classification.risk == Risk.high
    assert session.classification.human_approval_required is True  # informational
    assert session.approvals == [], "no pre-run approval gate"
    assert len(session.rounds) >= 1, "deliberation ran immediately"
    assert session.final is not None


def test_safe_task_never_pauses(service):
    session = service.run("What is SQLite?", source="test")
    assert session.status == SessionStatus.done
    assert session.approvals == []
