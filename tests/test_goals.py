"""Goal layer (/goal): long-horizon objectives run milestone by milestone.

The architect plans milestone-sized deliverables once; each milestone runs as a
normal session and completing one auto-advances to the next. Failure/cancel
pauses the goal; the human resumes with a fresh attempt. A server restart parks
in-flight goals as paused (their workers died with the process).
"""

import hashlib
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gangof8 import assembly, config, goals as goals_mod
from gangof8.adapters.mock import MockAdapter
from gangof8.models import Goal, GoalMilestone, Role, Session, SessionStatus, Task
from gangof8.registry import AdapterResult
from gangof8.service import GangOf8Service

PLAN = (
    "MILESTONE 1: Storage decision\n"
    "TASK: Compare SQLite vs plain JSON for session logs and recommend one.\n"
    "OUTPUTS: NONE\n"
    "MILESTONE 2: Retention decision\n"
    "TASK: Recommend a retention policy for the store chosen in milestone 1.\n"
    "OUTPUTS: NONE\n"
)

PROMOTE_DRAFT = (
    "ARTIFACT: report.md\n"
    "# Storage Recommendation\n\nUse SQLite.\n"
    "PROMOTE: report.md\n"
)

INVALID_ASSEMBLY_PLAN = (
    "PACKAGE 1: Runtime core\nOWNER: gemini\nAFTER: NONE\nCONTRACTS: NONE\n"
    "TASK: Implement the shared browser runtime.\nOUTPUTS: src/core.js\n"
    "RELEASE: NONE\nREQUIRES: NONE\nASSEMBLY: NONE\nTEMPLATE: NONE\n"
    "INTERFACE: PROVIDES the runtime API\nCHECK: node --check src/core.js\n"
    "PACKAGE 2: Game module\nOWNER: gemini\nAFTER: NONE\nCONTRACTS: NONE\n"
    "TASK: Implement a game module.\nOUTPUTS: src/game.js\n"
    "RELEASE: NONE\nREQUIRES: NONE\nASSEMBLY: NONE\nTEMPLATE: NONE\n"
    "INTERFACE: PROVIDES a playable game\nCHECK: node --check src/game.js\n"
    "PACKAGE 3: Release\nOWNER: gemini\nAFTER: 1, 2\nCONTRACTS: NONE\n"
    "TASK: Assemble the final single-file HTML.\nOUTPUTS: index.html\n"
    "RELEASE: index.html\nREQUIRES: src/core.js, src/game.js\n"
    "ASSEMBLY: HTML_INLINE\nTEMPLATE: css/theme.css\n"
    "INTERFACE: PROVIDES the final application\nCHECK: NONE\n"
)

REPAIRED_ASSEMBLY_PLAN = (
    "PACKAGE 1: Runtime core\nOWNER: gemini\nAFTER: NONE\nCONTRACTS: NONE\n"
    "TASK: Implement the shared browser runtime.\nOUTPUTS: src/core.js\n"
    "RELEASE: NONE\nREQUIRES: NONE\nASSEMBLY: NONE\nTEMPLATE: NONE\n"
    "INTERFACE: PROVIDES the runtime API\nCHECK: node --check src/core.js\n"
    "PACKAGE 2: Game module\nOWNER: gemini\nAFTER: NONE\nCONTRACTS: NONE\n"
    "TASK: Implement a game module.\nOUTPUTS: src/game.js\n"
    "RELEASE: NONE\nREQUIRES: NONE\nASSEMBLY: NONE\nTEMPLATE: NONE\n"
    "INTERFACE: PROVIDES a playable game\nCHECK: node --check src/game.js\n"
    "PACKAGE 3: Integration QA shell\nOWNER: gemini\nAFTER: 1, 2\nCONTRACTS: NONE\n"
    "TASK: Integrate and verify both runtime producers and author the HTML shell.\n"
    "OUTPUTS: shell.html\nRELEASE: NONE\nREQUIRES: src/core.js, src/game.js\n"
    "ASSEMBLY: NONE\nTEMPLATE: NONE\n"
    "INTERFACE: PROVIDES the verified integration shell\nCHECK: NONE\n"
    "PACKAGE 4: Release\nOWNER: gemini\nAFTER: 3\nCONTRACTS: NONE\n"
    "TASK: Assemble the verified final single-file HTML.\nOUTPUTS: index.html\n"
    "RELEASE: index.html\nREQUIRES: src/core.js, src/game.js, shell.html\n"
    "ASSEMBLY: HTML_INLINE\nTEMPLATE: shell.html\n"
    "INTERFACE: PROVIDES the final application\nCHECK: NONE\n"
)


