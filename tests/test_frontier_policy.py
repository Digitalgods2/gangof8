"""Frontier implementation ownership, recovery, and release-quality policy."""

from __future__ import annotations

import re

import pytest

from gangof8 import config, goals, loop, rounds
from gangof8.artifacts import canonical_protocol_filename, parse_proposals
from gangof8.logstore import LogStore
from gangof8.models import (
    Classification, Complexity, Council, CouncilMember, Contribution,
    GoalMilestone, ProposedAction, Risk, Role, Session, Task, TaskType,
)
from gangof8.service import GangOf8Service


def _code_session() -> Session:
    session = Session(
        session_id="s_frontier", task=Task(
            task_id="t_frontier", session_id="s_frontier",
            text="Build game.html; it must support keyboard controls and pause.",
        ),
    )
    session.classification = Classification(
        task_type=TaskType.code, complexity=Complexity.complex,
        risk=Risk.none, produces_output=True,
    )
    return session


def test_protocol_filenames_strip_all_outer_presentation_quotes():
    text = (
        "ARTIFACT: `'arcade_portal.html'`\n<!doctype html><html></html>\n"
        "EDIT: \"src/app.js\"\n<<<<<<< OLD\nold\n=======\nnew\n>>>>>>> NEW\n"
        "PROMOTE: ‘arcade_portal.html’\n"
    )
    actions = parse_proposals("s", text)
    assert [action.filename for action in actions] == [
        "arcade_portal.html", "src/app.js", "arcade_portal.html",
    ]
    assert canonical_protocol_filename("'assets/Benny\'s-theme.css'") == "assets/Benny's-theme.css"


def test_frontier_implementation_has_no_default_coordinator_deadline():
    session = _code_session()
    session.cli_timeouts = {"claude": 30}
    assert loop._effective_agent_timeout(
        session, "claude", config.FRONTIER_AUTHOR_TIMEOUT) == config.FRONTIER_AUTHOR_TIMEOUT
    assert config.FRONTIER_AUTHOR_TIMEOUT == 0
    assert config.PACKAGE_AUTHOR_DEADLINE == 0
    assert config.FRONTIER_VERIFY_TIMEOUT == 0


def test_settings_timeout_never_caps_any_coding_stage():
    session = _code_session()
    session.cli_timeouts = {"claude": 320, "codex": 320, "gemini": 320}
    assert loop._effective_agent_timeout(session, "claude", config.LEAD_TIMEOUT) == 0
    assert loop._effective_agent_timeout(session, "codex", config.JUDGE_TIMEOUT) == 0
    assert loop._effective_agent_timeout(session, "gemini", config.PANEL_AUTHOR_TIMEOUT) == 0
    assert loop._effective_agent_timeout(session, "gemini", config.JUDGE_TIMEOUT) == 0


def test_substantial_build_auto_routes_but_small_fix_does_not():
    brief = (
        "Build a production-ready single-file game application. It must include "
        "responsive keyboard and touch controls, audio, persistence, tests, "
        "performance safeguards, accessibility, multiple integrated components, "
        "and a complete acceptance checklist. " * 6
    )
    assert goals.should_auto_route(brief)
    assert not goals.should_auto_route("Fix the typo in README.md")
    assert not goals.should_auto_route(brief, has_attachments=True)


