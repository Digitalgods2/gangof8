"""Deterministic, manifest-bound final assembly regression coverage."""

from __future__ import annotations

import hashlib

import pytest

from gangof8 import assembly, goals, loop
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import (
    Contribution, Council, CouncilMember, Goal, GoalMilestone, ProposedAction,
    Role, Session, Task,
)
from gangof8.service import GangOf8Service
from gangof8.sessions import SessionManager


def _write_manifest(root, files: dict[str, str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _template() -> str:
    return (
        "<!doctype html>\n<html><head>\n"
        "<!-- GANGOF8:STYLE assets/app.css -->\n"
        "</head><body><main id=\"app\"></main>\n"
        "<!-- GANGOF8:SCRIPT src/app.js -->\n"
        "</body></html>\n"
    )


def test_materializer_copies_every_accepted_source_exactly_once_without_slicing(tmp_path):
    root = tmp_path / "stage"
    features = [
        f"globalThis.GameRegistry.register('game-{number}', () => ({number}));"
        for number in range(128)
    ]
    javascript = "\n".join(features) + "\n// FINAL_ACCEPTED_SENTINEL\n"
    stylesheet = "body { color: #fff; background: #10121a; }\n"
    hashes = _write_manifest(root, {
        "assets/app.css": stylesheet,
        "src/app.js": javascript,
    })

    result = assembly.materialize_html_inline(
        _template(), root, ["assets/app.css", "src/app.js"], hashes)

    accepted_stylesheet = (root / "assets" / "app.css").read_bytes().decode("utf-8")
    accepted_javascript = (root / "src" / "app.js").read_bytes().decode("utf-8")
    assert accepted_stylesheet in result.content
    assert accepted_javascript in result.content
    assert result.content.count("FINAL_ACCEPTED_SENTINEL") == 1
    assert result.sources == ("assets/app.css", "src/app.js")
    assert result.source_hashes == hashes
    assert "GANGOF8:STYLE" not in result.content
    assert "GANGOF8:SCRIPT" not in result.content


@pytest.mark.parametrize(
    ("template", "message"),
    [
        (
            "<!doctype html><html>\n"
            "<!-- GANGOF8:STYLE assets/app.css -->\n</html>",
            "omitted dependencies",
        ),
        (
            "<!doctype html><html>\n"
            "<!-- GANGOF8:STYLE assets/app.css -->\n"
            "<!-- GANGOF8:SCRIPT src/app.js -->\n"
            "<!-- GANGOF8:SCRIPT src/app.js -->\n</html>",
            "more than once",
        ),
        (
            "<!doctype html><html>\n"
            "<!-- GANGOF8:STYLE assets/app.css -->\n"
            "<!-- GANGOF8:SCRIPT other.js -->\n</html>",
            "undeclared dependency",
        ),
    ],
)
def test_materializer_rejects_incomplete_or_unbound_templates(tmp_path, template, message):
    root = tmp_path / "stage"
    hashes = _write_manifest(root, {
        "assets/app.css": "body {}\n",
        "src/app.js": "globalThis.app = true;\n",
    })
    with pytest.raises(assembly.AssemblyError, match=message):
        assembly.materialize_html_inline(
            template, root, ["assets/app.css", "src/app.js"], hashes)


def test_materializer_rejects_changed_or_html_unsafe_accepted_sources(tmp_path):
    root = tmp_path / "stage"
    hashes = _write_manifest(root, {
        "assets/app.css": "body {}\n",
        "src/app.js": "globalThis.app = true;\n",
    })
    (root / "src" / "app.js").write_text("globalThis.app = false;\n", encoding="utf-8")
    with pytest.raises(assembly.AssemblyError, match="changed after acceptance"):
        assembly.materialize_html_inline(
            _template(), root, ["assets/app.css", "src/app.js"], hashes)

    hashes = _write_manifest(root, {
        "src/app.js": "const unsafe = '</script>';\n",
    }) | {"assets/app.css": hashes["assets/app.css"]}
    with pytest.raises(assembly.AssemblyError, match="cannot inline"):
        assembly.materialize_html_inline(
            _template(), root, ["assets/app.css", "src/app.js"], hashes)


def test_upstream_template_materializes_with_zero_model_calls(tmp_path):
    root = tmp_path / "stage"
    hashes = _write_manifest(root, {
        "shell.html": _template(),
        "assets/app.css": "body { margin: 0; }\n",
        "src/app.js": "globalThis.app = { ready: true };\n",
    })
    store = LogStore(tmp_path / "data")
    session = Session(
        session_id="s_zero_model",
        task=Task(task_id="t", session_id="s_zero_model", text="assemble"),
        workspace_root=str(root), required_files=["index.html"],
        runtime_dependencies=["shell.html", "assets/app.css", "src/app.js"],
        dependency_hashes=hashes, assembly_mode=assembly.HTML_INLINE,
        assembly_template="shell.html",
    )

    assert loop._prepare_deterministic_assembly(session, store)
    assert session.agent_calls == 0
    assert session.assembly_result["model_calls"] == 0
    assert [action.filename for action in session.proposed_actions] == ["index.html"]
    assert "globalThis.app" in session.proposed_actions[0].content


def test_owner_template_is_expanded_from_staging_instead_of_model_copied(tmp_path):
    root = tmp_path / "stage"
    hashes = _write_manifest(root, {
        "assets/app.css": "body { display: grid; }\n",
        "src/app.js": "globalThis.app = { ready: true };\n",
    })
    store = LogStore(tmp_path / "data")
    session = Session(
        session_id="s_owner_template",
        task=Task(task_id="t", session_id="s_owner_template", text="assemble"),
        workspace_root=str(root), required_files=["index.html"],
        runtime_dependencies=["assets/app.css", "src/app.js"],
        dependency_hashes=hashes, assembly_mode=assembly.HTML_INLINE,
        assembly_template=assembly.OWNER_TEMPLATE,
        proposed_actions=[ProposedAction(
            session_id="s_owner_template", kind="write_file", role=Role.panelist,
            filename="claude__index.html", content=_template(),
        )],
    )

    adopted, missing = loop._adopt_owned_package_artifacts(session, "claude", store)

    assert adopted == ["index.html"] and missing == []
    final = next(action for action in session.proposed_actions
                 if action.role == Role.implementer)
    assert "globalThis.app = { ready: true };" in final.content
    assert "GANGOF8:SCRIPT" not in final.content
    assert session.assembly_result["model_calls"] == 1


def test_owner_template_gets_one_call_and_cannot_request_dependency_reads(tmp_path):
    store = LogStore(tmp_path / "data")
    session = Session(
        session_id="s_one_call",
        task=Task(task_id="t", session_id="s_one_call", text="assemble"),
        assembly_mode=assembly.HTML_INLINE, assembly_template=assembly.OWNER_TEMPLATE,
    )
    member = CouncilMember(role=Role.panelist, agent="claude", active=True)
    calls: list[str] = []

    def call(_member, prompt, timeout_s=None):
        calls.append(prompt)
        return Contribution(
            round=0, role=Role.panelist, agent="claude",
            content='SKILL: read_file {"path":"src/app.js"}',
        )

    result = loop._panel_one(
        session, member, "emit compact glue", call, Governance(store), store, 180)
    assert result is None
    assert len(calls) == 1
    assert session.frontier_author_recoveries == {}


def test_expanded_assembly_is_never_sent_to_generic_model_repair(tmp_path):
    store = LogStore(tmp_path / "data")
    manager = SessionManager(store)
    member = CouncilMember(role=Role.panelist, agent="claude", active=True)
    session = Session(
        session_id="s_no_repair", council=Council(members=[member]),
        work_package_owner="claude", assembly_mode=assembly.HTML_INLINE,
        task=Task(task_id="t", session_id="s_no_repair", text="assemble"),
        proposed_actions=[ProposedAction(
            session_id="s_no_repair", kind="write_file", role=Role.implementer,
            filename="index.html", content="<!doctype html><html></html>",
        )],
    )
    calls: list[str] = []

    def repair_call(member, prompt):
        calls.append(prompt)
        raise AssertionError("expanded output must not enter a model repair prompt")

    assert not loop._repair_artifact_failure(
        session, manager, Governance(store), store, repair_call)
    assert calls == []


def test_planner_and_normalizer_preserve_explicit_assembly_contract(tmp_path):
    plan = (
        "PACKAGE 1: Release\nOWNER: claude\nAFTER: NONE\nCONTRACTS: NONE\n"
        "TASK: Assemble one single-file HTML release.\nOUTPUTS: index.html\n"
        "RELEASE: index.html\nREQUIRES: shell.html, assets/app.css, src/app.js\n"
        "ASSEMBLY: HTML_INLINE\nTEMPLATE: shell.html\n"
        "INTERFACE: final document\nCHECK: NONE\n"
    )
    package = goals.parse_milestones(plan)[0]
    assert package.assembly_mode == assembly.HTML_INLINE
    assert package.assembly_template == "shell.html"

    service = GangOf8Service(data_dir=tmp_path / "data")
    normalized, errors = service._normalize_work_packages([package], "Build one HTML file")
    assert not errors
    assert normalized[0].assembly_mode == assembly.HTML_INLINE
    assert normalized[0].assembly_template == "shell.html"


def test_pre_contract_single_file_package_is_structurally_inferred(tmp_path):
    package = GoalMilestone(
        index=0, package_id="wp_legacy", owner="claude", title="integration",
        task_text="Assemble all accepted sources inline into one single-file HTML app.",
        required_files=["index.html"], release_files=["index.html"],
        dependencies=["assets/app.css", "src/app.js"],
    )
    service = GangOf8Service(data_dir=tmp_path / "data")

    assert service._assembly_contract(package) == (
        assembly.HTML_INLINE, assembly.OWNER_TEMPLATE,
    )


def test_deterministic_release_gate_never_calls_a_frontier_verifier(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    release = "<!doctype html><html><body><main>ready</main></body></html>\n"
    output = stage / "index.html"
    output.write_text(release, encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    package = GoalMilestone(
        index=0, package_id="wp_1", title="release", task_text="assemble",
        owner="claude", required_files=["index.html"], release_files=["index.html"],
        accepted_hashes={"index.html": digest}, assembly_mode=assembly.HTML_INLINE,
        assembly_template=assembly.OWNER_TEMPLATE,
    )
    goal = Goal(text="build", staging_root=str(stage), milestones=[package])
    session = Session(
        session_id="s_release", required_files=["index.html"],
        task=Task(task_id="t", session_id="s_release", text="release"),
    )
    service = GangOf8Service(data_dir=tmp_path / "data")

    assert service._verify_goal_release(goal, session)
    assert session.agent_calls == 0
    assert session.quality_gate["stage"] == "deterministic_assembly_release"
    assert session.quality_gate["verdict"] == "PASS"
