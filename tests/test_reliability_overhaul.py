"""Regression coverage for the goal-recovery and command-safety overhaul."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

from gangof8 import config, executor, loop, skills
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import (
    ApprovalRequest, Contribution, Council, CouncilMember, Goal, GoalMilestone, ProposedAction,
    Role, Session, SessionStatus, Task,
)
from gangof8.registry import AdapterResult
from gangof8.service import GangOf8Service
from gangof8.sessions import SessionManager
from gangof8 import validation


def test_legacy_settings_timeout_is_ignored_but_explicit_policy_is_honored():
    session = Session(
        session_id="s_timeout", cli_timeouts={"claude": 320},
        task=Task(task_id="t", session_id="s_timeout", text="build"))
    assert loop._effective_agent_timeout(session, "claude", None) == 0
    assert loop._effective_agent_timeout(session, "claude", 900) == 900
    assert loop._effective_agent_timeout(session, "gemini", 360) == 360
    assert loop._effective_agent_timeout(session, "claude", config.PANEL_RETRY_TIMEOUT) == 0
    assert loop._effective_agent_timeout(
        session, "claude", config.FRONTIER_AUTHOR_TIMEOUT
    ) == 0


def test_final_batch_package_suppresses_even_late_repair_promotes(tmp_path):
    store = LogStore(tmp_path / "data")
    approval = ApprovalRequest(
        session_id="s_batch", action="promote game.js", category="file_write")
    action = ProposedAction(
        session_id="s_batch", kind="promote", role=Role.implementer,
        filename="game.js", status="awaiting_approval", approval_id=approval.approval_id)
    session = Session(
        session_id="s_batch", delivery_mode="final_batch", work_package_id="wp_1",
        approvals=[approval], proposed_actions=[action],
        task=Task(task_id="t", session_id="s_batch", text="build"))
    store.save_session(session)
    assert loop._suppress_package_promotes(session, store) == ["game.js"]
    assert action.status == "denied"
    assert approval.status == "denied"
    assert approval.resolved_at and approval.resolved_by == "system"


def test_contract_linked_module_defers_runtime_until_integration(tmp_path):
    store = LogStore(tmp_path / "data")
    manager = SessionManager(store)
    session = manager.create("build module", source="test")
    module = tmp_path / "invaders.js"
    module.write_text(
        "window.ARC.registerGame('invaders', class extends window.ARC.Game {});\n",
        encoding="utf-8")
    session.required_files = ["src/invaders.js"]
    session.deferred_runtime_dependencies = ["src/core.js"]
    session.proposed_actions = [ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="src/invaders.js", status="executed", result_path=str(module))]
    assert loop._verify_artifact_outputs(session, store, require_file=True)
    assert "runtime_deferred" in store.session_log_path(session.session_id).read_text(encoding="utf-8")


def _done_goal_session(goal: Goal, session_id: str, actions: list[ProposedAction]) -> Session:
    return Session(
        session_id=session_id,
        status=SessionStatus.done,
        outcome="succeeded",
        goal_id=goal.goal_id,
        goal_milestone=0,
        goal_epoch=goal.epoch,
        task=Task(task_id=f"t_{session_id}", session_id=session_id, text="build"),
        proposed_actions=actions,
    )


def test_only_static_checks_auto_run_and_shell_forms_are_never_executed(tmp_path):
    store = LogStore(tmp_path / "data")
    governance = Governance(store)
    session = SessionManager(store).create("validate", source="test")
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "src" / "check.py").write_text("answer = 42\n", encoding="utf-8")
    session.workspace_root = str(root)

    static = ProposedAction(
        session_id=session.session_id, kind="run_tests", role=Role.implementer,
        args={"command": "python -m py_compile src/check.py", "target": "workspace"},
    )
    assert governance.authorize_action(session, static) is None
    assert "[passed]" in executor.execute(session, static, store.data_dir)

    functional = ProposedAction(
        session_id=session.session_id, kind="run_tests", role=Role.implementer,
        args={"command": "pytest -q", "target": "workspace"},
    )
    assert governance.authorize_action(session, functional) is not None

    with pytest.raises(validation.ValidationCommandError, match="shell"):
        validation.approved_test_argv("cmd /c whoami")
    with pytest.raises(validation.ValidationCommandError, match="shell"):
        validation.approved_test_argv("pytest -q; whoami")


def test_build_plan_without_outputs_is_parked_not_silently_completed(tmp_path):
    class Planner:
        name = "planner"

        def call(self, role, prompt, timeout_s, images=None):
            return AdapterResult(
                content="MILESTONE 1: Build\nTASK: Implement app.py.\n", duration_ms=1)

    svc = GangOf8Service(
        data_dir=tmp_path / "data", role_agents={Role.architect: "planner"}, panel=[])
    svc.registry.register(Planner())
    goal = svc.create_goal("Build a Python application")
    assert goal.status == "paused"
    assert "delivery contract" in goal.last_error
    assert not goal.milestones


def test_goal_acceptance_keeps_nested_paths_distinct_and_hashes_them(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path / "data")
    delivery = tmp_path / "delivery"
    (delivery / "src").mkdir(parents=True)
    (delivery / "legacy").mkdir(parents=True)
    src = delivery / "src" / "app.js"
    legacy = delivery / "legacy" / "app.js"
    src.write_text("export const current = true;\n", encoding="utf-8")
    legacy.write_text("export const legacy = true;\n", encoding="utf-8")

    milestone = GoalMilestone(
        index=0, title="dual output", task_text="build", status="running", session_id="s_exact",
        contract_declared=True, requires_delivery=True,
        required_files=["src/app.js", "legacy/app.js"],
    )
    goal = Goal(text="build", status="running", milestones=[milestone])
    svc.goals.save(goal)
    actions = [
        ProposedAction(session_id="s_exact", kind="promote", role=Role.implementer,
                       filename="src/app.js", status="executed", result_path=str(src)),
        ProposedAction(session_id="s_exact", kind="promote", role=Role.implementer,
                       filename="legacy/app.js", status="executed", result_path=str(legacy)),
    ]
    svc._maybe_advance_goal(_done_goal_session(goal, "s_exact", actions))
    accepted = svc.goals.get(goal.goal_id)
    assert accepted.status == "completed"
    assert accepted.milestones[0].accepted_files == [str(src), str(legacy)]
    assert accepted.milestones[0].accepted_hashes == {
        "src/app.js": hashlib.sha256(src.read_bytes()).hexdigest(),
        "legacy/app.js": hashlib.sha256(legacy.read_bytes()).hexdigest(),
    }


def test_best_of_n_winner_restores_a_single_nested_goal_contract_path(tmp_path):
    store = LogStore(tmp_path / "data")
    session = SessionManager(store).create("build src/app.js", source="goal")
    session.required_files = ["src/app.js"]
    loop._ship_winner(session, store, "app.js", "export const app = true;\n")
    assert [a.filename for a in session.proposed_actions] == ["src/app.js", "src/app.js"]


def test_cancel_during_planning_cannot_resurrect_a_goal(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingPlanner:
        name = "planner"

        def call(self, role, prompt, timeout_s, images=None):
            entered.set()
            assert release.wait(5)
            return AdapterResult(
                content=("MILESTONE 1: Research\nTASK: Compare choices.\n"
                         "OUTPUTS: NONE\n"), duration_ms=1)

    svc = GangOf8Service(
        data_dir=tmp_path / "data", role_agents={Role.architect: "planner"}, panel=[])
    svc.registry.register(BlockingPlanner())
    goal = Goal(text="Compare two options")
    svc.goals.save(goal)
    worker = threading.Thread(target=svc._plan_and_start, args=(goal.goal_id,))
    worker.start()
    assert entered.wait(5)
    assert svc.cancel_goal(goal.goal_id)["status"] == "cancelled"
    release.set()
    worker.join(5)
    current = svc.goals.get(goal.goal_id)
    assert current.status == "cancelled"
    assert current.milestones == []
    assert svc.store.list_sessions(limit=None) == []


def test_restart_recovers_live_session_even_beyond_dashboard_page(tmp_path):
    data = tmp_path / "data"
    svc = GangOf8Service(data_dir=data)
    live = svc.manager.create("old live session", source="test")
    live.status = SessionStatus.deliberating
    live.created_at = "2000-01-01T00:00:00+00:00"
    svc.store.save_session(live)
    for _ in range(101):
        finished = svc.manager.create("new completed session", source="test")
        finished.status = SessionStatus.done
        svc.store.save_session(finished)

    restarted = GangOf8Service(data_dir=data)
    recovered = restarted.manager.load(live.session_id)
    assert recovered is not None and recovered.status == SessionStatus.cancelled
    assert recovered.outcome == "cancelled"


def test_runtime_prelude_reads_current_session_sandbox_and_checks_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_ROOT", tmp_path / "sandboxes")
    session = Session(
        session_id="s_prelude", task=Task(task_id="t", session_id="s_prelude", text="build"),
        runtime_dependencies=["src/core.js"],
    )
    source = executor.artifacts_dir(tmp_path, session.session_id) / "src" / "core.js"
    source.parent.mkdir(parents=True)
    source.write_text("const Core = {};\n", encoding="utf-8")
    session.dependency_hashes = {
        "src/core.js": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    assert "const Core" in loop._runtime_prelude(session, "src/feature.js")
    source.write_text("const Core = { changed: true };\n", encoding="utf-8")
    assert loop._runtime_prelude(session, "src/feature.js") == ""


def test_dependency_collision_is_deferred_not_blamed_on_current_package(tmp_path):
    store = LogStore(tmp_path / "data")
    workspace = tmp_path / "stage"
    sound = workspace / "src" / "core" / "soundfx.js"
    menu = workspace / "src" / "ui" / "menu.js"
    sound.parent.mkdir(parents=True)
    menu.parent.mkdir(parents=True)
    sound.write_text(
        "const _global = globalThis; const ARC = _global.ARC || (_global.ARC = {});\n",
        encoding="utf-8")
    menu.write_text(
        "const _global = globalThis; const ARC = _global.ARC || (_global.ARC = {});\n",
        encoding="utf-8")
    target = tmp_path / "asteroids.js"
    target.write_text(
        "globalThis.Asteroids = function Asteroids() {};\n", encoding="utf-8")
    session = SessionManager(store).create("build Asteroids", source="goal")
    session.workspace_root = str(workspace)
    session.required_files = ["src/games/asteroids.js"]
    session.runtime_dependencies = ["src/core/soundfx.js", "src/ui/menu.js"]
    session.acceptance_commands = ["node --check src/games/asteroids.js"]
    session.proposed_actions = [ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="src/games/asteroids.js", status="executed", result_path=str(target),
    )]

    assert loop._verify_artifact_outputs(session, store, require_file=True)
    assert any("integration runtime deferred" in item for item in session.unresolved)
    assert not any("asteroids.js: does not run" in item for item in session.unresolved)


def test_acceptance_stage_preserves_nested_paths(tmp_path):
    store = LogStore(tmp_path / "data")
    session = SessionManager(store).create("compile nested file", source="goal")
    delivered = tmp_path / "result" / "src" / "check.py"
    delivered.parent.mkdir(parents=True)
    delivered.write_text("value = 1\n", encoding="utf-8")
    session.acceptance_commands = ["python -m py_compile src/check.py"]
    action = ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="src/check.py", status="executed", result_path=str(delivered),
    )
    assert loop._run_acceptance_checks(session, store, [action]) == []


def test_persisted_none_acceptance_check_is_a_noop(tmp_path):
    store = LogStore(tmp_path / "data")
    session = SessionManager(store).create("old package plan", source="goal")
    session.acceptance_commands = ["NONE"]
    assert loop._run_acceptance_checks(session, store, []) == []


def test_same_path_goal_dependency_becomes_a_revision_target(tmp_path, monkeypatch):
    """A milestone may intentionally replace a file it needs to inspect.

    The original regression stored that file in ``dependency_hashes`` as though
    it were immutable, so validation rejected every successful edit.
    """
    svc = GangOf8Service(data_dir=tmp_path / "data", panel=[])
    original = b"class PlaceholderGame {}\n"
    baseline = hashlib.sha256(original).hexdigest()
    first = GoalMilestone(
        index=0, title="shell", task_text="create arcade.txt", status="done",
        contract_declared=True, requires_delivery=True, required_files=["arcade.txt"],
        accepted_hashes={"arcade.txt": baseline},
    )
    second = GoalMilestone(
        index=1, title="invaders", task_text="replace placeholder", status="pending",
        contract_declared=True, requires_delivery=True, required_files=["arcade.txt"],
        dependencies=["arcade.txt"],
    )
    goal = Goal(text="build arcade", status="running", current_index=1, epoch=4,
                milestones=[first, second])
    svc.goals.save(goal)
    monkeypatch.setattr(svc, "_run_owned", lambda session, fn, background: session)

    session = svc._start_milestone(goal, 1, background=False)

    assert session is not None
    assert session.revision_targets == ["arcade.txt"]
    assert session.revision_base_hashes == {"arcade.txt": baseline}
    assert session.dependency_hashes == {}


def _revision_fixture(tmp_path):
    """Return a small established project and a session editing it in place."""
    store = LogStore(tmp_path / "data")
    manager = SessionManager(store)
    root = tmp_path / "established"
    root.mkdir()
    source = (
        "class Game {}\n"
        "class PlaceholderGame extends Game {}\n"
        "class ArcadePortal {}\n"
        "ArcadePortal.register = function() {};\n"
        "ArcadePortal.register(\"invaders\", \"SPACE INVADERS\", PlaceholderGame);\n"
        "window.Game = Game;\n"
        "window.ArcadePortal = ArcadePortal;\n"
    )
    target = root / "arcade.txt"
    target.write_text(source, encoding="utf-8")
    session = manager.create(
        "Replace the placeholder with class SpaceInvaders extends Game in arcade.txt", source="test")
    session.established_root = str(root)
    session.required_files = ["arcade.txt"]
    session.runtime_dependencies = ["arcade.txt"]
    session.revision_targets = ["arcade.txt"]
    session.revision_base_hashes = {"arcade.txt": hashlib.sha256(target.read_bytes()).hexdigest()}
    # Simulate an older persisted session too: verification must not treat this
    # same-path target as immutable after the edit.
    session.dependency_hashes = dict(session.revision_base_hashes)
    store.save_session(session)
    return store, session, target, source


def test_in_place_revision_preserves_api_and_allows_the_intended_hash_change(tmp_path):
    store, session, target, source = _revision_fixture(tmp_path)

    assert loop._prepare_in_place_revision(session, store)
    sandbox_file = executor.artifacts_dir(store.data_dir, session.session_id) / "arcade.txt"
    revised = source.replace(
        "class PlaceholderGame extends Game {}\n",
        "class PlaceholderGame extends Game {}\nclass SpaceInvaders extends Game {}\n",
    ).replace(
        "ArcadePortal.register(\"invaders\", \"SPACE INVADERS\", PlaceholderGame);",
        "ArcadePortal.register(\"invaders\", \"SPACE INVADERS\", SpaceInvaders);",
    )
    sandbox_file.write_text(revised, encoding="utf-8")
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="edit_file", role=Role.implementer,
        filename="arcade.txt", status="executed", result_path=str(sandbox_file),
    ))

    assert loop._verify_artifact_outputs(session, store, require_file=True)
    assert target.read_text(encoding="utf-8") == source  # no direct overwrite before approval


def test_revision_promotion_prefers_repaired_sandbox_over_stale_staging(tmp_path):
    store, session, target, source = _revision_fixture(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    stale = staging / "arcade.txt"
    stale.write_text(source, encoding="utf-8")
    session.workspace_root = str(staging)
    sandbox_file = executor.artifacts_dir(
        store.data_dir, session.session_id
    ) / "arcade.txt"
    sandbox_file.parent.mkdir(parents=True, exist_ok=True)
    sandbox_file.write_text(source + "\n// corrected\n", encoding="utf-8")

    selected = skills._promote_source(
        session, store.data_dir, "arcade.txt"
    )

    assert selected == sandbox_file
    assert selected.read_text(encoding="utf-8").endswith("// corrected\n")
    assert target.read_text(encoding="utf-8") == source


def test_in_place_revision_rejects_an_external_change_before_delivery(tmp_path):
    store, session, target, source = _revision_fixture(tmp_path)
    assert loop._prepare_in_place_revision(session, store)
    sandbox_file = executor.artifacts_dir(store.data_dir, session.session_id) / "arcade.txt"
    sandbox_file.write_text(source + "class SpaceInvaders extends Game {}\n", encoding="utf-8")
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="edit_file", role=Role.implementer,
        filename="arcade.txt", status="executed", result_path=str(sandbox_file),
    ))
    target.write_text(source + "// changed by a human while the run was active\n", encoding="utf-8")

    assert not loop._verify_artifact_outputs(session, store, require_file=True)
    assert any("revision base changed externally before delivery" in item for item in session.unresolved)


def test_in_place_revision_uses_a_surgical_author_and_reviewer_not_best_of_n(tmp_path):
    store, session, _target, _source = _revision_fixture(tmp_path)
    manager = SessionManager(store)
    lead = CouncilMember(role=Role.lead, agent="author", active=True)
    reviewer = CouncilMember(role=Role.fact_validator, agent="reviewer", active=True)
    council = Council(members=[lead, reviewer])

    old_marker = "<<<" + "<<<< OLD"
    split_marker = "===" + "===="
    new_marker = ">>>" + ">>>> NEW"
    patch = f"""EDIT: arcade.txt
{old_marker}
class PlaceholderGame extends Game {{}}
{split_marker}
class PlaceholderGame extends Game {{}}
class SpaceInvaders extends Game {{}}
{new_marker}
EDIT: arcade.txt
{old_marker}
ArcadePortal.register(\"invaders\", \"SPACE INVADERS\", PlaceholderGame);
{split_marker}
ArcadePortal.register(\"invaders\", \"SPACE INVADERS\", SpaceInvaders);
{new_marker}
"""

    def authored(member, prompt):
        assert member == lead
        assert "PRIMARY REVISION AUTHOR" in prompt
        assert "ARTIFACT" in prompt  # explicit prohibition is part of the contract
        return Contribution(round=0, role=Role.lead, agent="author", content=patch)

    def reviewed(member, prompt):
        assert member == reviewer
        assert "REVISION REVIEWER" in prompt
        return Contribution(round=0, role=Role.fact_validator, agent="reviewer", content="REVIEW: PASS")

    paused = loop._run_in_place_revision(
        session, manager, council, lead, reviewed, authored, Governance(store), store)

    assert not paused
    assert {a.kind for a in session.proposed_actions} == {"edit_file"}
    sandbox_file = executor.artifacts_dir(store.data_dir, session.session_id) / "arcade.txt"
    assert "class SpaceInvaders extends Game" in sandbox_file.read_text(encoding="utf-8")


def test_hand_typed_binary_file_does_not_satisfy_the_deliverable_format():
    """A seat emits text, so a .pdf it types by hand is prose wearing an extension.

    Counting it as the deliverable made the PDF look present, which suppressed
    the BUILD requirement, so the generator that would have produced a real PDF
    was never run and its crash was never discovered.
    """
    from gangof8.models import Classification, Complexity, Risk, TaskType

    session = Session(
        session_id="s_fmt",
        task=Task(task_id="t", session_id="s_fmt", text="compile a pdf of the recipes"))
    session.classification = Classification(
        task_type=TaskType.content, complexity=Complexity.standard, risk=Risk.none,
        deliverable_formats=["pdf"])
    session.proposed_actions = [ProposedAction(
        session_id="s_fmt", kind="write_file", role=Role.implementer,
        filename="Book.pdf", status="executed", result_path="/tmp/Book.pdf")]

    assert loop._produced_deliverable_formats(session) == set()
    assert loop._missing_deliverable_formats(session) == ["pdf"]

    session.proposed_actions.append(ProposedAction(
        session_id="s_fmt", kind="build_artifact", role=Role.implementer,
        args={"command": "python make.py",
              "produced_paths": json.dumps(["/tmp/Book.pdf"])},
        status="executed", result_path="/tmp/Book.pdf"))
    assert loop._produced_deliverable_formats(session) == {"pdf"}
    assert loop._missing_deliverable_formats(session) == []


def test_corrective_followup_reads_its_base_from_the_parent_sandbox(tmp_path):
    """The revision base must never resolve to the file being rewritten.

    A follow-up owns a fresh sandbox, so a recorded "sandbox" source space
    belongs to the parent run. Resolving it against the child reported the base
    as missing, the repair then wrote it, and every later pass compared the
    repair's own bytes to the parent hash and refused to overwrite — a loop no
    repair could ever win.
    """
    data_dir = tmp_path / "data"
    parent = executor.artifacts_dir(data_dir, "s_parent")
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "Book.pdf").write_bytes(b"%PDF-1.7 original")

    session = Session(
        session_id="s_child", parent_session_id="s_parent",
        revision_targets=["Book.pdf"],
        revision_source_spaces={"Book.pdf": "sandbox"},
        task=Task(task_id="t", session_id="s_child", text="the pdf is poor"))

    found = loop._revision_source_for(session, data_dir, "Book.pdf")
    assert found is not None and found.read_bytes() == b"%PDF-1.7 original"

    # Once the repair writes into the child's own sandbox, that copy must still
    # not be mistaken for the base it is supposed to be revising.
    child = executor.artifacts_dir(data_dir, "s_child")
    child.mkdir(parents=True, exist_ok=True)
    (child / "Book.pdf").write_bytes(b"%PDF-1.7 rewritten")
    again = loop._revision_source_for(session, data_dir, "Book.pdf")
    assert again == found


class _Reply:
    def __init__(self, content): self.content = content


def _review_session(sid="s_rev"):
    session = Session(
        session_id=sid, frontier_author_seats=["gemini", "deepseek"],
        task=Task(task_id="t", session_id=sid, text="explain the mother sauces"))
    council = Council(members=[
        CouncilMember(role=Role.lead, agent="gemini", active=True),
        CouncilMember(role=Role.critic, agent="glm", active=False),
        CouncilMember(role=Role.fact_validator, agent="qwen", active=False),
    ])
    session.contributions.append(
        Contribution(round=0, role=Role.lead, agent="gemini", content="done"))
    return session, council


class _NamesOnly:
    def __init__(self, names): self._names = names

    def names(self): return list(self._names)


def test_answer_only_run_is_still_checked_by_a_second_model(tmp_path, monkeypatch):
    """Gating the mandatory check on file artifacts left questions unchecked.

    Every question and research task ran with exactly one model and nothing
    verifying it — the precise case "one does, one checks" exists for.
    """
    store = LogStore(tmp_path)
    session, council = _review_session()
    monkeypatch.setattr(config, "REVIEW_MODE", "on")
    seen: list[str] = []

    def fake_call(sess, registry, st, member, prompt, timeout_s=None):
        seen.append(member.agent)
        return _Reply("REVIEW: PASS\n- none")

    monkeypatch.setattr(loop, "_agent_call", fake_call)
    loop._review_deliverable(session, council, _NamesOnly(["gemini", "glm", "qwen"]),
                             store, [], answer="A veloute is a blond roux plus stock.")

    assert session.review["verdict"] == "pass"
    assert session.review["subject"] == "answer"
    # The author must never be the checker.
    assert seen and "gemini" not in seen


def test_a_lone_review_fail_does_not_veto_but_two_agreeing_ones_do(tmp_path, monkeypatch):
    """A FAIL only counts once a SECOND, different seat agrees with it.

    One seat is allowed to be wrong — a live reviewer failed a run whose PDF had
    in fact been built — so a lone FAIL stays advisory. Two agreeing seats are
    what turns the check into something that can actually refuse delivery.
    """
    store = LogStore(tmp_path)
    monkeypatch.setattr(config, "REVIEW_MODE", "on")
    registry = _NamesOnly(["gemini", "glm", "qwen"])

    def replies(sequence):
        it = iter(sequence)

        def fake_call(sess, reg, st, member, prompt, timeout_s=None):
            return _Reply(next(it))
        return fake_call

    # First seat fails, second disagrees -> advisory only.
    session, council = _review_session("s_rev_split")
    monkeypatch.setattr(loop, "_agent_call", replies(
        ["REVIEW: FAIL\n- wrong kind of artifact", "REVIEW: PASS\n- none"]))
    loop._review_deliverable(session, council, registry, store, [], answer="an answer")
    assert session.review["verdict"] == "fail"
    assert session.review["confirmed"] is False

    # Both seats fail -> confirmed, and the findings are surfaced.
    session, council = _review_session("s_rev_agree")
    monkeypatch.setattr(loop, "_agent_call", replies(
        ["REVIEW: FAIL\n- wrong kind of artifact", "REVIEW: FAIL\n- agreed, it is a script"]))
    loop._review_deliverable(session, council, registry, store, [], answer="an answer")
    assert session.review["verdict"] == "fail"
    assert session.review["confirmed"] is True
    assert any("wrong kind of artifact" in u for u in session.unresolved)
