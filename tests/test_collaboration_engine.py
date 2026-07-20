"""Artifact-aware full-council collaboration for owned build packages."""

from __future__ import annotations

import pytest

from gangof8 import loop
from gangof8.cancellation import SessionCancelled
from gangof8.governance import BudgetExceeded
from gangof8.logstore import LogStore
from gangof8.models import (
    Classification,
    Complexity,
    Contribution,
    Council,
    CouncilMember,
    ProposedAction,
    Risk,
    Role,
    Session,
    Task,
    TaskType,
)
from gangof8.registry import AgentError
from gangof8.service import GangOf8Service


RESOURCES = ["codex", "claude", "gemini", "deepseek", "glm", "qwen", "kimi"]
ROLE_AGENTS = {
    Role.code_generator: "codex",
    Role.architect: "claude",
    Role.critic: "gemini",
    Role.api_integrator: "glm",
    Role.red_team: "qwen",
    Role.implementer: "kimi",
}
BASELINE = "<!doctype html><html><body><p>baseline</p></body></html>"
INTEGRATED = "<!doctype html><html><body><p>integrated</p></body></html>"


def _session() -> Session:
    session = Session(
        session_id="s_collaboration",
        task=Task(
            task_id="t_collaboration", session_id="s_collaboration",
            text="Build a complete single-file HTML arcade game",
        ),
        collaboration_mode="build_team",
        work_package_id="wp_1",
        work_package_owner="codex",
        resource_roster=list(RESOURCES),
        participation_mode="full_council",
        required_files=["game.html"],
    )
    session.classification = Classification(
        task_type=TaskType.code,
        complexity=Complexity.complex,
        risk=Risk.none,
        produces_output=True,
    )
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id,
        kind="write_file",
        role=Role.implementer,
        filename="game.html",
        content=BASELINE,
        args={"filename": "game.html", "content": BASELINE},
    ))
    return session


def _contribution(session: Session, member: CouncilMember, content: str) -> Contribution:
    contribution = Contribution(
        round=0, role=member.role, agent=member.agent, content=content,
    )
    session.contributions.append(contribution)
    return contribution


def _owner_reply() -> str:
    dispositions = "\n".join(
        f"DISPOSITION: {seat} | ACCEPT | incorporated the useful review"
        for seat in RESOURCES if seat != "codex"
    )
    return (
        dispositions
        + "\nARTIFACT: game.html\n"
        + INTEGRATED
        + "\nEND_ARTIFACT"
    )


def test_enabled_deepseek_is_kept_in_resource_roster_without_a_role(
        tmp_path, monkeypatch):
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.backend = "cli"
    service.panel = ["claude", "codex"]
    service.role_agents = dict(ROLE_AGENTS)
    service.settings.openrouter_enabled = {
        "deepseek": True, "glm": True, "qwen": True, "kimi": True,
    }
    monkeypatch.setattr(service.registry, "names", lambda: list(RESOURCES))

    assert service._effective_resource_roster() == RESOURCES
    assert "deepseek" not in service.role_agents.values()


