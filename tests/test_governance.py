"""Default-deny governance: side effects are always blocked without an
explicit human approval, and risky tasks pause before round 1."""

import pytest

from gangof8.governance import ApprovalRequired, Governance
from gangof8.logstore import LogStore
from gangof8.models import Risk, SessionStatus
from gangof8.service import GangOf8Service
from gangof8.sessions import SessionManager


@pytest.fixture()
def service(tmp_path):
    return GangOf8Service(data_dir=tmp_path)


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


def _promote_session(tmp_path, existing: str, replacement: str):
    """A session whose sandbox holds `replacement` for a file that already
    exists in the user's established folder as `existing`."""
    from gangof8 import executor
    from gangof8.models import ProposedAction

    established = tmp_path / "delivered"
    established.mkdir()
    (established / "gen.py").write_text(existing, encoding="utf-8")
    store = LogStore(tmp_path / "data")
    session = SessionManager(store).create("deliver", source="test")
    session.established_root = str(established)
    sandbox = executor.artifacts_dir(store.data_dir, session.session_id)
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "gen.py").write_text(replacement, encoding="utf-8")
    action = ProposedAction(session_id=session.session_id, kind="promote",
                            filename="gen.py", args={"filename": "gen.py"})
    return session, action, Governance(store)


def test_destructive_promote_is_named_in_the_approval(tmp_path):
    """Replacing a file with a fraction of itself must SAY so.

    Regression: a truncated council copy replaced a 49,283-byte delivered file
    with 514 bytes. The diff was shown and approved, but nothing stated that
    99% of the file was being deleted.
    """
    session, action, gov = _promote_session(
        tmp_path, "x = 1\n" + "# body\n" * 500, "x = 1\n")
    approval = gov.authorize_action(session, action)
    assert approval is not None
    assert "DESTRUCTIVE promote" in approval.action
    assert "removing 99" in approval.action and "%" in approval.action


def test_standing_promote_approval_does_not_cover_a_destructive_promote(tmp_path):
    """'Approve all promote' saves identical clicks; it is not consent to gut
    an existing file. That promote stops for its own decision."""
    session, action, gov = _promote_session(
        tmp_path, "x = 1\n" + "# body\n" * 500, "x = 1\n")
    session.standing_approvals.append("promote")
    assert gov.authorize_action(session, action) is not None


def test_standing_promote_approval_still_covers_an_ordinary_promote(tmp_path):
    """A normal delivery must not start prompting again."""
    body = "x = 1\n" + "# body\n" * 500
    session, action, gov = _promote_session(tmp_path, body, body + "y = 2\n")
    session.standing_approvals.append("promote")
    assert gov.authorize_action(session, action) is None
