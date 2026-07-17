"""Deterministic, manifest-bound final assembly regression coverage."""

from __future__ import annotations

import hashlib

import pytest

from gangof8 import assembly, goals, loop
import gangof8.service as service_module
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import (
    Contribution, Council, CouncilMember, Goal, GoalMilestone, ProposedAction,
    Role, Session, SessionStatus, Task,
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


def test_directive_missing_filename_names_the_problem_and_the_fix():
    """A real build had a model write `<!-- GANGOF8:STYLE -->` with no
    filename and repeat the IDENTICAL mistake three retries in a row: the old
    message here was just "malformed or non-standalone assembly directive",
    with no line, no quoted text, no example of the correct shape — nothing
    for a retry to act on. The message must now be specific enough that a
    model (or a human) can fix it without guessing.
    """
    template = (
        "<!doctype html>\n<html><head>\n"
        "<!-- GANGOF8:STYLE -->\n"
        "</head><body></body></html>\n"
    )
    with pytest.raises(assembly.AssemblyError) as exc:
        assembly.validate_template_directives(template)
    message = str(exc.value)
    assert "GANGOF8:STYLE -->" in message
    assert "GANGOF8:STYLE name.css" in message
    assert exc.value.fault_scope == "template"


def test_directive_sharing_its_line_names_the_stray_content():
    template = (
        "<!doctype html>\n<html><head>\n"
        "<!-- GANGOF8:STYLE assets/app.css --> extra text\n"
        "</head><body></body></html>\n"
    )
    with pytest.raises(assembly.AssemblyError) as exc:
        assembly.validate_template_directives(template)
    message = str(exc.value)
    assert "not standalone" in message
    assert "extra text" in message


def test_well_formed_directives_pass_validation():
    assert assembly.validate_template_directives(_template()) == (
        "assets/app.css", "src/app.js",
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
    with pytest.raises(assembly.AssemblyError, match="changed after acceptance") as changed:
        assembly.materialize_html_inline(
            _template(), root, ["assets/app.css", "src/app.js"], hashes)
    assert changed.value.fault_scope == "integrity"
    assert changed.value.fault_path == "src/app.js"

    hashes = _write_manifest(root, {
        "src/app.js": "const unsafe = '</script>';\n",
    }) | {"assets/app.css": hashes["assets/app.css"]}
    with pytest.raises(assembly.AssemblyError, match="cannot inline"):
        assembly.materialize_html_inline(
            _template(), root, ["assets/app.css", "src/app.js"], hashes)


def test_materializer_identifies_non_self_contained_stylesheet_owner(tmp_path):
    root = tmp_path / "stage"
    hashes = _write_manifest(root, {
        "assets/app.css": "@import url('https://example.test/font.css');\nbody {}\n",
        "src/app.js": "globalThis.app = true;\n",
    })

    with pytest.raises(assembly.AssemblyError, match="contains @import") as raised:
        assembly.materialize_html_inline(
            _template(), root, ["assets/app.css", "src/app.js"], hashes)

    assert raised.value.fault_scope == "dependency"
    assert raised.value.fault_path == "assets/app.css"


def test_materializer_rejects_directives_nested_in_script_or_style(tmp_path):
    root = tmp_path / "stage"
    hashes = _write_manifest(root, {
        "assets/app.css": "body {}\n",
        "src/app.js": "globalThis.app = true;\n",
    })
    nested = (
        "<!doctype html><html><head>\n"
        "<!-- GANGOF8:STYLE assets/app.css -->\n"
        "</head><body><script id=\"arcade-scripts\">\n"
        "<!-- GANGOF8:SCRIPT src/app.js -->\n"
        "</script></body></html>"
    )

    with pytest.raises(assembly.AssemblyError, match="nested inside <script>"):
        assembly.materialize_html_inline(
            nested, root, ["assets/app.css", "src/app.js"], hashes)


def test_package_verification_rejects_nested_assembly_template_early(tmp_path):
    template = tmp_path / "index.template.html"
    template.write_text(
        "<!doctype html><html><body><script>\n"
        "<!-- GANGOF8:SCRIPT src/app.js -->\n"
        "</script></body></html>",
        encoding="utf-8",
    )
    store = LogStore(tmp_path / "data")
    session = Session(
        session_id="s_nested_template",
        task=Task(task_id="t", session_id="s_nested_template", text="build template"),
        required_files=["index.template.html"],
        proposed_actions=[ProposedAction(
            session_id="s_nested_template",
            kind="write_file",
            role=Role.implementer,
            filename="index.template.html",
            status="executed",
            result_path=str(template),
        )],
    )

    assert loop._verify_artifact_outputs(session, store, require_file=True) is False
    assert "assembly template contract failed" in session.unresolved[-1]
    assert "nested inside <script>" in session.unresolved[-1]


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


def test_zero_call_assembly_provenance_does_not_credit_nominal_owner(tmp_path):
    service = GangOf8Service(data_dir=tmp_path / "data")
    session = Session(
        session_id="s_assembly_provenance",
        task=Task(task_id="t", session_id="s_assembly_provenance", text="assemble"),
        assembly_result={
            "mode": assembly.HTML_INLINE,
            "model_calls": 0,
            "template_hash": "template-sha",
            "source_hashes": {"src/app.js": "source-sha"},
        },
        package_output_history={
            "index.html": [{"status": "completed", "agent": "claude"}],
        },
    )

    records = service._accepted_output_provenance(
        session, ["index.html"], {"index.html": "release-sha"})

    assert records["index.html"]["method"] == "deterministic_assembly"
    assert records["index.html"]["agent"] is None
    assert records["index.html"]["source_hashes"] == {"src/app.js": "source-sha"}


def test_deterministic_release_continues_into_semantic_frontier_review(tmp_path, monkeypatch):
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
    service.panel = ["claude", "codex"]
    monkeypatch.setattr(service.registry, "names", lambda: ["claude", "codex"])

    def semantic_pass(current, _registry, _store, member, _prompt, timeout_s=None):
        del current, _registry, _store, timeout_s
        return Contribution(
            round=0, role=member.role, agent=member.agent,
            content="CHECK R1: PASS - inspected the final behavior\nVERDICT: PASS",
        )

    monkeypatch.setattr(service_module, "_agent_call", semantic_pass)

    assert service._verify_goal_release(goal, session)
    assert session.quality_gate["verdict"] == "PASS"
    assert session.quality_gate["verifier"] == "codex"
    assert session.quality_gate["deterministic_preflight"]["verdict"] == "PASS"


def test_frontier_pass_with_edits_applies_then_requires_clean_confirmation(
    tmp_path, monkeypatch,
):
    stage = tmp_path / "stage"
    stage.mkdir()
    output = stage / "index.html"
    output.write_text(
        "<!doctype html><html><body><script>const player = { x: 0 };</script></body></html>",
        encoding="utf-8",
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    package = GoalMilestone(
        index=0, package_id="wp_1", title="release", task_text="assemble",
        owner="claude", required_files=["index.html"], release_files=["index.html"],
        accepted_hashes={"index.html": digest}, assembly_mode=assembly.HTML_INLINE,
        assembly_template=assembly.OWNER_TEMPLATE,
    )
    goal = Goal(
        text="The release must initialize the player position.",
        staging_root=str(stage), milestones=[package],
    )
    session = Session(
        session_id="s_release_repair", status=SessionStatus.deliberating,
        workspace_root=str(stage), required_files=["index.html"],
        task=Task(task_id="t", session_id="s_release_repair", text=goal.text),
    )
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.panel = ["claude", "codex"]
    monkeypatch.setattr(service.registry, "names", lambda: ["claude", "codex"])
    calls = []

    def semantic_review(current, _registry, _store, member, _prompt, timeout_s=None):
        del current, _registry, _store, member, _prompt, timeout_s
        calls.append(1)
        if len(calls) == 1:
            content = (
                "CHECK R1: PASS - repaired below\n"
                "```text\n===== EDIT: index.html =====\n"
                "OLD:\nconst player = { x: 0 };\n"
                "NEW:\nconst player = { x: 0, y: 10 };\n```\n"
                "VERDICT: PASS"
            )
        else:
            content = "CHECK R1: PASS - current bytes initialize x and y\nVERDICT: PASS"
        return Contribution(
            round=0, role=Role.panelist, agent="codex", content=content,
        )

    monkeypatch.setattr(service_module, "_agent_call", semantic_review)

    assert service._verify_goal_release(goal, session)
    assert len(calls) == 2
    assert "const player = { x: 0, y: 10 };" in output.read_text(encoding="utf-8")
    assert session.quality_gate["verdict"] == "PASS"
    assert session.quality_gate["repairs_applied"] == 1
    assert goal.release_defects == []


def test_semantic_frontier_can_block_clean_deterministic_assembly(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    output = stage / "index.html"
    output.write_text("<!doctype html><html><body><script>let ready=true;</script></body></html>",
                      encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    package = GoalMilestone(
        index=0, package_id="wp_1", title="release", task_text="assemble",
        owner="claude", required_files=["index.html"], release_files=["index.html"],
        accepted_hashes={"index.html": digest}, assembly_mode=assembly.HTML_INLINE,
        assembly_template=assembly.OWNER_TEMPLATE,
    )
    goal = Goal(text="The pause control must work", staging_root=str(stage), milestones=[package])
    session = Session(
        session_id="s_release_fail", status=SessionStatus.deliberating,
        required_files=["index.html"],
        task=Task(task_id="t", session_id="s_release_fail", text=goal.text),
    )
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.panel = ["claude", "codex"]
    monkeypatch.setattr(service.registry, "names", lambda: ["claude", "codex"])

    def semantic_fail(current, _registry, _store, member, _prompt, timeout_s=None):
        del current, _registry, _store, timeout_s
        return Contribution(
            round=0, role=member.role, agent=member.agent,
            content="CHECK R1: FAIL - no pause handler exists\n"
                    "DEFECT: pause control is inert\nVERDICT: FAIL",
        )

    monkeypatch.setattr(service_module, "_agent_call", semantic_fail)

    assert not service._verify_goal_release(goal, session)
    assert session.quality_gate["deterministic_preflight"]["verdict"] == "PASS"
    assert session.quality_gate["verdict"] == "FAIL"
    assert session.status == SessionStatus.failed