class _PlannerSeat:
    """Mock seat that answers the goal-planning prompt with a parseable plan
    (and optionally promotes on 'delivered into' tasks); everything else
    falls through to the stock MockAdapter."""

    name = "mock"

    def __init__(self, plan=PLAN, promoting=False):
        self._inner = MockAdapter()
        self._plan = plan
        self._promoting = promoting

    def call(self, role, prompt, timeout_s, images=None):
        from gangof8.models import Role
        if "MILESTONE 1:" in prompt and "GOAL:" in prompt:
            return AdapterResult(content=self._plan, duration_ms=1)
        if self._promoting and role in (Role.lead, Role.panelist) and "delivered into" in prompt:
            return AdapterResult(content=PROMOTE_DRAFT, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


class _SequencedPlannerSeat:
    """Recording architect that returns each supplied plan, then repeats the last."""

    name = "gemini"

    def __init__(self, plans):
        self.plans = list(plans)
        self.prompts: list[str] = []
        self.roles: list[Role] = []
        self.timeouts: list[int] = []

    def call(self, role, prompt, timeout_s, images=None):
        self.prompts.append(prompt)
        self.roles.append(role)
        self.timeouts.append(timeout_s)
        index = min(len(self.prompts) - 1, len(self.plans) - 1)
        return AdapterResult(content=self.plans[index], duration_ms=1)


class _BlockingRepairPlannerSeat(_SequencedPlannerSeat):
    """Hold the second call so cancellation can revoke its planning lease."""

    def __init__(self, plans):
        super().__init__(plans)
        self.repair_started = threading.Event()
        self.release_repair = threading.Event()

    def call(self, role, prompt, timeout_s, images=None):
        if len(self.prompts) == 1:
            self.repair_started.set()
            self.release_repair.wait(timeout=5)
        return super().call(role, prompt, timeout_s, images=images)


@pytest.fixture()
def svc(tmp_path):
    s = GangOf8Service(data_dir=tmp_path / "data")
    s.registry.register(_PlannerSeat())
    return s


# ---- plan parsing ------------------------------------------------------------


def test_parse_milestones_strict_format():
    ms = goals_mod.parse_milestones(PLAN)
    assert [m.title for m in ms] == ["Storage decision", "Retention decision"]
    assert ms[0].index == 0 and ms[1].index == 1
    assert "recommend one" in ms[0].task_text


def test_parse_milestones_tolerates_prose_fences_and_multiline_tasks():
    text = (
        "Here is my plan:\n```\nMILESTONE 1: Core\nTASK: Build the core.\n"
        "It should include a config file.\n\nMILESTONE 2: Polish\n"
        "TASK: Polish everything.\n```\nGood luck!"
    )
    ms = goals_mod.parse_milestones(text)
    assert [m.title for m in ms] == ["Core", "Polish"]
    assert "config file" in ms[0].task_text  # TASK continues across lines


def test_parse_milestones_unparseable_returns_empty():
    assert goals_mod.parse_milestones("I would start with the backend.") == []
    assert goals_mod.parse_milestones("") == []


def test_parse_milestones_caps_at_config_max():
    text = "".join(f"MILESTONE {i}: Step {i}\nTASK: do {i}\n" for i in range(1, 20))
    from gangof8 import config
    assert len(goals_mod.parse_milestones(text)) == config.GOAL_MAX_MILESTONES


def test_parse_milestones_records_explicit_acceptance_contract():
    plan = (
        "MILESTONE 1: Core\n"
        "TASK: Build the base.\n"
        "OUTPUTS: shell.html, core.js\n"
        "REQUIRES: package.json\n"
        "CHECK: node --check core.js\n"
    )
    ms = goals_mod.parse_milestones(plan)
    assert ms[0].required_files == ["shell.html", "core.js"]
    assert ms[0].dependencies == ["package.json"]
    assert ms[0].acceptance_commands == ["node --check core.js"]
    assert ms[0].contract_declared and ms[0].requires_delivery


def test_parse_milestones_treats_check_none_as_no_check():
    plan = (
        "PACKAGE 1: Integrate\nOWNER: claude\nTASK: Assemble.\n"
        "OUTPUTS: index.html\nCHECK: NONE\n"
    )
    package = goals_mod.parse_milestones(plan)[0]
    assert package.acceptance_commands == []


def test_parse_release_manifest_is_separate_from_staging_outputs():
    plan = (
        "PACKAGE 1: Integrate\nOWNER: claude\nAFTER: NONE\nCONTRACTS: NONE\n"
        "TASK: Assemble the app.\n"
        "OUTPUTS: arcade.html, tools/build.mjs, README.md\n"
        "RELEASE: arcade.html\nREQUIRES: NONE\n"
        "INTERFACE: PROVIDES the final app\n"
    )
    package = goals_mod.parse_milestones(plan)[0]
    assert package.required_files == ["arcade.html", "tools/build.mjs", "README.md"]
    assert package.release_files == ["arcade.html"]
    assert package.release_declared is True


def test_parse_owned_package_graph():
    plan = (
        "PACKAGE 1: Contract\nOWNER: claude\nAFTER: NONE\n"
        "TASK: Define the engine contract.\nOUTPUTS: src/contract.js\n"
        "REQUIRES: NONE\nINTERFACE: export createEngine()\n"
        "PACKAGE 2: Renderer\nOWNER: gemini\nAFTER: 1\n"
        "TASK: Implement the renderer.\nOUTPUTS: src/render.js\n"
        "REQUIRES: src/contract.js\nINTERFACE: consume createEngine()\n"
    )
    packages = goals_mod.parse_milestones(plan)
    assert [p.owner for p in packages] == ["claude", "gemini"]
    assert packages[1].depends_on == [0]
    assert packages[0].interface_contract == "export createEngine()"


def test_parse_contract_dependencies_as_non_blocking_edges():
    plan = (
        "PACKAGE 1: Runtime\nOWNER: claude\nAFTER: NONE\nCONTRACTS: NONE\n"
        "TASK: Define the runtime.\nOUTPUTS: src/core.js\nREQUIRES: NONE\n"
        "INTERFACE: PROVIDES ARC.Game and ARC.registerGame\n"
        "PACKAGE 2: Game\nOWNER: codex\nAFTER: NONE\nCONTRACTS: 1\n"
        "TASK: Implement against the runtime contract.\nOUTPUTS: src/game.js\n"
        "REQUIRES: NONE\nINTERFACE: CONSUMES ARC.Game and ARC.registerGame\n"
    )
    packages = goals_mod.parse_milestones(plan)
    assert packages[1].depends_on == []
    assert packages[1].contract_depends_on == [0]


def test_plan_prompt_reserves_after_for_real_bytes():
    prompt = goals_mod.plan_prompt("Build an app", ["claude", "codex"])
    assert "CONTRACTS never blocks scheduling" in prompt
    assert "MUST use AFTER" in prompt
    assert "Deterministic assembly is concatenation, not integration" in prompt
    assert "A broad build should normally start most owners in the first wave" in prompt
    assert "exact upstream TEMPLATE path must also appear in REQUIRES" in prompt
    assert "runtime HTML/CSS/JS artifact that final assembly lists in REQUIRES" in prompt


def test_invalid_assembly_plan_is_repaired_with_exact_validator_feedback(
    tmp_path, monkeypatch,
):
    planner = _SequencedPlannerSeat([INVALID_ASSEMBLY_PLAN, REPAIRED_ASSEMBLY_PLAN])
    service = GangOf8Service(
        data_dir=tmp_path / "data",
        role_agents={Role.architect: "gemini"},
        panel=["gemini"],
    )
    service.registry.register(planner)
    started: list[str] = []
    monkeypatch.setattr(
        service,
        "_start_ready_packages",
        lambda current, background: started.append(current.goal_id),
    )

    goal = service.create_goal(
        "Build and deliver a complete single-file HTML arcade application")

    template_error = "wp_3 TEMPLATE must be OWNER or one of REQUIRES"
    integration_error = (
        "wp_3 assembly follows multiple runtime producers without a hard-after "
        "non-assembly integration/QA package"
    )
    assert goal.status == "running"
    assert goal.planned_by == "gemini"
    assert len(planner.prompts) == 2
    assert planner.roles == [Role.architect, Role.architect]
    assert planner.timeouts == [config.GOAL_PLAN_TIMEOUT, config.GOAL_PLAN_TIMEOUT]
    assert template_error in planner.prompts[1]
    assert integration_error in planner.prompts[1]
    assert INVALID_ASSEMBLY_PLAN.strip() in planner.prompts[1]
    assert "COMPLETE REPLACEMENT PLAN" in planner.prompts[1]
    assert goal.plan_rationale == (
        "planner contract repaired automatically after 1 rejected attempt")
    assert goal.last_error == ""
    assert goal.epoch == 1
    assert started == [goal.goal_id]
    assert len(goal.milestones) == 4
    assert goal.milestones[2].depends_on == [0, 1]
    assert set(goal.milestones[3].depends_on) == {0, 1, 2}
    assert goal.milestones[3].assembly_template == "shell.html"
    assert "shell.html" in goal.milestones[3].dependencies

    events = [
        json.loads(line)["event"]
        for line in service.store.session_log_path("-").read_text(encoding="utf-8").splitlines()
    ]
    assert events.index("goal_plan_repair_requested") < events.index("goal_plan_repaired")
    assert events.index("goal_plan_repaired") < events.index("goal_planned")


def test_invalid_plan_repair_is_bounded_and_never_starts_packages(tmp_path, monkeypatch):
    planner = _SequencedPlannerSeat([INVALID_ASSEMBLY_PLAN])
    service = GangOf8Service(
        data_dir=tmp_path / "data",
        role_agents={Role.architect: "gemini"},
        panel=["gemini"],
    )
    service.registry.register(planner)
    started: list[str] = []
    monkeypatch.setattr(
        service,
        "_start_ready_packages",
        lambda current, background: started.append(current.goal_id),
    )

    goal = service.create_goal(
        "Build and deliver a complete single-file HTML arcade application")

    assert goal.status == "paused"
    assert goal.milestones == []
    assert started == []
    assert len(planner.prompts) == 1 + config.GOAL_PLAN_REPAIR_ATTEMPTS
    assert all(timeout == config.GOAL_PLAN_TIMEOUT for timeout in planner.timeouts)
    assert "TEMPLATE must be OWNER or one of REQUIRES" in goal.last_error
    assert "without a hard-after non-assembly integration/QA package" in goal.plan_rationale
    assert service.store.list_sessions() == []

    events = [
        json.loads(line)["event"]
        for line in service.store.session_log_path("-").read_text(encoding="utf-8").splitlines()
    ]
    assert events.count("goal_plan_repair_requested") == config.GOAL_PLAN_REPAIR_ATTEMPTS
    assert "goal_plan_repaired" not in events
    assert "goal_planned" not in events


def test_cancel_during_plan_repair_cannot_resurrect_or_start_goal(tmp_path, monkeypatch):
    planner = _BlockingRepairPlannerSeat(
        [INVALID_ASSEMBLY_PLAN, INVALID_ASSEMBLY_PLAN, REPAIRED_ASSEMBLY_PLAN])
    service = GangOf8Service(
        data_dir=tmp_path / "data",
        role_agents={Role.architect: "gemini"},
        panel=["gemini"],
    )
    service.registry.register(planner)
    started: list[str] = []
    monkeypatch.setattr(
        service,
        "_start_ready_packages",
        lambda current, background: started.append(current.goal_id),
    )

    created = service.create_goal(
        "Build and deliver a complete single-file HTML arcade application",
        background=True,
    )
    assert planner.repair_started.wait(timeout=2)

    cancelled = service.cancel_goal(created.goal_id)
    planner.release_repair.set()
    service._pool.shutdown(wait=True)

    goal = service.goals.get(created.goal_id)
    assert cancelled["status"] == "cancelled"
    assert goal is not None and goal.status == "cancelled"
    assert goal.milestones == []
    assert started == []
    assert len(planner.prompts) == 2
    assert service.store.list_sessions() == []


def test_build_team_assigns_distinct_enabled_owners(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data", panel=["claude", "codex", "gemini"])
    packages = [
        GoalMilestone(index=i, title=f"p{i}", task_text="work", contract_declared=True)
        for i in range(3)
    ]
    normalized, errors = service._normalize_work_packages(packages)
    assert not errors
    assert [p.owner for p in normalized] == ["claude", "codex", "gemini"]


def test_single_file_goal_infers_only_final_html_when_planner_omits_release(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data", panel=["claude"])
    packages = [
        GoalMilestone(
            index=0, title="integrate", task_text="assemble", owner="claude",
            required_files=["arcade.html", "tools/build.mjs", "README.md"],
            contract_declared=True,
        )
    ]
    normalized, errors = service._normalize_work_packages(
        packages, "Build one complete single-file HTML application")
    assert not errors
    assert normalized[0].release_files == ["arcade.html"]


def test_release_manifest_never_includes_internal_package_outputs(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(text="single file", milestones=[
        GoalMilestone(
            index=0, title="source", task_text="build modules",
            required_files=["src/core.js", "tools/build.mjs"], release_files=[]),
        GoalMilestone(
            index=1, title="integrate", task_text="assemble",
            required_files=["arcade.html", "qa/report.txt"],
            release_files=["arcade.html"], release_declared=True),
    ])
    assert service._goal_release_files(goal) == ["arcade.html"]


def test_release_file_must_be_owned_by_the_same_package(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    packages = [GoalMilestone(
        index=0, package_id="wp_1", owner="claude", title="bad", task_text="bad",
        required_files=["internal.js"], release_files=["arcade.html"],
        release_declared=True, contract_declared=True)]
    _normalized, errors = service._normalize_work_packages(packages)
    assert errors == ["wp_1 RELEASE is not owned by OUTPUTS: arcade.html"]


def test_build_plan_cannot_explicitly_release_nothing(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    packages = [GoalMilestone(
        index=0, package_id="wp_1", owner="claude", title="internal", task_text="build",
        required_files=["internal.js"], release_files=[], release_declared=True,
        contract_declared=True)]
    _normalized, errors = service._normalize_work_packages(
        packages, "Build a complete JavaScript application")
    assert errors == ["build plan declares no final RELEASE files"]


def test_package_scheduler_starts_every_ready_branch(tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="parallel build", status="running", collaboration_mode="build_team",
        delivery_mode="final_batch", epoch=1,
        milestones=[
            GoalMilestone(index=0, title="a", task_text="a", contract_declared=True),
            GoalMilestone(index=1, title="b", task_text="b", contract_declared=True,
                          depends_on=[0]),
            GoalMilestone(index=2, title="c", task_text="c", contract_declared=True),
        ])
    service.goals.save(goal)
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_milestone",
        lambda current, index, background: started.append(index))
    service._start_ready_packages(goal, background=True)
    assert started == [0, 2]


def test_contract_linked_packages_start_before_provider_finishes(tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="parallel contract build", status="running", collaboration_mode="build_team",
        delivery_mode="final_batch", epoch=1,
        milestones=[
            GoalMilestone(index=0, package_id="wp_1", owner="claude", title="runtime",
                          task_text="runtime", contract_declared=True),
            GoalMilestone(index=1, package_id="wp_2", owner="codex", title="client",
                          task_text="client", contract_declared=True,
                          contract_depends_on=[0]),
            GoalMilestone(index=2, package_id="wp_3", owner="gemini", title="integration",
                          task_text="integration", contract_declared=True, depends_on=[0, 1]),
        ])
    service.goals.save(goal)
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_milestone",
        lambda current, index, background: started.append(index))
    service._start_ready_packages(goal, background=True)
    assert started == [0, 1]


def test_hard_file_requirement_still_infers_blocking_provider(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data", panel=["claude", "codex"])
    packages = [
        GoalMilestone(index=0, title="schema", task_text="schema", owner="claude",
                      required_files=["generated/schema.json"], contract_declared=True),
        GoalMilestone(index=1, title="migration", task_text="migration", owner="codex",
                      dependencies=["generated/schema.json"], contract_depends_on=[0],
                      contract_declared=True),
    ]
    normalized, errors = service._normalize_work_packages(packages)
    assert not errors
    assert normalized[1].depends_on == [0]
    assert normalized[1].contract_depends_on == []


def test_runtime_contract_is_promoted_to_real_byte_dependency(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data", panel=["claude", "codex"])
    packages = [
        GoalMilestone(index=0, title="runtime", task_text="runtime", owner="claude",
                      required_files=["src/core.js"], contract_declared=True),
        GoalMilestone(index=1, title="game", task_text="game", owner="codex",
                      required_files=["src/game.js"], contract_depends_on=[0],
                      contract_declared=True),
    ]

    normalized, errors = service._normalize_work_packages(packages)

    assert not errors
    assert normalized[1].depends_on == [0]
    assert normalized[1].contract_depends_on == []
    assert normalized[1].dependencies == ["src/core.js"]


def test_broad_build_plan_repairs_duplicate_owners_to_use_every_enabled_ai(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data", panel=["alpha", "beta", "gamma"])
    packages = [
        GoalMilestone(index=0, title="a", task_text="a", owner="alpha",
                      required_files=["a.py"], contract_declared=True),
        GoalMilestone(index=1, title="b", task_text="b", owner="alpha",
                      required_files=["b.py"], contract_declared=True),
        GoalMilestone(index=2, title="c", task_text="c", owner="beta",
                      required_files=["c.py"], contract_declared=True),
    ]

    normalized, errors = service._normalize_work_packages(packages, "Build an application")

    assert not errors
    assert {package.owner for package in normalized} == {"alpha", "beta", "gamma"}


def test_runtime_graph_cannot_flow_directly_into_zero_call_assembly(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data", panel=["alpha", "beta", "gamma"])
    packages = [
        GoalMilestone(index=0, package_id="wp_1", title="core", task_text="core",
                      owner="alpha", required_files=["src/core.js"], contract_declared=True),
        GoalMilestone(index=1, package_id="wp_2", title="game", task_text="game",
                      owner="beta", required_files=["src/game.js"], contract_declared=True),
        GoalMilestone(index=2, package_id="wp_3", title="release", task_text="assemble",
                      owner="gamma", required_files=["index.html"], release_files=["index.html"],
                      release_declared=True, dependencies=["src/core.js", "src/game.js"],
                      assembly_mode=assembly.HTML_INLINE, assembly_template=assembly.OWNER_TEMPLATE,
                      contract_declared=True),
    ]

    _normalized, errors = service._normalize_work_packages(packages, "Build an arcade application")

    assert any("without a hard-after non-assembly integration/QA package" in error
               for error in errors)


def test_hard_after_integration_package_satisfies_runtime_release_graph(tmp_path):
    service = GangOf8Service(
        data_dir=tmp_path / "data", panel=["alpha", "beta", "gamma"])
    packages = [
        GoalMilestone(index=0, package_id="wp_1", title="core", task_text="core",
                      owner="alpha", required_files=["src/core.js"], contract_declared=True),
        GoalMilestone(index=1, package_id="wp_2", title="game", task_text="game",
                      owner="beta", required_files=["src/game.js"], contract_depends_on=[0],
                      contract_declared=True),
        GoalMilestone(index=2, package_id="wp_3", title="Integration QA",
                      task_text="Verify the combined runtime", owner="gamma",
                      required_files=["src/integration.js"], contract_depends_on=[0, 1],
                      contract_declared=True),
        GoalMilestone(index=3, package_id="wp_4", title="release", task_text="assemble",
                      owner="gamma", required_files=["index.html"], release_files=["index.html"],
                      release_declared=True,
                      dependencies=["src/core.js", "src/game.js", "src/integration.js"],
                      assembly_mode=assembly.HTML_INLINE, assembly_template=assembly.OWNER_TEMPLATE,
                      contract_declared=True),
    ]

    normalized, errors = service._normalize_work_packages(packages, "Build an arcade application")

    assert not errors
    assert normalized[2].depends_on == [0, 1]


def test_contract_linked_owner_receives_pending_interface_without_future_bytes():
    goal = Goal(text="Build an arcade", collaboration_mode="build_team", milestones=[
        GoalMilestone(index=0, title="Runtime", task_text="Build runtime", owner="claude",
                      status="running", required_files=["src/core.js"],
                      interface_contract="PROVIDES ARC.Game and ARC.registerGame"),
        GoalMilestone(index=1, title="Invaders", task_text="Build game", owner="codex",
                      contract_depends_on=[0], required_files=["src/invaders.js"],
                      interface_contract="CONSUMES ARC.Game"),
    ])
    text = goals_mod.compose_milestone_task(goal, 1)
    assert "NON-BLOCKING INTERFACE INPUTS" in text
    assert "PROVIDES ARC.Game and ARC.registerGame" in text
    assert "may still be working" in text
    assert "OTHER OWNED PACKAGES" in text
    assert "status running" not in text
    assert "(running)" not in text
    assert "not live status indicators" in text


def test_rejected_package_receives_concrete_retry_failure():
    goal = Goal(text="Build an arcade", collaboration_mode="build_team", milestones=[
        GoalMilestone(index=0, title="Runtime", task_text="Build runtime", owner="claude",
                      acceptance_detail="pointer x/y never updates from mouse input"),
    ])

    text = goals_mod.compose_milestone_task(goal, 0)

    assert "RETRY CORRECTION" in text
    assert "pointer x/y never updates" in text


def test_compose_milestone_task_frames_scope():
    goal = Goal(text="Build the whole thing", milestones=[
        GoalMilestone(index=0, title="Data layer", task_text="Build storage.",
                      status="done", files=["C:/ws/storage.py"], summary="Shipped storage."),
        GoalMilestone(index=1, title="API", task_text="Build the API."),
        GoalMilestone(index=2, title="UI", task_text="Build the UI."),
    ])
    text = goals_mod.compose_milestone_task(goal, 1)
    assert "[GOAL MILESTONE 2/3] API" in text
    assert "Build the whole thing" in text            # overall goal
    assert "Data layer" in text and "storage.py" in text  # completed work
    assert "Shipped storage." in text                 # prior outcome
    assert "Build the API." in text                   # the actual task
    assert "UI" in text and "OUT of scope" in text    # later work fenced off


# ---- the full loop on the mock backend ----------------------------------------


def test_goal_runs_all_milestones_and_completes(svc):
    goal = svc.create_goal("/goal Decide storage and retention for session logs")
    assert goal.status == "completed"
    assert goal.planned_by == "mock"
    assert [m.status for m in goal.milestones] == ["done", "done"]
    # each milestone ran as a REAL session tagged back to the goal
    for i, m in enumerate(goal.milestones):
        data = svc.get(m.session_id)
        assert data is not None
        assert data["status"] == "done"
        assert data["goal_id"] == goal.goal_id
        assert data["goal_milestone"] == i
        assert m.summary  # the final answer flowed into the milestone record
    # milestone 2's session was framed with milestone 1's outcome
    second = svc.get(goal.milestones[1].session_id)
    assert "AVAILABLE IN THE SHARED GOAL STAGING WORKSPACE" in second["task"]["text"]
    assert "Storage decision" in second["task"]["text"]
    view = svc.get_goal(goal.goal_id)
    assert view["build_roster"] == ["mock"]
    assert view["contributing_agents"] == ["mock"]
    assert view["participation_complete"] is True


def test_build_package_round_names_owner_instead_of_claiming_full_panel(svc):
    goal = svc.create_goal("/goal Decide storage and retention for session logs")
    first = svc.manager.load(goal.milestones[0].session_id)
    assert first is not None and first.rounds
    assert "build package" in first.rounds[0].goal
    assert "owner mock" in first.rounds[0].goal
    assert first.rounds[0].agents == [Role.panelist]
    assert "every seat contributes" not in first.rounds[0].goal


def test_unparseable_plan_degrades_to_single_milestone(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path / "data")
    svc.registry.register(_PlannerSeat(plan="I would start with the backend."))
    goal = svc.create_goal("Compare SQLite vs JSON and recommend one")
    assert goal.status == "completed"
    assert len(goal.milestones) == 1
    assert "analysis-only milestone" in goal.plan_rationale


def test_resume_empty_delivery_goal_replans_instead_of_releasing(tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="Build and deliver index.html into C:\\tmp",
        status="paused",
        collaboration_mode="build_team",
        delivery_mode="final_batch",
        milestones=[],
        last_error="planner did not produce a delivery contract",
        build_roster=["mock"],
    )
    service.goals.save(goal)
    plan = (
        "PACKAGE 1: Web app\nOWNER: mock\nAFTER: NONE\nCONTRACTS: NONE\n"
        "TASK: Build and deliver index.html.\nOUTPUTS: index.html\n"
        "RELEASE: index.html\nREQUIRES: NONE\n"
        "INTERFACE: PROVIDES the complete web app\n"
    )
    service.registry.register(_PlannerSeat(plan=plan))
    started = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.append(current.goal_id),
    )

    out = service.resume_goal(goal.goal_id, background=False)

    assert out["status"] == "running"
    assert len(out["milestones"]) == 1
    assert out["release_status"] == "not_started"
    assert started == [goal.goal_id]


def test_delivery_goal_with_empty_release_fails_closed(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="Build and deliver index.html into C:\\tmp",
        status="running",
        collaboration_mode="build_team",
        delivery_mode="final_batch",
        staging_root=str(tmp_path / "empty-stage"),
        milestones=[],
    )

    service._prepare_goal_release(goal)

    assert goal.status == "paused"
    assert goal.release_status == "failed"
    assert goal.release_files == []
    assert goal.last_error == "final release has no verified output files"


def test_cancelled_milestone_pauses_goal_and_resume_retries(svc):
    # a goal mid-flight whose current milestone session got cancelled
    goal = Goal(text="Decide storage and retention", status="running", milestones=[
        GoalMilestone(index=0, title="Storage decision",
                      task_text="Compare SQLite vs plain JSON and recommend one.",
                      status="running", session_id="s_dead", contract_declared=True),
        GoalMilestone(index=1, title="Retention decision",
                      task_text="Recommend a retention policy.", contract_declared=True),
    ])
    svc.goals.save(goal)
    dead = Session(session_id="s_dead", status=SessionStatus.cancelled,
                   goal_id=goal.goal_id, goal_milestone=0,
                   task=Task(task_id="t", session_id="s_dead", text="x"))
    svc._maybe_advance_goal(dead)
    paused = svc.goals.get(goal.goal_id)
    assert paused.status == "paused"
    assert "cancelled" in paused.last_error
    assert paused.milestones[0].status == "pending"  # retryable
    # resume retries milestone 1 with a FRESH session and runs to completion
    out = svc.resume_goal(goal.goal_id, background=False)
    assert out["status"] == "completed"
    assert out["milestones"][0]["session_id"] != "s_dead"
    assert [m["status"] for m in out["milestones"]] == ["done", "done"]


def test_resume_preserves_epoch_and_live_siblings_and_starts_every_ready_retry(
        tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="parallel", status="paused", epoch=7,
        collaboration_mode="build_team", delivery_mode="final_batch",
        milestones=[
            GoalMilestone(index=0, package_id="wp_1", owner="claude", title="failed",
                          task_text="retry", status="failed", session_id="s_failed",
                          contract_declared=True),
            GoalMilestone(index=1, package_id="wp_2", owner="codex", title="healthy",
                          task_text="continue", status="running", session_id="s_live",
                          contract_declared=True),
            GoalMilestone(index=2, package_id="wp_3", owner="gemini", title="ready",
                          task_text="start", status="pending", contract_declared=True),
        ],
    )
    service.goals.save(goal)
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_milestone",
        lambda current, index, background: started.append(index))
    out = service.resume_goal(goal.goal_id)
    assert out["epoch"] == 7
    assert out["milestones"][1]["session_id"] == "s_live"
    assert out["milestones"][1]["status"] == "running"
    assert started == [0, 2]


def test_resume_adopts_verified_completed_attempt_before_spending_again(
        tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    goal = Goal(
        text="recover", status="paused", epoch=4, staging_root=str(stage),
        collaboration_mode="build_team", delivery_mode="final_batch",
        milestones=[
            GoalMilestone(index=0, package_id="wp_1", owner="qwen", title="audio",
                          task_text="audio", status="failed", session_id="s_lost",
                          required_files=["src/audio.js"], contract_declared=True,
                          requires_delivery=True),
            GoalMilestone(index=1, package_id="wp_2", owner="claude", title="integrate",
                          task_text="integrate", status="pending", depends_on=[0],
                          contract_declared=True),
        ],
    )
    service.goals.save(goal)
    source = tmp_path / "recovered.js"
    source.write_text("globalThis.audio = true;\n", encoding="utf-8")
    from gangof8.models import FinalAnswer, ProposedAction
    prior = Session(
        session_id="s_verified", status=SessionStatus.done, outcome="succeeded",
        goal_id=goal.goal_id, goal_epoch=1, goal_milestone=0,
        collaboration_mode="build_team", delivery_mode="final_batch",
        work_package_id="wp_1", work_package_owner="qwen",
        required_files=["src/audio.js"],
        verified_output_hashes={
            "src/audio.js": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        task=Task(task_id="t", session_id="s_verified", text="audio"),
        final=FinalAnswer(answer="audio complete", confidence="high"),
        proposed_actions=[ProposedAction(
            session_id="s_verified", kind="write_file", role=Role.implementer,
            filename="src/audio.js", status="executed", result_path=str(source))],
    )
    service.store.save_session(prior)
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_milestone",
        lambda current, index, background: started.append(index))
    out = service.resume_goal(goal.goal_id)
    assert out["milestones"][0]["status"] == "done"
    assert out["milestones"][0]["session_id"] == "s_verified"
    assert (stage / "src" / "audio.js").read_text(encoding="utf-8").startswith("globalThis")
    assert started == [1]


def test_recovery_rejects_completed_attempt_whose_verified_bytes_changed(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    goal = Goal(
        text="recover", status="paused", staging_root=str(stage),
        collaboration_mode="build_team", delivery_mode="final_batch",
        milestones=[GoalMilestone(
            index=0, package_id="wp_1", owner="qwen", title="audio",
            task_text="audio", status="failed", required_files=["src/audio.js"],
            contract_declared=True, requires_delivery=True,
        )],
    )
    service.goals.save(goal)
    source = tmp_path / "result.js"
    source.write_text("verified", encoding="utf-8")
    verified = hashlib.sha256(source.read_bytes()).hexdigest()
    from gangof8.models import ProposedAction
    prior = Session(
        session_id="s_tampered", status=SessionStatus.done, outcome="succeeded",
        goal_id=goal.goal_id, collaboration_mode="build_team", delivery_mode="final_batch",
        work_package_id="wp_1", work_package_owner="qwen",
        required_files=["src/audio.js"], verified_output_hashes={"src/audio.js": verified},
        task=Task(task_id="t", session_id="s_tampered", text="audio"),
        proposed_actions=[ProposedAction(
            session_id="s_tampered", kind="write_file", role=Role.implementer,
            filename="src/audio.js", status="executed", result_path=str(source),
        )],
    )
    service.store.save_session(prior)
    source.write_text("changed after verification", encoding="utf-8")

    assert service._recover_verified_goal_packages(goal.goal_id) == []
    assert not (stage / "src" / "audio.js").exists()


def test_stale_session_from_retried_milestone_cannot_advance(svc):
    goal = Goal(text="g", status="running", milestones=[
        GoalMilestone(index=0, title="only", task_text="t",
                      status="running", session_id="s_new", contract_declared=True),
    ])
    svc.goals.save(goal)
    stale = Session(session_id="s_old", status=SessionStatus.done,
                    goal_id=goal.goal_id, goal_milestone=0,
                    task=Task(task_id="t", session_id="s_old", text="x"))
    svc._maybe_advance_goal(stale)
    assert svc.goals.get(goal.goal_id).status == "running"  # untouched


def test_failed_verification_is_terminal_and_never_reported_as_done(svc):
    """Regression for the live run: validation failure is not success."""
    goal = Goal(text="g", status="running", milestones=[
        GoalMilestone(index=0, title="core", task_text="build core", status="running",
                      session_id="s_failed", required_files=["core.js"],
                      contract_declared=True, requires_delivery=True),
        GoalMilestone(index=1, title="next", task_text="build next", contract_declared=True),
    ])
    svc.goals.save(goal)
    failed = Session(
        session_id="s_failed", status=SessionStatus.failed, outcome="failed_verification",
        goal_id=goal.goal_id, goal_milestone=0,
        task=Task(task_id="t", session_id="s_failed", text="build core"),
    )
    svc._maybe_advance_goal(failed)
    parked = svc.goals.get(goal.goal_id)
    assert parked.status == "paused"
    assert parked.current_index == 0
    assert parked.milestones[0].status == "failed"
    assert "failed" in parked.last_error


def test_parallel_failure_drains_live_sibling_before_parent_pauses(svc):
    goal = Goal(text="parallel", status="running", milestones=[
        GoalMilestone(index=0, package_id="wp_1", owner="claude", title="failed",
                      task_text="one", status="running", session_id="s_failed",
                      contract_declared=True),
        GoalMilestone(index=1, package_id="wp_2", owner="qwen", title="healthy",
                      task_text="two", status="running", session_id="s_healthy",
                      contract_declared=True),
    ])
    svc.goals.save(goal)
    failed = Session(
        session_id="s_failed", status=SessionStatus.failed, outcome="failed_verification",
        stop_reason="artifact failed", goal_id=goal.goal_id, goal_milestone=0,
        task=Task(task_id="t1", session_id="s_failed", text="one"),
    )
    svc._maybe_advance_goal(failed)
    draining = svc.goals.get(goal.goal_id)
    assert draining.status == "draining"
    assert [m.status for m in draining.milestones] == ["failed", "running"]

    healthy = Session(
        session_id="s_healthy", status=SessionStatus.done, outcome="succeeded",
        goal_id=goal.goal_id, goal_milestone=1,
        task=Task(task_id="t2", session_id="s_healthy", text="two"),
    )
    svc._maybe_advance_goal(healthy)
    parked = svc.goals.get(goal.goal_id)
    assert parked.status == "paused"
    assert [m.status for m in parked.milestones] == ["failed", "done"]


def test_goal_context_uses_only_promoted_accepted_files(svc, tmp_path):
    goal = Goal(text="g", status="running", milestones=[
        GoalMilestone(index=0, title="core", task_text="build core", status="running",
                      session_id="s_ok", required_files=["core.js"],
                      contract_declared=True, requires_delivery=True),
        GoalMilestone(index=1, title="next", task_text="build next", contract_declared=True),
    ])
    svc.goals.save(goal)
    delivered = tmp_path / "core.js"
    delivered.write_text("export const core = true;\n", encoding="utf-8")
    finished = Session(
        session_id="s_ok", status=SessionStatus.done, outcome="succeeded",
        goal_id=goal.goal_id, goal_milestone=0,
        task=Task(task_id="t", session_id="s_ok", text="build core"),
    )
    from gangof8.models import ProposedAction, Role
    # Scratch panel paths are intentionally present but must never become goal context.
    finished.files_changed = ["C:/sandbox/codex__core.js", str(delivered)]
    finished.proposed_actions = [
        ProposedAction(session_id="s_ok", kind="write_file", role=Role.panelist,
                       filename="codex__core.js", content="draft", status="executed"),
        ProposedAction(session_id="s_ok", kind="promote", role=Role.implementer,
                       filename="core.js", status="executed", result_path=str(delivered)),
    ]
    svc._maybe_advance_goal(finished)
    updated = svc.goals.get(goal.goal_id)
    assert updated.milestones[0].accepted_files == [str(delivered)]
    assert updated.milestones[0].files == [str(delivered)]
    assert "codex__core.js" not in goals_mod.compose_milestone_task(updated, 1)


def test_restart_parks_inflight_goals_as_paused(tmp_path):
    data = tmp_path / "data"
    svc1 = GangOf8Service(data_dir=data)
    goal = Goal(text="long build", status="running", milestones=[
        GoalMilestone(index=0, title="m1", task_text="t", status="running",
                      session_id="s_gone", contract_declared=True),
        GoalMilestone(index=1, title="m2", task_text="t", status="running",
                      session_id="s_also_gone", contract_declared=True),
    ])
    svc1.goals.save(goal)
    svc2 = GangOf8Service(data_dir=data)  # simulated restart
    parked = svc2.goals.get(goal.goal_id)
    assert parked.status == "paused"
    assert "restart" in parked.last_error
    assert [m.status for m in parked.milestones] == ["pending", "pending"]
    assert [m.session_id for m in parked.milestones] == [None, None]


def test_second_instance_does_not_park_goal_while_first_is_live(tmp_path, monkeypatch):
    """A second launch (double-clicked launcher, an accidental duplicate
    `serve`) constructs its own Service object BEFORE it ever tries to bind
    the port and fails — so without this guard, the mere act of launching a
    redundant process is enough to park an in-flight goal the real,
    already-running server is actively working on. Only a real standalone
    start (no injected data_dir) probes the port; see the guard in
    GangOf8Service.__init__."""
    import socket

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)

    svc1 = GangOf8Service()  # the real, already-running dashboard server
    goal = Goal(text="long build", status="running", milestones=[
        GoalMilestone(index=0, title="m1", task_text="t", status="running",
                      session_id="s_gone", contract_declared=True),
    ])
    svc1.goals.save(goal)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    monkeypatch.setenv("GANGOF8_PORT", str(port))
    try:
        svc2 = GangOf8Service()  # simulated accidental double-launch
    finally:
        listener.close()

    survived = svc2.goals.get(goal.goal_id)
    assert survived.status == "running", "an active owner is live — must not be parked"


def test_goal_api_aggregates_package_attempts_and_blockers(tmp_path):
    from gangof8.models import ApprovalRequest

    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="build", status="running", collaboration_mode="build_team",
        delivery_mode="final_batch", milestones=[GoalMilestone(
            index=0, package_id="wp_1", owner="claude", title="core", task_text="core",
            status="running", session_id="s_active", contract_declared=True)])
    service.goals.save(goal)
    old = Session(
        session_id="s_old", status=SessionStatus.failed,
        goal_id=goal.goal_id, work_package_id="wp_1", work_package_owner="claude",
        active_agent_calls=[{"call_id": "stale_terminal_call"}],
        task=Task(task_id="t0", session_id="s_old", text="core"),
    )
    service.store.save_session(old)
    session = Session(
        session_id="s_active", status=SessionStatus.awaiting_approval,
        goal_id=goal.goal_id, work_package_id="wp_1", work_package_owner="claude",
        active_agent_calls=[{
            "call_id": "call_1", "agent": "claude", "role": "panelist",
            "started_at": "2026-07-13T22:00:00Z", "timeout_s": 320,
        }],
        agent_calls=1,
        agent_call_attempts=2,
        agent_attempt_duration_ms=360_000,
        package_output_authors={"src/core.js": "claude", "src/view.js": "gemini"},
        package_output_attempts={"src/core.js": 2, "src/view.js": 1},
        package_output_history={
            "src/core.js": [
                {"attempt": 1, "agent": "claude", "kind": "primary", "status": "failed"},
                {"attempt": 2, "agent": "gemini", "kind": "failover", "status": "completed"},
            ]
        },
        package_call_failures={"claude": "timed out"},
        package_started_at="2026-07-13T22:04:00+00:00",
        package_deadline_at="2026-07-13T22:10:00+00:00",
        approvals=[ApprovalRequest(
            session_id="s_active", action="release", category="file_write")],
        task=Task(task_id="t", session_id="s_active", text="core"),
    )
    service.store.save_session(session)
    view = service.get_goal(goal.goal_id)
    assert view["display_status"] == "awaiting_approval"
    assert view["pending_approvals"] == 1
    assert view["active_agent_calls"] == 1
    assert view["actionable_session_id"] == "s_active"
    package = view["milestones"][0]
    assert package["attempt_count"] == 2
    assert [attempt["session_id"] for attempt in package["attempts"]] == ["s_old", "s_active"]
    assert package["attempts"][0]["is_current"] is False
    assert package["attempts"][1]["is_current"] is True
    assert package["agent_call_attempts"] == 2
    assert package["agent_attempt_duration_ms"] == 360_000
    assert package["output_authors"]["src/view.js"] == "gemini"
    assert package["output_attempts"]["src/core.js"] == 2
    assert package["output_history"]["src/core.js"][0]["agent"] == "claude"
    assert package["attempts"][1]["output_history"]["src/core.js"][1]["agent"] == "gemini"
    assert package["author_failures"]["claude"] == "timed out"
    assert package["authoring_deadline_at"].endswith("+00:00")
    assert view["agent_call_attempts"] == 2
    assert view["agent_attempt_duration_ms"] == 360_000


def test_assembly_template_failure_reopens_only_its_upstream_owner(tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="assemble",
        status="running",
        epoch=4,
        current_index=1,
        collaboration_mode="build_team",
        delivery_mode="final_batch",
        milestones=[
            GoalMilestone(
                index=0,
                package_id="wp_template",
                owner="codex",
                title="template",
                task_text="template",
                status="done",
                session_id="s_bad_template",
                contract_declared=True,
                requires_delivery=True,
                required_files=["index.template.html"],
                accepted_files=["stage/index.template.html"],
                accepted_hashes={"index.template.html": "bad"},
            ),
            GoalMilestone(
                index=1,
                package_id="wp_assembly",
                owner="codex",
                title="assembly",
                task_text="assembly",
                status="running",
                session_id="s_assembly",
                depends_on=[0],
                contract_declared=True,
                requires_delivery=True,
                required_files=["arcade.html"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_bad_template",
        status=SessionStatus.done,
        outcome="succeeded",
        goal_id=goal.goal_id,
        goal_epoch=4,
        goal_milestone=0,
        work_package_id="wp_template",
        work_package_owner="codex",
        required_files=["index.template.html"],
        task=Task(task_id="t_bad", session_id="s_bad_template", text="template"),
    ))
    failed = Session(
        session_id="s_assembly",
        status=SessionStatus.failed,
        outcome="failed_verification",
        goal_id=goal.goal_id,
        goal_epoch=4,
        goal_milestone=1,
        work_package_id="wp_assembly",
        work_package_owner="codex",
        assembly_mode=assembly.HTML_INLINE,
        assembly_template="index.template.html",
        quality_gate={
            "verdict": "FAIL",
            "stage": "deterministic_assembly",
            "detail": "directive is nested inside <script>",
        },
        stop_reason="deterministic assembly contract failed",
        task=Task(task_id="t_assembly", session_id="s_assembly", text="assembly"),
    )
    scheduled: list[int] = []
    monkeypatch.setattr(
        service,
        "_start_ready_packages",
        lambda current, background: scheduled.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    service._maybe_advance_goal(failed)

    repairing = service.goals.get(goal.goal_id)
    assert repairing.status == "running"
    assert [package.status for package in repairing.milestones] == ["pending", "pending"]
    provider = repairing.milestones[0]
    assert provider.invalidated_session_ids == ["s_bad_template"]
    assert provider.accepted_hashes == {}
    assert repairing.current_index == 0
    assert scheduled == [0]

    service.goals.park_active(goal.goal_id, "simulated restart")
    assert service._recover_verified_goal_packages(goal.goal_id) == []
    still_paused = service.goals.get(goal.goal_id)
    assert still_paused.milestones[0].status == "pending"

    reopened = service.goals.resume(goal.goal_id)
    assert [package.status for package in reopened.milestones] == ["pending", "pending"]
    assert reopened.milestones[0].invalidated_session_ids == ["s_bad_template"]


def test_resume_reopens_legacy_non_self_contained_stylesheet_owner(
        tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    stylesheet_hash = hashlib.sha256(b"@import url('font.css');\n").hexdigest()
    goal = Goal(
        text="assemble", status="paused", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_styles", owner="gemini", title="styles",
                task_text="author styles", status="done", session_id="s_bad_styles",
                contract_declared=True, requires_delivery=True,
                required_files=["css/theme.css"],
                accepted_files=["stage/css/theme.css"],
                accepted_hashes={"css/theme.css": stylesheet_hash},
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="failed", session_id="s_assembly",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["arcade.html"],
                dependencies=["index.template.html", "css/theme.css"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_assembly", status=SessionStatus.failed,
        outcome="failed_verification", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", assembly_mode=assembly.HTML_INLINE,
        assembly_template="index.template.html",
        runtime_dependencies=["index.template.html", "css/theme.css"],
        quality_gate={
            "verdict": "FAIL", "stage": "deterministic_assembly",
            "detail": (
                "inline stylesheet contains @import and is not self-contained: "
                "css/theme.css"
            ),
        },
        stop_reason="deterministic assembly contract failed",
        task=Task(task_id="t_assembly", session_id="s_assembly", text="assembly"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done"
                    for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    assert started == [0]
    provider = service.goals.get(goal.goal_id).milestones[0]
    assert provider.status == "pending"
    assert provider.session_id is None
    assert provider.invalidated_session_ids == ["s_bad_styles"]
    assert provider.accepted_hashes == {}


def test_resume_reopens_provider_that_breaks_assembled_runtime(tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    (stage / "js").mkdir(parents=True)
    input_source = (
        "addEventListener('keydown',()=>{if(window.ArcadePortal&&window.ArcadePortal.input)"
        "window.ArcadePortal.input.actions.fire=true;});\n"
    )
    portal_source = "window.ArcadePortal={input:{held:{}}};\n"
    (stage / "js" / "input.js").write_text(input_source, encoding="utf-8")
    (stage / "js" / "portal.js").write_text(portal_source, encoding="utf-8")
    goal = Goal(
        text="assemble", status="paused", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_core", owner="codex", title="core",
                task_text="author core", status="done", session_id="s_bad_core",
                contract_declared=True, requires_delivery=True,
                required_files=["js/input.js", "js/portal.js"],
                accepted_files=[str(stage / "js" / "input.js"), str(stage / "js" / "portal.js")],
                accepted_hashes={
                    "js/input.js": hashlib.sha256(input_source.encode()).hexdigest(),
                    "js/portal.js": hashlib.sha256(portal_source.encode()).hexdigest(),
                },
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="failed", session_id="s_assembly_runtime",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["arcade.html"],
                dependencies=["js/input.js", "js/portal.js"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.html",
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_assembly_runtime", status=SessionStatus.failed,
        outcome="failed_verification", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.html",
            "sources": ["js/input.js", "js/portal.js"],
        },
        unresolved=[
            "artifact verification failed: arcade.html: does not run â€” "
            "Cannot set properties of undefined (setting 'fire')"
        ],
        stop_reason="artifact verification failed; no file was delivered",
        task=Task(task_id="t_runtime", session_id="s_assembly_runtime", text="assembly"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    assert started == [0]
    repaired = service.goals.get(goal.goal_id)
    provider = repaired.milestones[0]
    assert provider.status == "pending"
    assert provider.session_id is None
    assert provider.invalidated_session_ids == ["s_bad_core"]
    assert provider.accepted_hashes == {}
    # Stack-based attribution blames js/input.js, not js/portal.js: the throw
    # (`.actions.fire = true`) is a line inside input.js's own keydown
    # callback. Bisection alone would blame portal.js merely because ITS
    # addition is what first makes the combined bundle fail (input.js's
    # `if (window.ArcadePortal && ...)` guard silently no-ops until portal.js
    # defines window.ArcadePortal) — but that only identifies the trigger,
    # not the file whose code actually threw. Either file is a defensible
    # place to add a guard/adapter for this interface mismatch; the hint
    # below still surfaces the ArcadePortal.input shape so a fix can land on
    # whichever side is correct.
    assert "js/input.js" in provider.acceptance_detail
    # Case-preserved: this text is repeated to the rebuilding model, and
    # identifiers like ArcadePortal must survive verbatim.
    assert "ArcadePortal.input paths [actions, actions.fire]" in provider.acceptance_detail
    persisted = Session.model_validate(service.store.load_session("s_assembly_runtime"))
    assert persisted.quality_gate["fault_scope"] == "dependency"
    assert persisted.quality_gate["fault_path"] == "js/input.js"


def test_assembly_runtime_failure_blames_file_that_defines_the_bug_not_the_bootstrap(tmp_path):
    """A bug can sit dormant — merely DEFINED, never invoked — in an early
    dependency until a later entry-point file's lifecycle wiring finally
    calls it. Cumulative bisection alone would blame that later file, since
    its addition is what first flips the combined bundle from clean to
    failing; it never distinguishes "whichever file's inclusion triggered
    execution" from "whichever file's own code threw". This is exactly the
    shape of a real bug that cost 133 rebuild attempts of an innocent
    integration file (`main.js`) before a human traced it by hand to a
    jagged pixel-art array in an already-accepted `renderer.js`: the crash
    lived inside a sprite-compiling function that only ran once the
    integration file's DOMContentLoaded handler called it.
    """
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    # early.js: defines a buggy function but never calls it itself — it just
    # sits there, syntactically valid, semantically broken.
    early_source = "function boom(){ var x; x.trim(); }\n"
    # late.js: the "bootstrap" — wires boom() to DOMContentLoaded, the way a
    # real main.js wires a renderer's init() to the page load lifecycle.
    late_source = "document.addEventListener('DOMContentLoaded', function(){ boom(); });\n"
    (stage / "early.js").write_text(early_source, encoding="utf-8")
    (stage / "late.js").write_text(late_source, encoding="utf-8")
    session = Session(
        session_id="s_bootstrap_runtime", status=SessionStatus.failed,
        workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.html",
            "sources": ["early.js", "late.js"],
        },
        task=Task(task_id="t_bootstrap", session_id="s_bootstrap_runtime", text="assembly"),
    )

    path, detail = service._assembly_runtime_failure_target(session)

    assert path == "early.js"
    assert "trim" in detail.lower() or "undefined" in detail.lower()


def test_assembly_fault_streak_pauses_goal_instead_of_looping_forever(tmp_path):
    """The same upstream package being blamed for the same fault repeatedly
    means attribution (or the underlying defect) isn't actually resolving
    it — rebuilding it again is not going to help. Cap it and hand off to a
    human instead of repeating indefinitely, the way one real build looped
    133 times relaunching the same doomed retry.
    """
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    (stage / "js").mkdir(parents=True)
    bad_source = "window.Thing = {};\n"
    (stage / "js" / "bad.js").write_text(bad_source, encoding="utf-8")
    goal = Goal(
        text="assemble", status="running", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_core", owner="codex", title="core",
                task_text="author core", status="done", session_id="s_core",
                contract_declared=True, requires_delivery=True,
                required_files=["js/bad.js"],
                accepted_files=[str(stage / "js" / "bad.js")],
                accepted_hashes={"js/bad.js": hashlib.sha256(bad_source.encode()).hexdigest()},
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="failed", session_id="s_assembly",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["arcade.html"], dependencies=["js/bad.js"],
                assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
            ),
        ],
    )
    session = Session(
        session_id="s_assembly", status=SessionStatus.failed,
        goal_id=goal.goal_id, workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
        quality_gate={
            "verdict": "FAIL", "stage": "deterministic_assembly",
            "detail": "assembly dependency changed after acceptance: js/bad.js",
            "fault_scope": "dependency", "fault_path": "js/bad.js",
        },
        task=Task(task_id="t_assembly", session_id="s_assembly", text="assemble"),
    )

    for _ in range(config.ASSEMBLY_FAULT_STREAK_LIMIT):
        provider = service._invalidate_assembly_input_provider(goal, session, 1)
        assert provider is not None
        assert goal.status == "running"

    provider = service._invalidate_assembly_input_provider(goal, session, 1)

    assert provider is None
    assert goal.status == "paused"
    assert "js/bad.js" in goal.last_error
    assert "package 1" in goal.last_error


def test_cancelled_goal_never_projects_running_packages_or_actionable_sessions(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="cancel", status="cancelled", milestones=[GoalMilestone(
            index=0, package_id="wp_1", owner="claude", title="core", task_text="core",
            status="running", session_id="s_old", contract_declared=True)])
    service.goals.save(goal)
    session = Session(
        session_id="s_old", status=SessionStatus.deliberating,
        goal_id=goal.goal_id, work_package_id="wp_1", work_package_owner="claude",
        active_agent_calls=[{"call_id": "stale"}],
        task=Task(task_id="t", session_id="s_old", text="core"))
    service.store.save_session(session)
    view = service.get_goal(goal.goal_id)
    assert view["display_status"] == "cancelled"
    assert view["milestones"][0]["status"] == "cancelled"
    assert view["active_packages"] == 0
    assert view["active_agent_calls"] == 0
    assert view["actionable_session_id"] is None


def test_revoked_worker_lease_rejects_stale_session_write(svc):
    """A hot-reload's old worker cannot resurrect a cancelled session."""
    session = svc.manager.create("write a report", source="test")
    token = svc.store.claim_worker_lease(session.session_id)
    assert token
    stale = svc.manager.load(session.session_id)
    stale.worker_lease = token
    svc.store.revoke_worker_lease(session.session_id)
    stale.stop_reason = "old worker finished late"
    assert svc.store.save_session(stale) is False
    fresh = svc.manager.load(session.session_id)
    assert fresh.stop_reason is None
    assert fresh.worker_lease == ""


# ---- promote gate inside a goal ------------------------------------------------


def test_goal_stages_every_package_then_uses_one_final_batch_approval(tmp_path):
    """Package writes never interrupt; the complete manifest has one gate."""
    est = tmp_path / "established"
    est.mkdir()
    plan = (
        f"MILESTONE 1: Ship the report\n"
        f"TASK: Write a short report recommending SQLite, delivered into {est}\n"
        "OUTPUTS: report.md\n"
        "RELEASE: report.md\n"
        f"MILESTONE 2: Retention decision\n"
        f"TASK: Recommend a retention policy for session logs.\n"
        "OUTPUTS: NONE\n"
        "RELEASE: NONE\n"
    )
    svc = GangOf8Service(data_dir=tmp_path / "data")
    svc.registry.register(_PlannerSeat(plan=plan, promoting=True))
    goal = svc.create_goal(f"Ship a storage report into {est}, then a retention policy")
    assert goal.status == "awaiting_release"
    ms1 = goal.milestones[0]
    sess = svc.manager.load(ms1.session_id)
    assert sess.status == SessionStatus.done
    assert not [a for a in sess.approvals if a.status == "pending"]
    assert not (est / "report.md").exists()
    assert (Path(goal.staging_root) / "report.md").exists()

    release = svc.manager.load(goal.release_session_id)
    assert release.status == SessionStatus.awaiting_approval
    pending = [a for a in release.approvals if a.status == "pending"]
    assert len(pending) == 1
    assert [a.kind for a in release.proposed_actions] == ["promote_batch"]
    release_action = release.proposed_actions[0]
    verified_hashes = json.loads(release_action.args["source_hashes"])
    assert verified_hashes["report.md"] == release.release_verified_hashes["report.md"]
    svc.approve(release.session_id, pending[0].approval_id, True)
    g = svc.goals.get(goal.goal_id)
    assert g.status == "completed"
    assert [m.status for m in g.milestones] == ["done", "done"]
    assert (est / "report.md").exists()
    assert hashlib.sha256((est / "report.md").read_bytes()).hexdigest() == verified_hashes["report.md"]
    provenance = g.milestones[0].output_provenance["report.md"]
    assert provenance["sha256"] == verified_hashes["report.md"]
    assert provenance["agent"] == "mock"
    assert provenance["method"] == "model_authored"


def test_final_batch_detects_destination_drift_before_writing(tmp_path):
    from gangof8 import executor
    from gangof8.models import ProposedAction, Role, Session, Task

    stage, dest = tmp_path / "stage", tmp_path / "dest"
    stage.mkdir(); dest.mkdir()
    (stage / "a.txt").write_text("new", encoding="utf-8")
    (dest / "a.txt").write_text("old", encoding="utf-8")
    baseline = hashlib.sha256(b"old").hexdigest()
    session = Session(
        session_id="s_batch", workspace_root=str(stage), established_root=str(dest),
        task=Task(task_id="t", session_id="s_batch", text="release"))
    action = ProposedAction(
        session_id="s_batch", kind="promote_batch", role=Role.implementer,
        args={"files": json.dumps(["a.txt"]),
              "baselines": json.dumps({"a.txt": baseline}),
              "source_hashes": json.dumps({
                  "a.txt": hashlib.sha256(b"new").hexdigest(),
              })})
    (dest / "a.txt").write_text("someone else's change", encoding="utf-8")
    with pytest.raises(executor.ExecutionError, match="project changed after final review"):
        executor.execute(session, action, tmp_path / "data")
    assert (dest / "a.txt").read_text(encoding="utf-8") == "someone else's change"


def test_final_batch_rejects_staging_drift_after_approval(tmp_path):
    from gangof8 import executor
    from gangof8.models import ProposedAction, Role, Session, Task

    stage, dest = tmp_path / "stage", tmp_path / "dest"
    stage.mkdir(); dest.mkdir()
    (stage / "a.txt").write_text("verified", encoding="utf-8")
    verified = hashlib.sha256(b"verified").hexdigest()
    session = Session(
        session_id="s_stage_drift", workspace_root=str(stage), established_root=str(dest),
        task=Task(task_id="t", session_id="s_stage_drift", text="release"))
    action = ProposedAction(
        session_id="s_stage_drift", kind="promote_batch", role=Role.implementer,
        args={"files": json.dumps(["a.txt"]),
              "baselines": json.dumps({"a.txt": None}),
              "source_hashes": json.dumps({"a.txt": verified})})
    (stage / "a.txt").write_text("mutated after approval", encoding="utf-8")

    with pytest.raises(executor.ExecutionError, match="changed after verification/approval"):
        executor.execute(session, action, tmp_path / "data")

    assert not (dest / "a.txt").exists()


def test_final_batch_rolls_back_if_a_later_replace_fails(tmp_path, monkeypatch):
    from gangof8 import executor, skills
    from gangof8.models import ProposedAction, Role, Session, Task

    stage, dest = tmp_path / "stage", tmp_path / "dest"
    stage.mkdir(); dest.mkdir()
    for name in ("a.txt", "b.txt"):
        (stage / name).write_text(f"new-{name}", encoding="utf-8")
        (dest / name).write_text(f"old-{name}", encoding="utf-8")
    baselines = {
        name: hashlib.sha256(f"old-{name}".encode()).hexdigest()
        for name in ("a.txt", "b.txt")}
    session = Session(
        session_id="s_rollback", workspace_root=str(stage), established_root=str(dest),
        task=Task(task_id="t", session_id="s_rollback", text="release"))
    action = ProposedAction(
        session_id="s_rollback", kind="promote_batch", role=Role.implementer,
        args={"files": json.dumps(["a.txt", "b.txt"]),
              "baselines": json.dumps(baselines),
              "source_hashes": json.dumps({
                  name: hashlib.sha256(f"new-{name}".encode()).hexdigest()
                  for name in ("a.txt", "b.txt")
              })})
    real_replace = skills.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-file failure")
        return real_replace(source, target)

    monkeypatch.setattr(skills.os, "replace", fail_second)
    with pytest.raises(executor.ExecutionError, match="rolled back"):
        executor.execute(session, action, tmp_path / "data")
    assert (dest / "a.txt").read_text(encoding="utf-8") == "old-a.txt"
    assert (dest / "b.txt").read_text(encoding="utf-8") == "old-b.txt"


# ---- HTTP surface ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from gangof8 import main as main_mod

    main_mod.service = GangOf8Service(data_dir=tmp_path / "data")
    main_mod.service.registry.register(_PlannerSeat())
    return TestClient(main_mod.app)


def test_goal_api_lifecycle(client):
    r = client.post("/goals", json={"text": "/goal Decide storage and retention"})
    assert r.status_code == 200
    goal = r.json()
    assert goal["status"] == "completed"
    assert len(goal["milestones"]) == 2

    listed = client.get("/goals").json()
    assert [g["goal_id"] for g in listed] == [goal["goal_id"]]

    got = client.get(f"/goals/{goal['goal_id']}").json()
    assert got["text"] == "Decide storage and retention"

    # resume on a settled goal is a conflict, not a crash
    r = client.post(f"/goals/{goal['goal_id']}/resume")
    assert r.status_code == 409

    r = client.delete(f"/goals/{goal['goal_id']}")
    assert r.status_code == 200
    assert client.get(f"/goals/{goal['goal_id']}").status_code == 404


def test_goal_api_rejects_empty_text(client):
    assert client.post("/goals", json={"text": "  /goal   "}).status_code == 422


def test_goal_composer_hint_served(client):
    page = client.get("/").text
    assert "/goal" in page  # the composer advertises the command


def test_assembly_runtime_failure_blames_module_that_silently_fails_to_attach_its_export(tmp_path):
    """The no-throw failure shape a stack trace can never attribute: a module
    reads a sibling module at script-load time, but the template's declared
    order loads the sibling LATER, so a defensive guard silently bails and the
    module never attaches its own export. Nothing throws; the only symptom is
    the entry point's "missing modules" console.error — far from the culprit.
    A real goal shipped exactly this (renderer.js reading Frogger.World before
    world.js loaded), passed every deterministic gate, failed final browser
    verification twice, and the blamed owner reproduced the same idiom because
    the error text never stated the load-order constraint. The export probe
    must blame the silently-bailing file and spell out that constraint."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    (stage / "js").mkdir(parents=True)
    painter_source = (
        "window.Arcade = window.Arcade || {};\n"
        "(function(){\n"
        "  var World = window.Arcade.World;\n"
        "  if (!World) return;\n"
        "  window.Arcade.Painter = { draw: function(){} };\n"
        "})();\n"
    )
    world_source = (
        "window.Arcade = window.Arcade || {};\n"
        "window.Arcade.World = { lanes: [] };\n"
    )
    boot_source = (
        "document.addEventListener('DOMContentLoaded', function(){\n"
        "  if (!window.Arcade.Painter) console.error('[Arcade] missing modules');\n"
        "});\n"
    )
    (stage / "js" / "painter.js").write_text(painter_source, encoding="utf-8")
    (stage / "js" / "world.js").write_text(world_source, encoding="utf-8")
    (stage / "js" / "boot.js").write_text(boot_source, encoding="utf-8")
    session = Session(
        session_id="s_silent_bail", status=SessionStatus.failed,
        workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.html",
            "sources": ["js/painter.js", "js/world.js", "js/boot.js"],
        },
        task=Task(task_id="t_silent", session_id="s_silent_bail", text="assembly"),
    )

    path, detail = service._assembly_runtime_failure_target(session)

    assert path == "js/painter.js"
    assert "never attached window.Arcade.Painter" in detail
    assert "Arcade.World" in detail
    assert "AFTER" in detail
    # the fix constraint must be stated, not just the symptom
    assert "lazily" in detail


def test_export_probe_accepts_exports_attached_by_load_handlers(tmp_path):
    """A module that legitimately attaches its export inside a
    DOMContentLoaded/load handler is NOT a silent-bail defect; the probe runs
    after those handlers and must stay quiet."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    (stage / "late_attach.js").write_text(
        "window.Arcade = window.Arcade || {};\n"
        "window.addEventListener('load', function(){\n"
        "  window.Arcade.Hud = { score: 0 };\n"
        "});\n",
        encoding="utf-8",
    )
    session = Session(
        session_id="s_late_attach", status=SessionStatus.failed,
        workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.html",
            "sources": ["late_attach.js"],
        },
        task=Task(task_id="t_late", session_id="s_late_attach", text="assembly"),
    )

    path, detail = service._assembly_runtime_failure_target(session)

    assert path == ""
    assert detail == ""


def test_resume_reopens_culprit_package_after_failed_release_verification(tmp_path, monkeypatch):
    """A release that failed browser verification cannot pass by re-verifying
    the same staged bytes — a real goal burned two full frontier release
    sessions on the identical console error because resume only re-ran the
    release. Resume must instead reproduce the failure deterministically from
    the staged assembly inputs and reopen the culprit package, exactly like an
    assembly-time fault (streak accounting included)."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    (stage / "js").mkdir(parents=True)
    painter_source = (
        "window.Arcade = window.Arcade || {};\n"
        "(function(){\n"
        "  var World = window.Arcade.World;\n"
        "  if (!World) return;\n"
        "  window.Arcade.Painter = { draw: function(){} };\n"
        "})();\n"
    )
    world_source = (
        "window.Arcade = window.Arcade || {};\n"
        "window.Arcade.World = { lanes: [] };\n"
    )
    (stage / "js" / "painter.js").write_text(painter_source, encoding="utf-8")
    (stage / "js" / "world.js").write_text(world_source, encoding="utf-8")
    goal = Goal(
        text="assemble", status="paused", current_index=2,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        release_status="failed_verification",
        release_session_id="s_failed_release",
        release_defects=[
            "arcade.html: browser acceptance failed with 1 error(s): "
            "console error: [Arcade] missing modules"
        ],
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_painter", owner="qwen", title="painter",
                task_text="author painter", status="done", session_id="s_painter",
                contract_declared=True, requires_delivery=True,
                required_files=["js/painter.js"],
                accepted_hashes={
                    "js/painter.js": hashlib.sha256(painter_source.encode()).hexdigest(),
                },
            ),
            GoalMilestone(
                index=1, package_id="wp_world", owner="glm", title="world",
                task_text="author world", status="done", session_id="s_world",
                contract_declared=True, requires_delivery=True,
                required_files=["js/world.js"],
                accepted_hashes={
                    "js/world.js": hashlib.sha256(world_source.encode()).hexdigest(),
                },
            ),
            GoalMilestone(
                index=2, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="done", session_id="s_assembly_done",
                depends_on=[0, 1], contract_declared=True, requires_delivery=True,
                required_files=["arcade.html"],
                dependencies=["js/painter.js", "js/world.js"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.html",
                release_files=["arcade.html"],
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_assembly_done", status=SessionStatus.done,
        outcome="succeeded", goal_id=goal.goal_id,
        goal_milestone=2, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE, assembly_template="index.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.html",
            "sources": ["js/painter.js", "js/world.js"],
        },
        task=Task(task_id="t_release", session_id="s_assembly_done", text="assembly"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    assert started == [0]
    repaired = service.goals.get(goal.goal_id)
    provider = repaired.milestones[0]
    assert provider.status == "pending"
    assert provider.session_id is None
    assert provider.invalidated_session_ids == ["s_painter"]
    assert "js/painter.js" in provider.acceptance_detail
    assert "AFTER" in provider.acceptance_detail
    assert repaired.milestones[2].status == "pending"
    assert repaired.release_status == "not_started"
    assert repaired.release_session_id is None
    assert repaired.assembly_fault_streak == {"0:dependency:js/painter.js": 1}


def test_resume_reopens_stylesheet_package_after_style_contract_release_failure(tmp_path, monkeypatch):
    """A style-contract release failure (rendered DOM classes mostly unmatched
    by any stylesheet rule) is reproducible statically from the staged
    template + stylesheets. Resume must reopen the stylesheet owner with the
    exact unmatched class list instead of re-running a frontier verification
    that a real goal watched decline to rewrite a stylesheet inline, twice."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    template_source = (
        "<html><head>\n"
        "<!-- GANGOF8:STYLE styles.css -->\n"
        "</head><body>\n"
        '<div class="hud hud-row"><span class="hud-label">Score</span>\n'
        '<span class="hud-value hud-score">0</span></div>\n'
        '<div class="overlay menu"><h1 class="title">Game</h1>\n'
        '<button class="btn btn-primary">Start</button>\n'
        '<p class="hint subtitle">press enter</p></div>\n'
        "</body></html>\n"
    )
    css_source = ".menu { color: red; }\n"  # 1 of 11 classes covered
    (stage / "index.template.html").write_text(template_source, encoding="utf-8")
    (stage / "styles.css").write_text(css_source, encoding="utf-8")
    goal = Goal(
        text="assemble", status="paused", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        release_status="failed_verification",
        release_session_id="s_failed_release",
        release_defects=[
            "game.html: browser acceptance failed with 1 error(s): style "
            "contract: only 1/11 DOM classes match any stylesheet rule"
        ],
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_shell", owner="gemini", title="shell",
                task_text="author shell", status="done", session_id="s_shell",
                contract_declared=True, requires_delivery=True,
                required_files=["index.template.html", "styles.css"],
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="done", session_id="s_assembly_style",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["game.html"],
                dependencies=["styles.css"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
                release_files=["game.html"],
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_assembly_style", status=SessionStatus.done,
        outcome="succeeded", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE,
        assembly_template="index.template.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.template.html",
            "sources": ["styles.css"],
        },
        task=Task(task_id="t_style", session_id="s_assembly_style", text="assembly"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    assert started == [0]
    repaired = service.goals.get(goal.goal_id)
    provider = repaired.milestones[0]
    assert provider.status == "pending"
    assert "styles.css" in provider.acceptance_detail
    assert "ONE contract" in provider.acceptance_detail
    # the unmatched classes are named so the rebuild can converge
    assert "hud-score" in provider.acceptance_detail
    assert repaired.milestones[1].status == "pending"
    assert repaired.release_status == "not_started"
    assert repaired.assembly_fault_streak == {"0:dependency:styles.css": 1}


def test_resume_of_breaker_paused_goal_grants_one_more_review_cycle(tmp_path, monkeypatch):
    """The fault-streak breaker pauses "for human review" — so an explicit
    human resume IS that review. Without this, a breaker-paused goal was a
    dead end (resume re-ran the doomed assembly once and re-paused on the
    same cap). Resume must drop capped streaks to one below the limit,
    reopen the blamed provider with the corrective guidance, and leave the
    breaker armed so the very next identical fault re-pauses."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    (stage / "styles.css").write_text(
        "@import url('https://fonts.example/retro.css');\n.menu{color:red}\n",
        encoding="utf-8")
    (stage / "index.template.html").write_text(
        "<html><head><!-- GANGOF8:STYLE styles.css --></head><body></body></html>",
        encoding="utf-8")
    limit = config.ASSEMBLY_FAULT_STREAK_LIMIT
    goal = Goal(
        text="assemble", status="paused", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        last_error=(
            "assembly attribution has blamed package 1 for the same dependency "
            "fault 3 times in a row without resolving it (styles.css); pausing "
            "for human review instead of rebuilding it again"
        ),
        assembly_fault_streak={f"0:dependency:styles.css": limit + 1},
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_shell", owner="gemini", title="shell",
                task_text="author shell", status="done", session_id="s_shell",
                contract_declared=True, requires_delivery=True,
                required_files=["index.template.html", "styles.css"],
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="failed", session_id="s_asm_import",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["game.html"], dependencies=["styles.css"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_asm_import", status=SessionStatus.failed,
        outcome="failed_verification", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE,
        assembly_template="index.template.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.template.html",
            "sources": ["styles.css"],
        },
        quality_gate={
            "verdict": "FAIL", "stage": "deterministic_assembly",
            # the pre-prescriptive message shape a saved session carries
            "detail": "inline stylesheet contains @import and is not self-contained: styles.css",
            "fault_scope": "dependency", "fault_path": "styles.css",
        },
        task=Task(task_id="t_import", session_id="s_asm_import", text="assembly"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    assert started == [0]
    repaired = service.goals.get(goal.goal_id)
    provider = repaired.milestones[0]
    assert provider.status == "pending"
    # exactly one more cycle: reduced to limit-1 by review, +1 by the reopen
    assert repaired.assembly_fault_streak == {"0:dependency:styles.css": limit}
    # the saved symptom-only message gained the prescriptive fix
    assert "@import" in provider.acceptance_detail
    assert "DELETE the @import" in provider.acceptance_detail
    assert "font stacks" in provider.acceptance_detail


def test_recovery_does_not_adopt_attempt_whose_dependencies_are_being_rebuilt(tmp_path):
    """Recovery adopts completed package attempts that lost their goal commit —
    but an attempt that completed against dependency bytes now being rebuilt
    would resurrect a stale output. A real goal adopted an assembled HTML
    expanded from a superseded stylesheet exactly this way, then headed into
    a doomed frontier release of the fossil."""
    from gangof8.models import ProposedAction
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    output = work / "game.html"
    output.write_text("<html>assembled</html>", encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    goal = Goal(
        text="assemble", status="paused", current_index=0,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_css", owner="gemini", title="css",
                task_text="author css", status="pending",
                contract_declared=True, requires_delivery=True,
                required_files=["styles.css"],
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="pending",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["game.html"], dependencies=["styles.css"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
            ),
        ],
    )
    service.goals.save(goal)
    candidate = Session(
        session_id="s_old_assembly", status=SessionStatus.done,
        outcome="succeeded", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        required_files=["game.html"],
        verified_output_hashes={"game.html": digest},
        task=Task(task_id="t_old", session_id="s_old_assembly", text="assembly"),
    )
    candidate.proposed_actions.append(ProposedAction(
        session_id="s_old_assembly", kind="write_file", role=Role.implementer,
        filename="game.html", status="executed", result_path=str(output),
        args={"filename": "game.html"},
    ))
    service.store.save_session(candidate)

    recovered = service._recover_verified_goal_packages(goal.goal_id)

    # wp_assembly must NOT be adopted while its dependency wp_css is pending
    assert recovered == []
    assert service.goals.get(goal.goal_id).milestones[1].status == "pending"


def test_release_prep_reassembles_when_accepted_assembly_inputs_were_rebuilt(tmp_path, monkeypatch):
    """Deterministic assembly records the exact source hashes it expanded. If
    a provider package was rebuilt afterwards, the accepted HTML is a fossil
    of superseded inputs; preparing a release from it re-verifies defects the
    inputs no longer have. Release prep must reopen the assembly package
    instead of opening (and paying for) a release session."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    new_css = ".menu { color: red; }\n"
    (stage / "styles.css").write_text(new_css, encoding="utf-8")
    (stage / "index.template.html").write_text(
        "<html><head><!-- GANGOF8:STYLE styles.css --></head><body></body></html>",
        encoding="utf-8")
    new_css_hash = hashlib.sha256(new_css.encode()).hexdigest()
    old_css_hash = hashlib.sha256(b"@import url('gone');").hexdigest()
    goal = Goal(
        text="assemble", status="paused", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        release_status="failed_verification",
        release_session_id="s_stale_release",
        release_defects=["game.html: style contract: only 1/11 DOM classes match"],
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_css", owner="gemini", title="css",
                task_text="author css", status="done", session_id="s_css_new",
                contract_declared=True, requires_delivery=True,
                required_files=["styles.css"],
                accepted_hashes={"styles.css": new_css_hash},
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="done", session_id="s_stale_assembly",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["game.html"], dependencies=["styles.css"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
                release_files=["game.html"],
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_stale_assembly", status=SessionStatus.done,
        outcome="succeeded", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE,
        assembly_template="index.template.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.template.html",
            "sources": ["styles.css"],
            # built from the OLD stylesheet bytes
            "source_hashes": {"styles.css": old_css_hash},
        },
        task=Task(task_id="t_stale", session_id="s_stale_assembly", text="assembly"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    repaired = service.goals.get(goal.goal_id)
    assert repaired.milestones[1].status == "pending"
    assert started == [1]
    assert repaired.release_status == "not_started"
    assert repaired.release_session_id is None
    assert "stale" in repaired.last_error


def test_resume_maps_verifier_critique_to_owning_package(tmp_path, monkeypatch):
    """The final semantic gate can fail a release on gameplay grounds no
    deterministic check can see (a real verifier correctly caught a Frogger
    world whose road/river bands were inverted). Without a usable inline
    repair that critique used to strand the goal — resume could only re-run
    an identical verification. The critique quotes the identifiers it judged
    (`LANES`); the file declaring such an identifier is deterministic to
    find, and resume must reopen its owner package with the critique as the
    retry correction."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    world_source = (
        "window.Arcade = window.Arcade || {};\n"
        "const LANES = [{row: 2, type: 'river'}, {row: 8, type: 'road'}];\n"
        "window.Arcade.World = { LANES: LANES };\n"
    )
    (stage / "world.js").write_text(world_source, encoding="utf-8")
    (stage / "index.template.html").write_text(
        "<html><head></head><body>"
        "<!-- GANGOF8:SCRIPT world.js --></body></html>",
        encoding="utf-8")
    goal = Goal(
        text="assemble", status="paused", current_index=1,
        collaboration_mode="build_team", delivery_mode="final_batch",
        staging_root=str(stage),
        release_status="failed_verification",
        release_session_id="s_semantic_release",
        release_defects=[],
        milestones=[
            GoalMilestone(
                index=0, package_id="wp_world", owner="kimi", title="world",
                task_text="author world", status="done", session_id="s_world",
                contract_declared=True, requires_delivery=True,
                required_files=["world.js"],
                accepted_hashes={
                    "world.js": hashlib.sha256(world_source.encode()).hexdigest(),
                },
            ),
            GoalMilestone(
                index=1, package_id="wp_assembly", owner="codex", title="assembly",
                task_text="assemble", status="done", session_id="s_asm_semantic",
                depends_on=[0], contract_declared=True, requires_delivery=True,
                required_files=["game.html"], dependencies=["world.js"],
                assembly_mode=assembly.HTML_INLINE,
                assembly_template="index.template.html",
                release_files=["game.html"],
            ),
        ],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_asm_semantic", status=SessionStatus.done,
        outcome="succeeded", goal_id=goal.goal_id,
        goal_milestone=1, work_package_id="wp_assembly",
        work_package_owner="codex", workspace_root=str(stage),
        assembly_mode=assembly.HTML_INLINE,
        assembly_template="index.template.html",
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "template": "index.template.html",
            "sources": ["world.js"],
            "source_hashes": {
                "world.js": hashlib.sha256(world_source.encode()).hexdigest(),
            },
        },
        task=Task(task_id="t_sem", session_id="s_asm_semantic", text="assembly"),
    ))
    service.store.save_session(Session(
        session_id="s_semantic_release", status=SessionStatus.failed,
        outcome="failed_verification", goal_id=goal.goal_id,
        goal_release=True, workspace_root=str(stage),
        quality_gate={
            "verifier": "codex", "verdict": "FAIL",
            "checks": [
                {"id": "R1", "status": "PASS", "detail": "single file"},
                {"id": "R2", "status": "FAIL",
                 "detail": "`LANES` places river rows above the road; classic "
                           "order requires road first then river"},
            ],
            "remaining_defects": [],
        },
        task=Task(task_id="t_rel", session_id="s_semantic_release", text="release"),
    ))
    started: list[int] = []
    monkeypatch.setattr(
        service, "_start_ready_packages",
        lambda current, background: started.extend(
            package.index for package in current.milestones
            if package.status == "pending"
            and all(current.milestones[d].status == "done" for d in package.depends_on)
        ),
    )

    out = service.resume_goal(goal.goal_id)

    assert out["status"] == "running"
    assert started == [0]
    repaired = service.goals.get(goal.goal_id)
    provider = repaired.milestones[0]
    assert provider.status == "pending"
    assert "world.js" in provider.acceptance_detail
    assert "LANES" in provider.acceptance_detail
    assert "road first then river" in provider.acceptance_detail
    assert repaired.milestones[1].status == "pending"
    assert repaired.assembly_fault_streak == {"0:dependency:world.js": 1}
