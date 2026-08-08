"""Exact-output package fan-out, deadline, and failure-accounting policy."""

from __future__ import annotations

import re
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from gangof8 import loop, rounds
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import (
    Classification,
    Complexity,
    Contribution,
    Council,
    CouncilMember,
    Goal,
    GoalMilestone,
    Risk,
    Role,
    Session,
    Task,
    TaskType,
)
from gangof8.registry import AdapterResult, AgentError, AgentRegistry
from gangof8.sessions import SessionManager
from gangof8.service import GangOf8Service


FILES = [
    "index.template.html",
    "css/theme.css",
    "js/core/portal.js",
    "js/core/engine.js",
]


def _session() -> Session:
    session = Session(
        session_id="s_package_fanout",
        task=Task(
            task_id="t_package_fanout",
            session_id="s_package_fanout",
            text=(
                "OVERALL GOAL:\nA deliberately long product brief that should not be "
                "repeated to every exact-output author.\n\n"
                "NON-BLOCKING INTERFACE INPUTS:\nGame exposes start and pause.\n\n"
                "THE PACKAGE TO COMPLETE NOW:\nBuild the shell, theme, portal, and engine."
            ),
        ),
        collaboration_mode="build_team",
        work_package_id="1",
        work_package_owner="codex",
        package_helpers=["gemini", "qwen", "grok"],
        required_files=list(FILES),
    )
    session.classification = Classification(
        task_type=TaskType.code,
        complexity=Complexity.complex,
        risk=Risk.none,
        produces_output=True,
    )
    session.budgets.max_agent_calls = 20
    return session


def _council() -> tuple[Council, CouncilMember]:
    lead = CouncilMember(role=Role.lead, agent="codex", active=True)
    owner = CouncilMember(role=Role.panelist, agent="codex", active=True)
    return Council(members=[lead, owner]), lead


def test_package_assignments_keep_cohesive_outputs_with_owner():
    session = _session()
    council, _lead = _council()

    assignments = loop._package_output_assignments(session, council)

    assert list(assignments) == FILES
    assert [member.agent for member in assignments.values()] == ["codex"] * len(FILES)
    assert loop._package_author_jobs(assignments)[0][1] == FILES
    assert len(loop._package_author_jobs(assignments)) == 1

    prompt = rounds.package_output_prompt(
        session,
        assignments["css/theme.css"],
        0,
        FILES,
        {name: member.agent for name, member in assignments.items()},
        staged_context="===== ACCEPTED DEPENDENCY: src/core.js =====\nconst dtUnit = 'seconds';",
    )
    assert "OVERALL GOAL" not in prompt
    assert "NON-BLOCKING INTERFACE INPUTS" in prompt
    assert "YOUR ASSIGNED OUTPUTS: " + ", ".join(FILES) in prompt
    assert "const dtUnit = 'seconds'" in prompt
    template_prompt = rounds.package_output_prompt(
        session,
        assignments["index.template.html"],
        0,
        ["index.template.html"],
        {name: member.agent for name, member in assignments.items()},
    )
    assert "literal standalone line" in template_prompt
    assert "Never place a directive inside" in template_prompt