def test_full_council_challenges_real_baseline_and_owner_integrates(
        tmp_path, monkeypatch):
    session = _session()
    store = LogStore(tmp_path)
    owner = CouncilMember(role=Role.panelist, agent="codex", active=True)
    council = Council(members=[owner])
    prompts: dict[str, str] = {}
    monkeypatch.setattr(
        loop.smoke, "smoke_source", lambda *args, **kwargs: (True, True, "ok", True),
    )

    def peer_call(member, prompt):
        prompts[member.agent] = prompt
        return _contribution(
            session,
            member,
            "VERDICT: CHANGES\n"
            f"FINDING: {member.agent} found a concrete issue\n"
            "EDIT: game.html\n<<<<<<< OLD\n<p>baseline</p>\n=======\n"
            f"<p>{member.agent} improvement</p>\n>>>>>>> NEW",
        )

    def owner_call(member, prompt):
        prompts["owner_integration"] = prompt
        return _contribution(session, member, _owner_reply())

    loop._run_package_collaboration(
        session, council, peer_call, owner_call, store, ROLE_AGENTS,
    )

    assert [assignment.seat for assignment in session.collaboration_assignments] == RESOURCES[1:]
    assert all(
        assignment.status == "contributed"
        for assignment in session.collaboration_assignments
    )
    deepseek = next(
        assignment for assignment in session.collaboration_assignments
        if assignment.seat == "deepseek"
    )
    assert deepseek.lens == "independent"
    assert deepseek.patch_files == ["game.html"]
    assert "ACTUAL BASELINE FILE: game.html" in prompts["deepseek"]
    assert BASELINE in prompts["deepseek"]
    assert "RESOURCE CONTRIBUTION: deepseek" in prompts["owner_integration"]
    assert all(
        assignment.disposition.startswith("ACCEPT")
        for assignment in session.collaboration_assignments
    )
    assert session.collaboration_integration_status == "integrated"
    assert session.collaboration_integrated_files == ["game.html"]
    delivered = next(
        action for action in session.proposed_actions
        if action.kind == "write_file" and action.role == Role.implementer
    )
    assert delivered.content == INTEGRATED
    assert session.collaboration_baseline["game.html"] == BASELINE


def test_unavailable_deepseek_stays_visible_without_blocking_other_resources(
        tmp_path, monkeypatch):
    session = _session()
    store = LogStore(tmp_path)
    owner = CouncilMember(role=Role.panelist, agent="codex", active=True)
    council = Council(members=[owner])
    monkeypatch.setattr(
        loop.smoke, "smoke_source", lambda *args, **kwargs: (True, True, "ok", True),
    )

    def peer_call(member, prompt):
        del prompt
        if member.agent == "deepseek":
            raise AgentError("quota exceeded")
        return _contribution(
            session, member,
            f"VERDICT: PASS\nFINDING: {member.agent} verified its assigned lens",
        )

    def owner_call(member, prompt):
        del prompt
        dispositions = "\n".join(
            f"DISPOSITION: {seat} | ACCEPT | review completed"
            for seat in RESOURCES if seat not in {"codex", "deepseek"}
        )
        return _contribution(
            session, member,
            dispositions + "\nARTIFACT: game.html\n" + INTEGRATED + "\nEND_ARTIFACT",
        )

    loop._run_package_collaboration(
        session, council, peer_call, owner_call, store, ROLE_AGENTS,
    )

    deepseek = next(
        assignment for assignment in session.collaboration_assignments
        if assignment.seat == "deepseek"
    )
    assert deepseek.status == "unavailable"
    assert "quota" in deepseek.error
    assert "deepseek" in session.resource_roster
    assert session.collaboration_integration_status == "integrated"


def test_focused_mode_does_not_schedule_resource_calls(tmp_path):
    session = _session()
    session.participation_mode = "focused"
    called = []

    loop._run_package_collaboration(
        session,
        Council(members=[]),
        lambda *args: called.append(args),
        lambda *args: called.append(args),
        LogStore(tmp_path),
        ROLE_AGENTS,
    )

    assert called == []
    assert session.collaboration_assignments == []


def test_adaptive_mode_skips_prose_only_artifacts(tmp_path):
    session = _session()
    session.participation_mode = "adaptive"
    session.required_files = ["report.md"]
    called = []

    loop._run_package_collaboration(
        session,
        Council(members=[]),
        lambda *args: called.append(args),
        lambda *args: called.append(args),
        LogStore(tmp_path),
        ROLE_AGENTS,
    )

    assert called == []
    assert session.collaboration_assignments == []
    assert session.proposed_actions[0].content == BASELINE


