"""Phase 3: established-folder path extraction + the promote-time target ask.

A path the user references in the prompt becomes the session's established folder
(read source + approval-gated promote target). A build that references NO path
runs freely in the sandbox; only when it wants to DELIVER (PROMOTE) does the
coordinator ask WHERE — at delivery time, never up front.
"""

import pytest

from gangof8.classifier import classify
from gangof8.models import Role, SessionStatus, TaskType
from gangof8.paths import (extract_delivery_target, extract_established_root,
                               prior_deliverable_files)
from gangof8.registry import AdapterResult
from gangof8.adapters.mock import MockAdapter
from gangof8.service import GangOf8Service


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


def test_extract_stops_at_real_dir_ignoring_trailing_prose(tmp_path):
    # THE bug: "saved to C:\...\tmp and opened directly in a browser..." created a
    # folder literally named with the whole sentence. The real, existing dir wins.
    text = f"save centipede.html to {tmp_path} and open it directly in a browser with no server"
    assert extract_established_root(text) == str(tmp_path.resolve())


def test_extract_new_target_drops_trailing_prose(tmp_path):
    # A brand-new folder inside an existing parent, followed by prose: keep only
    # the new folder name, not the sentence.
    target = tmp_path / "newgame"
    text = f"build it into {target} and then run it in a browser please"
    assert extract_established_root(text) == str(target.resolve())


def test_extract_preserves_legit_dir_with_spaces(tmp_path):
    # A real folder whose name genuinely contains spaces must survive intact.
    spaced = tmp_path / "My Cool Project"
    spaced.mkdir()
    assert extract_established_root(f"save to {spaced}") == str(spaced.resolve())


def test_extract_new_file_with_prose_returns_parent(tmp_path):
    # "…\out.html and open it" → established root is the existing parent dir.
    text = fr"write {tmp_path}\out.html and open it in a browser"
    assert extract_established_root(text) == str(tmp_path.resolve())


def test_extract_apostrophe_path_not_truncated(tmp_path):
    # THE Benny bug: an apostrophe in the filename truncated the path, and a
    # subfolder sharing the pre-apostrophe prefix got selected as the root.
    parent = tmp_path / "Benny"
    parent.mkdir()
    (parent / "Benny").mkdir()  # the decoy the truncated path used to point at
    src = parent / "Benny's Splash.txt"
    src.write_text("splash", encoding="utf-8")
    text = f"Read the first story at: {src}\nThen write a sequel."
    # the file's REAL parent, not parent/Benny
    assert extract_established_root(text) == str(parent.resolve())


def test_extract_quoted_apostrophe_path(tmp_path):
    # A double-quoted path with an apostrophe must not end at the apostrophe.
    src = tmp_path / "Benny's Splash.txt"
    src.write_text("x", encoding="utf-8")
    assert extract_established_root(f'read the story in "{src}" first') == str(tmp_path.resolve())


def test_bare_drive_root_never_wins_over_a_specific_path(tmp_path):
    # A bare filesystem/drive root EXISTS on disk, but must never be chosen as the
    # established folder — a specific target path wins instead (live: the Enhance
    # amplifier mentioned `C:\` and it beat the actual save path; promoting into
    # C:\ would be dangerous).
    target = tmp_path / "proj"
    target.mkdir()
    root = tmp_path.anchor  # "C:\" on Windows, "/" on POSIX — a real root
    text = f'engineer it on the "{root}" drive, saved into "{target}"'
    assert extract_established_root(text) == str(target.resolve())


def test_bare_root_alone_yields_no_established_folder(tmp_path):
    # only a bare root referenced ⇒ None (the greenfield gate asks where to save)
    root = tmp_path.anchor
    assert extract_established_root(f'just put it on "{root}"') is None


# --- explicit delivery target (save-dest ≠ read-source) -----------------------


def test_delivery_target_distinguishes_save_dest_from_read_source(tmp_path):
    """THE Benny overwrite bug: the task read a source AND named a separate save
    folder. The FIRST path (the read source) became the promote target and the
    story overwrote the source's canon. The read source and the save target must
    resolve independently."""
    src_dir = tmp_path / "Benny"
    src_dir.mkdir()
    src_file = src_dir / "Benny's Splash.txt"
    src_file.write_text("canon", encoding="utf-8")
    out_dir = tmp_path / "out"  # save target — need not exist yet
    text = (f"Read the first story at: {src_file}\n"
            f"Write the sequel and save it as a .txt file in: {out_dir}")
    assert extract_established_root(text) == str(src_dir.resolve()), "read source"
    assert extract_delivery_target(text) == str(out_dir.resolve()), "save target"


