"""Phase 3: established-folder path extraction + the promote-time target ask.

A path the user references in the prompt becomes the session's established folder
(read source + approval-gated promote target). A build that references NO path
runs freely in the sandbox; only when it wants to DELIVER (PROMOTE) does the
coordinator ask WHERE — at delivery time, never up front.
"""

import pytest

from conclave_os.classifier import classify
from conclave_os.models import Role, SessionStatus, TaskType
from conclave_os.paths import extract_established_root
from conclave_os.registry import AdapterResult
from conclave_os.adapters.mock import MockAdapter
from conclave_os.service import ConclaveService


# --- path extraction ----------------------------------------------------------


def test_extract_quoted_windows_path(tmp_path):
    d = tmp_path / "pushmodo"
    d.mkdir()
    text = f'examine the app in "{d}" and recommend improvements'
    assert extract_established_root(text) == str(d.resolve())


def test_extract_file_path_returns_parent(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "main.py"
    f.write_text("x = 1", encoding="utf-8")
    assert extract_established_root(f"look at {f}") == str(d.resolve())


def test_extract_drive_path_even_if_missing():
    # a drive-qualified path is accepted as an intended target even if absent
    got = extract_established_root(r"build into C:\Users\me\newproj please")
    assert got is not None and got.lower().endswith("newproj")


def test_no_path_referenced_returns_none():
    assert extract_established_root("build me a tic-tac-toe game") is None


# --- greenfield classification ------------------------------------------------


def test_greenfield_build_is_flagged():
    c = classify("build me a brand new tic-tac-toe app from scratch")
    assert c.task_type == TaskType.code and c.greenfield is True


def test_examine_task_is_not_greenfield():
    c = classify("examine the existing app and recommend 10 improvements")
    assert c.greenfield is False


def test_examine_recommend_is_analysis_no_file_output():
    # the reported bug: "examine ... recommend ... make this app better" must NOT
    # be a code/file-producing task (it was echoing source files into the sandbox)
    c = classify("examine the app in this folder and understand it and then "
                 "recommend 5 things that can be introduced or improved to make "
                 "this app even better")
    assert c.task_type == TaskType.research
    assert c.produces_output is False


def test_examine_with_real_modify_verb_stays_code():
    # an explicit change verb keeps it a file-producing task
    assert classify("examine the app and refactor the auth module").task_type == TaskType.code
    assert classify("review X then implement the fix").task_type == TaskType.code


# --- the promote-time target ask (ask at delivery, don't assume) ---------------


class _ImplAdapter:
    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.lead:
            return AdapterResult(content="ARTIFACT: game.py\nprint('tic tac toe')\n", duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def _svc(tmp_path):
    svc = ConclaveService(data_dir=tmp_path / "data")
    svc.registry.register(_ImplAdapter())
    return svc


def test_greenfield_without_promote_runs_free(tmp_path):
    """No up-front target question: a greenfield build that never asks to
    deliver completes with zero pauses (sandbox writes are free)."""
    svc = _svc(tmp_path)
    session = svc.run("build me a brand new tic-tac-toe app from scratch", source="test")
    assert session.status == SessionStatus.done
    assert session.input_requests == []
    assert session.approvals == []


class _PromotingImplAdapter:
    """Lead writes a file AND asks to deliver it — with no target referenced."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            return AdapterResult(
                content="ARTIFACT: game.py\nprint('tic tac toe')\nPROMOTE: game.py\n",
                duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def _promote_svc(tmp_path):
    svc = ConclaveService(data_dir=tmp_path / "data")
    svc.registry.register(_PromotingImplAdapter())
    return svc


def test_promote_without_target_asks_at_delivery_time(tmp_path):
    svc = _promote_svc(tmp_path)
    session = svc.run("build me a brand new tic-tac-toe app from scratch", source="test")
    assert session.status == SessionStatus.awaiting_input
    req = session.input_requests[-1]
    assert req.agent == "system" and req.purpose == "promote_target"
    assert "game.py" in req.question
    assert "where" in req.question.lower()


def test_answer_with_path_routes_promote_through_the_gate(tmp_path):
    svc = _promote_svc(tmp_path)
    target = tmp_path / "delivery"
    target.mkdir()
    session = svc.run("build me a brand new tic-tac-toe app from scratch", source="test")
    req = session.input_requests[-1]
    session = svc.answer(session.session_id, req.input_id, str(target))
    assert session.established_asked is True
    assert session.established_root == str(target.resolve())
    # the promote now pauses on the ONE hard gate, with a diff to review
    assert session.status == SessionStatus.awaiting_approval
    approval = next(a for a in session.approvals if a.status == "pending")
    assert approval.category == "promote"
    assert not (target / "game.py").exists(), "nothing lands before approval"
    done = svc.approve(session.session_id, approval.approval_id, approved=True)
    assert done.status == SessionStatus.done
    assert (target / "game.py").read_text(encoding="utf-8").startswith("print(")


def test_answer_workspace_keeps_in_council_space(tmp_path):
    svc = _promote_svc(tmp_path)
    session = svc.run("create a new app from scratch", source="test")
    req = session.input_requests[-1]
    assert req.purpose == "promote_target"
    session = svc.answer(session.session_id, req.input_id, "workspace")
    assert session.established_asked is True
    assert session.established_root is None
    assert session.status == SessionStatus.done
    promote = next(a for a in session.proposed_actions if a.kind == "promote")
    assert promote.status == "denied", "delivery skipped; file stays in the sandbox"
    write = next(a for a in session.proposed_actions if a.kind == "write_file")
    assert write.status == "executed"


def test_free_write_into_established_subfolder_is_refused(tmp_path):
    """A subfolder of the source IS the source: a free write (workspace target)
    that resolves inside the established folder must be refused — only an approved
    promote may reach it."""
    from conclave_os.executor import ExecutionError, execute
    from conclave_os.logstore import LogStore
    from conclave_os.models import ProposedAction
    from conclave_os.sessions import SessionManager

    est = tmp_path / "pushmodo"
    est.mkdir()
    store = LogStore(tmp_path / "data")
    s = SessionManager(store).create("work", source="test")
    s.established_root = str(est)
    # workspace mistakenly pointed at a SUBFOLDER of the established source
    s.workspace_root = str(est / "sub")
    action = ProposedAction(
        session_id=s.session_id, kind="write_file", role=Role.implementer,
        args={"filename": "x.py", "content": "nope", "target": "workspace"},
    )
    with pytest.raises(ExecutionError):
        execute(s, action, store.data_dir)
    assert not (est / "sub" / "x.py").exists()  # nothing written into the source tree


def test_established_overview_injected_into_prompts(tmp_path):
    """The council must START with the established folder's real content, not
    depend on an agent remembering to request a SKILL (the gemini-refusal bug)."""
    from conclave_os import loop
    from conclave_os.classifier import classify
    from conclave_os.logstore import LogStore
    from conclave_os.roles import build_council
    from conclave_os.sessions import SessionManager

    est = tmp_path / "app"
    (est / "src").mkdir(parents=True)
    (est / "README.md").write_text("# CoolApp\nDoes neat things.", encoding="utf-8")
    (est / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    store = LogStore(tmp_path / "data")
    s = SessionManager(store).create("examine it", source="test")
    s.established_root = str(est)
    s.classification = classify("examine it")

    overview = loop._established_overview(s, store.data_dir)
    assert "README.md" in overview and "CoolApp" in overview      # key file content read
    assert "src/main.py" in overview                              # directory tree present

    council = build_council(s.classification)
    p = loop.lead_prompt(s, council, None, overview)
    assert "CoolApp" in p                                         # injected into the lead prompt
    # and the governance context forbids the "can't access" refusal
    assert "NEVER say you 'cannot access'" in p


class _PromoteAdapter:
    """Implementer writes a file to scratch AND asks to promote it."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.lead:
            return AdapterResult(
                content="ARTIFACT: feature.py\nprint('new feature')\n\nPROMOTE: feature.py\n",
                duration_ms=1)
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_promote_pipeline_end_to_end(tmp_path):
    """examine an established folder → write to scratch (free) → PROMOTE pauses for
    approval with a diff → approve → the file lands in the real folder."""
    established = tmp_path / "realproj"
    established.mkdir()
    svc = ConclaveService(data_dir=tmp_path / "data")
    svc.registry.register(_PromoteAdapter())

    session = svc.run(f'add a feature to the app in "{established}"', source="test")
    assert session.established_root == str(established.resolve())
    # the free write executed into the sandbox; the promote paused for approval
    assert session.status == SessionStatus.awaiting_approval
    promote_approval = next(a for a in session.approvals
                            if a.category == "promote" and a.status == "pending")
    assert promote_approval.details and "feature.py" in promote_approval.details
    assert "new feature" in promote_approval.details        # the diff shows the content
    assert not (established / "feature.py").exists()         # nothing in real folder yet

    done = svc.approve(session.session_id, promote_approval.approval_id, approved=True)
    assert done.status == SessionStatus.done
    assert (established / "feature.py").read_text(encoding="utf-8").strip() == "print('new feature')"


def test_promote_denied_leaves_established_untouched(tmp_path):
    established = tmp_path / "realproj"
    established.mkdir()
    svc = ConclaveService(data_dir=tmp_path / "data")
    svc.registry.register(_PromoteAdapter())
    session = svc.run(f'add a feature to the app in "{established}"', source="test")
    promote_approval = next(a for a in session.approvals if a.category == "promote")
    done = svc.approve(session.session_id, promote_approval.approval_id, approved=False)
    assert done.status == SessionStatus.done
    assert not (established / "feature.py").exists()  # denial keeps real code untouched


def test_empty_workspace_clears_contents_only(tmp_path):
    svc = ConclaveService(data_dir=tmp_path / "data")
    proj = tmp_path / "proj"
    ws = svc.create_workspace("proj", str(proj))
    svc.set_active_workspace(ws.id)
    (proj / "old.py").write_text("stale", encoding="utf-8")
    (proj / "sub").mkdir()
    (proj / "sub" / "more.py").write_text("x", encoding="utf-8")

    out = svc.empty_workspace()
    assert out["emptied"] == ws.id
    assert proj.is_dir() and list(proj.iterdir()) == []  # folder kept, contents gone


def test_referenced_path_skips_the_gate(tmp_path):
    svc = _svc(tmp_path)
    existing = tmp_path / "existingproj"
    existing.mkdir()
    session = svc.run(f'build a new module inside "{existing}"', source="test")
    # a path was referenced → established set, no greenfield question
    assert session.established_root == str(existing.resolve())
    assert not any(r.purpose == "establish_target" for r in session.input_requests)