def test_protocol_miss_is_retried_once_before_owner_integration(
        tmp_path, monkeypatch):
    session = _session()
    session.resource_roster = ["codex", "deepseek"]
    store = LogStore(tmp_path)
    calls = 0
    monkeypatch.setattr(
        loop.smoke, "smoke_source", lambda *args, **kwargs: (True, True, "ok", True),
    )

    def peer_call(member, prompt):
        nonlocal calls
        del prompt
        calls += 1
        content = (
            "I will inspect it later"
            if calls == 1 else
            "VERDICT: PASS\nFINDING: the independent pass found no blocking defect"
        )
        return _contribution(session, member, content)

    def owner_call(member, prompt):
        del prompt
        return _contribution(
            session, member,
            "DISPOSITION: deepseek | ACCEPT | independent pass completed\n"
            "ARTIFACT: game.html\n" + INTEGRATED + "\nEND_ARTIFACT",
        )

    loop._run_package_collaboration(
        session, Council(members=[]), peer_call, owner_call, store, ROLE_AGENTS,
    )

    assert calls == 2
    assert session.collaboration_assignments[0].attempts == 2
    assert session.collaboration_assignments[0].status == "contributed"
    assert session.collaboration_integration_status == "integrated"


def test_missing_owner_disposition_preserves_valid_baseline(tmp_path):
    session = _session()
    session.resource_roster = ["codex", "deepseek"]
    store = LogStore(tmp_path)

    def peer_call(member, prompt):
        del prompt
        return _contribution(
            session, member,
            "VERDICT: PASS\nFINDING: baseline is acceptable",
        )

    def owner_call(member, prompt):
        del prompt
        return _contribution(
            session, member,
            "ARTIFACT: game.html\n" + INTEGRATED + "\nEND_ARTIFACT",
        )

    loop._run_package_collaboration(
        session, Council(members=[]), peer_call, owner_call, store, ROLE_AGENTS,
    )

    assert session.collaboration_integration_status == (
        "baseline_preserved_missing_dispositions"
    )
    assert session.proposed_actions[0].content == BASELINE


def test_collaboration_budget_exhaustion_preserves_baseline(tmp_path):
    session = _session()
    session.resource_roster = ["codex", "deepseek"]
    store = LogStore(tmp_path)

    def exhausted(*args):
        del args
        raise BudgetExceeded("max_agent_calls reached")

    loop._run_package_collaboration(
        session, Council(members=[]), exhausted, exhausted, store, ROLE_AGENTS,
    )

    assignment = session.collaboration_assignments[0]
    assert assignment.status == "failed"
    assert assignment.attempts == 2
    assert session.collaboration_integration_status == (
        "baseline_preserved_no_contributors"
    )
    assert session.proposed_actions[0].content == BASELINE


def test_collaboration_cancellation_propagates_and_keeps_assignment_pending(tmp_path):
    session = _session()
    session.resource_roster = ["codex", "deepseek"]

    def cancelled(*args):
        del args
        raise SessionCancelled()

    with pytest.raises(SessionCancelled):
        loop._run_package_collaboration(
            session, Council(members=[]), cancelled, cancelled,
            LogStore(tmp_path), ROLE_AGENTS,
        )

    assert session.collaboration_assignments[0].status == "pending"
    assert session.proposed_actions[0].content == BASELINE


def test_completed_collaboration_is_resume_idempotent(tmp_path, monkeypatch):
    session = _session()
    session.resource_roster = ["codex", "deepseek"]
    store = LogStore(tmp_path)
    calls = []
    monkeypatch.setattr(
        loop.smoke, "smoke_source", lambda *args, **kwargs: (True, True, "ok", True),
    )

    def peer_call(member, prompt):
        calls.append((member.agent, "peer"))
        del prompt
        return _contribution(
            session, member,
            "VERDICT: PASS\nFINDING: independent pass completed",
        )

    def owner_call(member, prompt):
        calls.append((member.agent, "owner"))
        del prompt
        return _contribution(
            session, member,
            "DISPOSITION: deepseek | ACCEPT | review completed\n"
            "ARTIFACT: game.html\n" + INTEGRATED + "\nEND_ARTIFACT",
        )

    args = (
        session, Council(members=[]), peer_call, owner_call, store, ROLE_AGENTS,
    )
    loop._run_package_collaboration(*args)
    first_calls = list(calls)
    loop._run_package_collaboration(*args)

    assert first_calls == [("deepseek", "peer"), ("codex", "owner")]
    assert calls == first_calls