def test_normalizer_moves_claude_and_codex_onto_code_packages(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.panel = ["gemini", "claude", "codex"]
    service.role_agents[Role.code_generator] = "codex"
    packages = [
        # review.md is a released artifact too: two natural artifacts keep
        # this a legitimately multi-package plan under Phase 1 right-sizing.
        GoalMilestone(index=0, title="review", task_text="review", owner="claude",
                      required_files=["review.md"], release_files=["review.md"],
                      release_declared=True, contract_declared=True),
        GoalMilestone(index=1, title="engine", task_text="implement", owner="gemini",
                      required_files=["src/engine.js"], contract_declared=True),
        GoalMilestone(index=2, title="release", task_text="integrate", owner="gemini",
                      required_files=["index.html"], release_files=["index.html"],
                      release_declared=True, contract_declared=True),
    ]
    normalized, errors = service._normalize_work_packages(packages, "Build the application")
    assert not errors
    code_owners = {
        package.owner for package in normalized
        if any(name.endswith((".js", ".html")) for name in package.required_files)
    }
    assert {"claude", "codex"} <= code_owners
    assert normalized[2].owner == "codex", "configured coder owns final integration"


def test_configured_code_generator_owns_single_file_build(tmp_path):
    """Regression: a planner-authored Claude OWNER must not override Codex in
    Settings for the sole source package in a single-file build."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.role_agents[Role.code_generator] = "codex"
    package = GoalMilestone(
        index=0, title="game", task_text="author the complete game",
        owner="claude", required_files=["ms-pacman.html"],
        release_files=["ms-pacman.html"], release_declared=True,
        contract_declared=True,
    )

    normalized, errors = service._normalize_work_packages(
        [package], "Build a complete single-file HTML arcade game",
        roster=["claude", "codex"],
    )

    assert not errors
    assert normalized[0].owner == "codex"


def test_default_build_roster_puts_configured_code_generator_first(
        tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.panel = ["claude", "codex", "gemini"]
    service.role_agents[Role.code_generator] = "codex"
    monkeypatch.setattr(config, "GOAL_FULL_ROSTER", False)

    assert service._default_build_roster() == ["codex", "claude", "gemini"]


def test_service_role_remap_does_not_mutate_backend_defaults(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    configured_default = config.ROLE_AGENTS_BY_BACKEND[
        service.backend
    ][Role.code_generator]

    service.role_agents[Role.code_generator] = "temporary-test-seat"

    assert config.ROLE_AGENTS_BY_BACKEND[
        service.backend
    ][Role.code_generator] == configured_default


def test_missing_frontier_candidate_is_a_hard_gate(tmp_path):
    session = _code_session()
    session.required_frontier_authors = ["claude", "codex"]
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.panelist,
        filename="claude__game.html", content="<!doctype html><html></html>",
        args={"filename": "claude__game.html", "content": "<!doctype html><html></html>"},
    ))
    council = Council(members=[])
    with pytest.raises(loop.QualityGateFailed, match="codex"):
        loop._run_best_of_n(
            session, council, [], lambda *args: None, lambda *args: None,
            LogStore(tmp_path),
        )


def test_independent_frontier_gate_requires_every_acceptance_id(tmp_path):
    session = _code_session()
    session.required_frontier_authors = ["claude", "codex"]
    lead = CouncilMember(role=Role.lead, agent="gemini", active=True)
    claude = CouncilMember(role=Role.panelist, agent="claude", active=True)
    codex = CouncilMember(role=Role.panelist, agent="codex", active=True)
    council = Council(members=[lead, claude, codex])
    session.council = council

    def call(member, prompt, timeout_s=None):
        requirement_ids = re.findall(r"^R(\d+):", prompt, re.MULTILINE)
        body = "\n".join(
            f"CHECK R{number}: PASS - verified in source" for number in requirement_ids
        ) + "\nVERDICT: PASS"
        return Contribution(round=0, role=member.role, agent=member.agent, content=body)

    content, edits, verifier = loop._independent_frontier_release_gate(
        session, council, "gemini", "game.html",
        "<!doctype html><html><body><script>let paused=false;</script></body></html>",
        call, LogStore(tmp_path),
    )
    assert content
    assert edits == 0
    assert verifier == "claude"
    assert session.quality_gate["verdict"] == "PASS"
    assert not session.quality_gate["missing_checks"]


def test_frontier_verdict_without_checks_is_never_a_pass():
    verdict, checks, defects = rounds.parse_frontier_verdict("VERDICT: PASS")
    assert verdict == "FAIL"
    assert checks == []
    assert defects == []