def test_goal_package_preserves_healthy_sibling_seats_as_helpers(tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.panel = ["codex", "gemini", "qwen"]
    goal = Goal(
        text="build",
        status="running",
        epoch=1,
        collaboration_mode="build_team",
        delivery_mode="final_batch",
        staging_root=str(tmp_path / "stage"),
        milestones=[GoalMilestone(
            index=0,
            package_id="wp_1",
            title="core",
            task_text="build core",
            owner="codex",
            required_files=["src/core.js", "src/view.js"],
            contract_declared=True,
            requires_delivery=True,
        )],
    )
    service.goals.save(goal)
    monkeypatch.setattr(
        service,
        "_run_owned",
        lambda session, _runner, background: session,
    )

    session = service._start_milestone(goal, 0, background=False)

    assert session is not None
    assert session.panel == ["codex"]
    assert session.package_helpers == ["gemini", "qwen"]


def test_package_author_receives_hash_bound_dependency_bytes(tmp_path):
    workspace = tmp_path / "stage"
    source = workspace / "src" / "core.js"
    source.parent.mkdir(parents=True)
    source.write_text("globalThis.CLOCK_UNIT = 'seconds';\n", encoding="utf-8")
    session = _session()
    session.workspace_root = str(workspace)
    session.runtime_dependencies = ["src/core.js"]
    session.dependency_hashes = {
        "src/core.js": hashlib.sha256(source.read_bytes()).hexdigest(),
    }

    context = loop._accepted_dependency_context(session, tmp_path / "data")

    assert "ACCEPTED DEPENDENCY: src/core.js" in context
    assert session.dependency_hashes["src/core.js"] in context
    assert "CLOCK_UNIT = 'seconds'" in context


def test_multi_output_package_uses_one_atomic_owner_call(tmp_path):
    session = _session()
    council, lead = _council()
    session.council = council
    store = LogStore(tmp_path)
    manager = SessionManager(store)
    governance = Governance(store)
    entered: set[str] = set()
    prompts: dict[str, str] = {}
    lock = threading.Lock()

    def call(member, prompt, timeout_s=None):
        del timeout_s
        with lock:
            entered.add(member.agent)
            prompts[member.agent] = prompt
        match = re.search(r"YOUR ASSIGNED OUTPUTS: ([^\n]+)", prompt)
        assert match
        filenames = [name.strip() for name in match.group(1).split(",")]
        blocks = []
        for filename in filenames:
            if filename.endswith(".html"):
                content = "<!doctype html><html><body></body></html>"
            elif filename.endswith(".css"):
                content = ":root { color-scheme: dark; }"
            else:
                content = "globalThis.packageReady = true;"
            blocks.append(f"ARTIFACT: {filename}\n{content}\nEND_ARTIFACT")
        return Contribution(
            round=0,
            role=member.role,
            agent=member.agent,
            content="\n".join(blocks),
        )

    paused = loop._run_panel_rounds(
        session,
        manager,
        council,
        lead,
        call,
        call,
        governance,
        store,
        None,
        "unused whole-goal overview",
        time.monotonic(),
    )

    assert paused is False
    assert entered == {"codex"}
    assert session.package_output_authors == {name: "codex" for name in FILES}
    assert all(session.package_output_attempts[name] == 1 for name in FILES)
    adopted = [
        action.filename for action in session.proposed_actions
        if action.role == Role.implementer
    ]
    assert adopted == FILES
    assert all("OVERALL GOAL" not in prompt for prompt in prompts.values())


def test_build_team_timeout_is_not_retried_against_same_author(tmp_path):
    session = _session()
    session.required_files = ["index.template.html"]
    member = CouncilMember(role=Role.panelist, agent="codex", active=True)
    store = LogStore(tmp_path)
    calls = 0

    def call(_member, _prompt, timeout_s=None):
        nonlocal calls
        del timeout_s
        calls += 1
        raise AgentError("codex CLI timed out after 600s")

    result = loop._panel_one(
        session, member, "author", call, Governance(store), store, timeout_s=600,
    )

    assert result is None
    assert calls == 1
    assert session.frontier_author_recoveries == {}
    assert "timed out" in session.package_call_failures["codex"]


def test_owner_timeout_fans_missing_outputs_out_to_enabled_helpers(tmp_path):
    session = _session()
    session.required_files = ["index.html", "css/arcade.css", "js/portal.js"]
    session.package_helpers = ["gemini", "deepseek", "glm"]
    council, lead = _council()
    session.council = council
    store = LogStore(tmp_path)
    calls: dict[str, list[str]] = {}

    def call(member, prompt, timeout_s=None):
        del timeout_s
        match = re.search(r"YOUR ASSIGNED OUTPUTS: ([^\n]+)", prompt)
        assert match
        filenames = [name.strip() for name in match.group(1).split(",")]
        calls[member.agent] = filenames
        if member.agent == "codex":
            raise AgentError("codex CLI timed out after 120s")
        blocks = []
        for filename in filenames:
            if filename.endswith(".html"):
                content = "<!doctype html><html><body></body></html>"
            elif filename.endswith(".css"):
                content = ":root { color-scheme: dark; }"
            else:
                content = "globalThis.portalReady = true;"
            blocks.append(f"ARTIFACT: {filename}\n{content}\nEND_ARTIFACT")
        return Contribution(
            round=0,
            role=member.role,
            agent=member.agent,
            content="\n".join(blocks),
        )

    paused = loop._run_panel_rounds(
        session,
        SessionManager(store),
        council,
        lead,
        call,
        call,
        Governance(store),
        store,
        None,
        "unused",
        time.monotonic(),
    )

    assert paused is False
    assert calls == {
        "codex": ["index.html", "css/arcade.css", "js/portal.js"],
        "gemini": ["index.html"],
        "deepseek": ["css/arcade.css"],
        "glm": ["js/portal.js"],
    }
    assert session.package_output_authors == {
        "index.html": "gemini",
        "css/arcade.css": "deepseek",
        "js/portal.js": "glm",
    }
    assert session.package_output_attempts == {
        "index.html": 2,
        "css/arcade.css": 2,
        "js/portal.js": 2,
    }
    for filename, helper in session.package_output_authors.items():
        assert [
            (entry["agent"], entry["kind"], entry["status"])
            for entry in session.package_output_history[filename]
        ] == [
            ("codex", "primary", "failed"),
            (helper, "failover", "completed"),
        ]
    assert "timed out" in session.package_call_failures["codex"]
    assert {
        action.filename for action in session.proposed_actions
        if action.role == Role.implementer
    } == set(session.required_files)


def test_package_authoring_has_no_default_wall_clock_cutoff():
    session = _session()
    member = CouncilMember(role=Role.panelist, agent="codex", active=True)

    timeout = loop._package_author_timeout(session, member)

    assert timeout == 0
    assert loop._package_seconds_remaining(session) is None


def test_atomic_package_never_mixes_helper_authorship(tmp_path):
    session = _session()
    council, lead = _council()
    session.council = council
    store = LogStore(tmp_path)
    manager = SessionManager(store)
    governance = Governance(store)
    calls: dict[str, int] = {}

    def call(member, prompt, timeout_s=None):
        del timeout_s
        calls[member.agent] = calls.get(member.agent, 0) + 1
        match = re.search(r"YOUR ASSIGNED OUTPUTS: ([^\n]+)", prompt)
        assert match
        filenames = [name.strip() for name in match.group(1).split(",")]
        blocks = []
        for filename in filenames:
            if filename.endswith(".html"):
                content = "<!doctype html><html><body></body></html>"
            elif filename.endswith(".css"):
                content = ":root { color-scheme: dark; }"
            else:
                content = f"globalThis['{filename}'] = true;"
            blocks.append(f"ARTIFACT: {filename}\n{content}\nEND_ARTIFACT")
        return Contribution(
            round=0,
            role=member.role,
            agent=member.agent,
            content="\n".join(blocks),
        )

    paused = loop._run_panel_rounds(
        session,
        manager,
        council,
        lead,
        call,
        call,
        governance,
        store,
        None,
        "unused",
        time.monotonic(),
    )

    assert paused is False
    assert calls == {"codex": 1}
    assert session.package_output_authors == {name: "codex" for name in FILES}
    assert session.package_output_attempts == {name: 1 for name in FILES}
    assert all(
        session.package_output_history[filename][0]["agent"] == "codex"
        for filename in FILES
    )
    adopted = {
        action.filename for action in session.proposed_actions
        if action.role == Role.implementer
    }
    assert adopted == set(FILES)


def test_no_healthy_sibling_fails_without_recalling_owner(tmp_path):
    session = _session()
    session.required_files = ["index.template.html"]
    session.package_helpers = []
    council, lead = _council()
    session.council = council
    store = LogStore(tmp_path)
    calls = 0

    def call(_member, _prompt, timeout_s=None):
        nonlocal calls
        del timeout_s
        calls += 1
        raise AgentError("owner timed out")

    with pytest.raises(loop.QualityGateFailed, match="did not produce"):
        loop._run_panel_rounds(
            session,
            SessionManager(store),
            council,
            lead,
            call,
            call,
            Governance(store),
            store,
            None,
            "unused",
            time.monotonic(),
        )

    assert calls == 1
    assert session.package_output_attempts["index.template.html"] == 1
    assert session.package_output_history["index.template.html"][0]["agent"] == "codex"


def test_expired_package_deadline_rejects_another_attempt():
    session = _session()
    member = CouncilMember(role=Role.panelist, agent="codex", active=True)
    session.package_deadline_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()

    with pytest.raises(loop.QualityGateFailed, match="shared authoring deadline"):
        loop._package_call_timeout(session, member, 600)


class _FailingAdapter:
    name = "failing"
    local_process = True

    def call(self, role, prompt, timeout_s, images=None):
        del role, prompt, timeout_s, images
        time.sleep(0.01)
        raise AgentError("provider timeout")


def test_failed_calls_remain_visible_as_attempts(tmp_path):
    store = LogStore(tmp_path)
    session = SessionManager(store).create("observe failed call", source="test")
    registry = AgentRegistry()
    registry.register(_FailingAdapter())
    member = CouncilMember(role=Role.panelist, agent="failing", active=True)

    with pytest.raises(AgentError, match="provider timeout"):
        loop._agent_call(session, registry, store, member, "work", timeout_s=1)

    assert session.agent_calls == 0
    assert session.agent_call_attempts == 1
    assert session.agent_attempt_duration_ms >= 1
    persisted = Session.model_validate(store.load_session(session.session_id))
    assert persisted.agent_call_attempts == 1
    assert persisted.agent_attempt_duration_ms >= 1


class _ProgressSupervisedAdapter:
    name = "streaming-api"
    local_process = False
    streams_progress = True

    def __init__(self):
        self.timeout_s = None

    def call(self, role, prompt, timeout_s, images=None):
        del role, prompt, images
        self.timeout_s = timeout_s
        return AdapterResult(content="completed")


def test_streaming_api_calls_have_no_arbitrary_hard_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(loop.config, "OPENROUTER_HARD_TIMEOUT", 0)
    store = LogStore(tmp_path)
    session = SessionManager(store).create("review the artifact", source="test")
    registry = AgentRegistry()
    adapter = _ProgressSupervisedAdapter()
    registry.register(adapter)
    member = CouncilMember(role=Role.critic, agent=adapter.name, active=True)

    loop._agent_call(session, registry, store, member, "work", timeout_s=120)

    assert adapter.timeout_s == 0