def test_delivery_target_none_without_explicit_save_dest(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    # 'add a feature to the app in <path>' names a LOCATION, not a save target
    assert extract_delivery_target(f'add a feature to the app in "{d}"') is None
    # a save verb with no destination path is not a delivery instruction
    assert extract_delivery_target("write the story and save it to disk") is None
    assert extract_delivery_target("summarize the notes") is None


def test_promote_delivers_to_declared_target_not_source(tmp_path):
    """With a delivery target set, promote lands THERE — and never writes into
    the read-source folder (which keeps its original file byte-for-byte)."""
    from gangof8 import executor
    from gangof8.logstore import LogStore
    from gangof8.models import ProposedAction
    from gangof8.sessions import SessionManager
    from gangof8.skills import _promote

    source = tmp_path / "Benny"
    source.mkdir()
    (source / "story.txt").write_text("ORIGINAL CANON — do not touch", encoding="utf-8")
    dest = tmp_path / "out"  # explicit save target (does not exist yet)
    store = LogStore(tmp_path / "data")
    s = SessionManager(store).create("read + save", source="test")
    s.established_root = str(source)
    s.delivery_root = str(dest)
    sandbox = executor.artifacts_dir(store.data_dir, s.session_id)
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "story.txt").write_text("NEW STORY", encoding="utf-8")

    action = ProposedAction(session_id=s.session_id, kind="promote",
                            filename="story.txt", args={"filename": "story.txt"})
    _promote(s, action, store.data_dir)

    assert (dest / "story.txt").read_text(encoding="utf-8") == "NEW STORY"
    assert (source / "story.txt").read_text(encoding="utf-8") == "ORIGINAL CANON — do not touch"


def test_promote_approval_flags_overwrite_vs_new(tmp_path):
    """The approval summary must say whether a promote OVERWRITES an existing file
    — so a standing 'approve all promote' can't silently clobber canon."""
    from gangof8.governance import Governance
    from gangof8.logstore import LogStore
    from gangof8.models import ProposedAction
    from gangof8.sessions import SessionManager

    dest = tmp_path / "out"
    dest.mkdir()
    store = LogStore(tmp_path / "data")
    gov = Governance(store)
    s = SessionManager(store).create("t", source="test")
    s.delivery_root = str(dest)

    def _promote_action():
        return ProposedAction(session_id=s.session_id, kind="promote",
                              role=Role.implementer, filename="story.txt",
                              args={"filename": "story.txt"})

    # new file → "(new file)"
    new_ap = gov.authorize_action(s, _promote_action())
    assert new_ap is not None and "new file" in new_ap.action
    assert str(dest) in new_ap.action

    # pre-existing file → "(OVERWRITES an existing file)"
    (dest / "story.txt").write_text("existing canon", encoding="utf-8")
    over_ap = gov.authorize_action(s, _promote_action())
    assert over_ap is not None and "OVERWRITES" in over_ap.action


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
    svc = GangOf8Service(data_dir=tmp_path / "data")
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
    svc = GangOf8Service(data_dir=tmp_path / "data")
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
    from gangof8.executor import ExecutionError, execute
    from gangof8.logstore import LogStore
    from gangof8.models import ProposedAction
    from gangof8.sessions import SessionManager

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
    from gangof8 import loop
    from gangof8.classifier import classify
    from gangof8.logstore import LogStore
    from gangof8.roles import build_council
    from gangof8.sessions import SessionManager

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


def test_named_txt_source_is_pre_read_regardless_of_extension(tmp_path):
    """A writing task that names a .txt source ('read Benny's Splash.txt and match
    its style') must get that file's REAL content up front. The code-extension
    overview filter skipped prose sources, so seats invented the canon (they wrote
    owner 'Emma'/'Sam' when the real owner was Grace). A produces_output task must
    also NOT carry the analysis-only 'HOW TO RECOMMEND' directive."""
    from gangof8 import loop
    from gangof8.classifier import classify
    from gangof8.logstore import LogStore
    from gangof8.sessions import SessionManager

    est = tmp_path / "Benny"
    est.mkdir()
    (est / "Benny's Splash.txt").write_text(
        "Benny loved three things: his yellow ball, his person named Grace, and naps.",
        encoding="utf-8")
    store = LogStore(tmp_path / "data")
    task = ("Read Benny's Splash.txt and write the next story in the same style, "
            "saving it as story.txt")
    s = SessionManager(store).create(task, source="test")
    s.established_root = str(est)
    s.classification = classify(task)
    assert s.classification.task_type == TaskType.content  # Fix A routed it to content

    overview = loop._established_overview(s, store.data_dir)
    assert "named Grace" in overview, "the named .txt source is read into the overview"
    assert "the task named" in overview                    # labeled as source material
    assert "HOW TO RECOMMEND" not in overview              # no analysis directive on a build


def test_benny_scenario_wires_source_dest_and_classifies_content(tmp_path):
    """The whole failing Benny task, end to end at the service wiring: read a .txt
    source in one folder, save the new story in ANOTHER. All the fixes must
    compose — content classification (not code), the source folder bound for
    reads, a SEPARATE save target for delivery, and the named source pre-read
    into the overview (so seats match the canon instead of inventing it)."""
    from gangof8 import loop
    from gangof8.classifier import classify
    from gangof8.service import GangOf8Service

    src_dir = tmp_path / "Benny"
    src_dir.mkdir()
    src_file = src_dir / "Benny's Splash.txt"
    src_file.write_text(
        "Benny loved three things: his ball, his person named Grace, and naps.",
        encoding="utf-8")
    out_dir = tmp_path / "out"
    task = (f"You are a children's book author. Read the first story at: {src_file}\n"
            f"Write story #2 about Benny's first car ride and save it as a .txt "
            f"file in: {out_dir}")

    svc = GangOf8Service(data_dir=tmp_path / "data")
    session = svc._open(task, source="test", budgets=None)
    assert session.established_root == str(src_dir.resolve()), "read source bound"
    assert session.delivery_root == str(out_dir.resolve()), "separate save target bound"

    session.classification = classify(task)
    assert session.classification.task_type == TaskType.content   # Fix A
    assert session.classification.produces_output is True

    overview = loop._established_overview(session, svc.store.data_dir)
    assert "named Grace" in overview                              # Fix E: source pre-read
    assert "HOW TO RECOMMEND" not in overview                     # Fix E: no analysis directive


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
    svc = GangOf8Service(data_dir=tmp_path / "data")
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
    svc = GangOf8Service(data_dir=tmp_path / "data")
    svc.registry.register(_PromoteAdapter())
    session = svc.run(f'add a feature to the app in "{established}"', source="test")
    promote_approval = next(a for a in session.approvals if a.category == "promote")
    done = svc.approve(session.session_id, promote_approval.approval_id, approved=False)
    assert done.status == SessionStatus.done
    assert not (established / "feature.py").exists()  # denial keeps real code untouched


def test_empty_workspace_clears_contents_only(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path / "data")
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


def test_followup_autofills_missing_promote_for_delivered_file(tmp_path):
    """Follow-up safety net: a revised file that was ALREADY delivered but whose
    PROMOTE line the lead forgot gets an auto-synthesized promote, so the update
    reaches the user instead of stranding in the sandbox."""
    from gangof8 import loop
    from gangof8.logstore import LogStore
    from gangof8.models import ProposedAction
    from gangof8.sessions import SessionManager

    est = tmp_path / "game"
    est.mkdir()
    (est / "index.html").write_text("<old build>", encoding="utf-8")  # delivered on turn 1
    store = LogStore(tmp_path / "data")
    s = SessionManager(store).create("make it better", source="test")
    s.established_root = str(est)
    # the follow-up re-authored the file but emitted NO promote line
    s.proposed_actions.append(ProposedAction(
        session_id=s.session_id, kind="write_file", role=Role.implementer,
        filename="index.html", args={"filename": "index.html", "content": "<new>"}))

    loop._ensure_redelivery_promotes(s, store)

    promotes = [a for a in s.proposed_actions if a.kind == "promote"]
    assert [a.filename for a in promotes] == ["index.html"]

    # idempotent: a second pass (e.g. on resume) does not duplicate
    loop._ensure_redelivery_promotes(s, store)
    assert len([a for a in s.proposed_actions if a.kind == "promote"]) == 1


def test_declared_destination_promotes_brand_new_files_too(tmp_path):
    """POLICY CHANGE (live failure 2026-07-05): an established_root only ever
    comes from the user naming a destination — so a brand-new authored file is
    PROPOSED for delivery there even on a first (greenfield) run. The old
    already-delivered-only rule left the user's explicitly named folder empty
    while the run reported success. Still human-gated: this proposes; the
    approval click ships. With NO established root, nothing is proposed."""
    from gangof8 import loop
    from gangof8.logstore import LogStore
    from gangof8.models import ProposedAction
    from gangof8.sessions import SessionManager

    est = tmp_path / "game"
    est.mkdir()  # empty — nothing delivered yet
    store = LogStore(tmp_path / "data")
    s = SessionManager(store).create("scaffold a helper", source="test")
    s.established_root = str(est)
    s.proposed_actions.append(ProposedAction(
        session_id=s.session_id, kind="write_file", role=Role.implementer,
        filename="helper.js", args={"filename": "helper.js", "content": "//new"}))

    loop._ensure_redelivery_promotes(s, store)
    assert [a.filename for a in s.proposed_actions if a.kind == "promote"] == ["helper.js"]

    # no declared destination → no auto-promote (nothing is force-shipped)
    s2 = SessionManager(store).create("scaffold another helper", source="test")
    s2.proposed_actions.append(ProposedAction(
        session_id=s2.session_id, kind="write_file", role=Role.implementer,
        filename="helper.js", args={"filename": "helper.js", "content": "//new"}))
    loop._ensure_redelivery_promotes(s2, store)
    assert not any(a.kind == "promote" for a in s2.proposed_actions)


# --- Fix 1 + Fix 5: source digest for judges, prior-deliverable warning -------


def test_source_digest_returns_named_source_not_prior_deliverable(tmp_path):
    """Fix 1: the digest fed to the blind judges is the task's NAMED source, and
    NOT a prior copy of the deliverable sitting in the same folder (referenced by
    title without extension — that is a prior answer, not source)."""
    from gangof8 import loop
    from gangof8.logstore import LogStore
    from gangof8.sessions import SessionManager

    est = tmp_path / "Benny"
    est.mkdir()
    (est / "Benny's Splash.txt").write_text("SPLASH SPREAD 1 ILLUSTRATION PROMPT", encoding="utf-8")
    (est / "Benny's First Car Ride.txt").write_text("A PRIOR ANSWER", encoding="utf-8")
    s = SessionManager(LogStore(tmp_path / "data")).create(
        "Read Benny's Splash.txt and write Benny's First Car Ride, save as a .txt", source="test")
    s.established_root = str(est)
    digest = loop._source_digest(s)
    assert "SPLASH SPREAD 1" in digest        # the named source is included
    assert "A PRIOR ANSWER" not in digest      # the prior deliverable is NOT source


def test_prior_deliverable_files_flags_title_match_only(tmp_path):
    """Fix 5: a file referenced by TITLE (stem in task) but not as a named input
    (full name not in task) is a prior/existing version of the deliverable; the
    named source (full name in task) and unrelated files are not flagged."""
    est = tmp_path / "Benny"
    est.mkdir()
    (est / "Benny's Splash.txt").write_text("x", encoding="utf-8")            # named source
    (est / "Benny's First Car Ride.txt").write_text("prior", encoding="utf-8")  # prior deliverable
    (est / "unrelated.txt").write_text("y", encoding="utf-8")
    task = "Read Benny's Splash.txt and write Benny's First Car Ride, save as a .txt"
    assert prior_deliverable_files(str(est), task) == ["Benny's First Car Ride.txt"]
    assert prior_deliverable_files(None, task) == []


def test_open_warns_when_source_folder_holds_a_prior_deliverable(tmp_path):
    """Fix 5, at the service wiring: opening the Benny task whose source folder
    already contains 'Benny's First Car Ride.txt' surfaces an up-front warning so
    a shipped copy of a prior answer is never silent."""
    src_dir = tmp_path / "Benny"
    src_dir.mkdir()
    (src_dir / "Benny's Splash.txt").write_text("Grace and the ball.", encoding="utf-8")
    (src_dir / "Benny's First Car Ride.txt").write_text("A PRIOR ANSWER", encoding="utf-8")
    src_file = src_dir / "Benny's Splash.txt"
    task = (f"Read the first story at: {src_file}\nWrite story #2, Benny's First "
            f"Car Ride, and save it as a .txt file.")
    svc = GangOf8Service(data_dir=tmp_path / "data")
    session = svc._open(task, source="test", budgets=None)
    assert any("Benny's First Car Ride.txt" in u and "PRIOR" in u
               for u in session.unresolved), "prior deliverable surfaced at open"
