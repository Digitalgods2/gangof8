"""Lead-driven model: the lead drives a task directly, pulls in talents only on
demand, and finishes a cut-off file by continuing it instead of re-drafting."""


import pytest

from gangof8 import executor, loop
from gangof8.adapters.mock import MockAdapter
from gangof8.logstore import LogStore
from gangof8.models import Contribution, ProposedAction, Role, SessionStatus
from gangof8.registry import AdapterResult
from gangof8.service import GangOf8Service
from gangof8.sessions import SessionManager


def _draft_session(tmp_path, content: str):
    store = LogStore(tmp_path)
    s = SessionManager(store).create("t", source="test")
    s.contributions.append(Contribution(round=0, role=Role.lead, agent="mock", content=content))
    return store, s


# --- Findings 1 & 6: truncation detection is reliable, not a guess -----------


def test_looks_truncated_only_flags_unclosed_full_documents(tmp_path):
    big_js = tmp_path / "app.js"
    big_js.write_text("// module\n" + "const x = 1;\n" * 250 + "})();", encoding="utf-8", newline="")
    assert big_js.stat().st_size > 2000 and loop._looks_truncated(big_js) is False, \
        "a complete code file with no trailing newline must NOT look truncated"

    cut = tmp_path / "page.html"
    cut.write_text("<!doctype html><html><body>hi", encoding="utf-8")
    assert loop._looks_truncated(cut) is True  # opened a doc, never closed it

    frag = tmp_path / "card.html"
    frag.write_text("<div class='card'>Hi</div>", encoding="utf-8")
    assert loop._looks_truncated(frag) is False  # an intentional fragment


def test_verify_accepts_html_fragment_but_rejects_unclosed_doc(tmp_path):
    store, s = _draft_session(tmp_path, "x")
    frag = tmp_path / "card.html"
    frag.write_text("<section><h2>Card</h2></section>", encoding="utf-8")
    s.proposed_actions.append(ProposedAction(
        session_id=s.session_id, kind="write_file", filename="card.html",
        status="executed", result_path=str(frag)))
    assert loop._verify_artifact_outputs(s, store) is True  # fragment ok

    store2, s2 = _draft_session(tmp_path, "x")
    bad = tmp_path / "page2.html"
    bad.write_text("<!doctype html><html><body>hi", encoding="utf-8")
    s2.proposed_actions.append(ProposedAction(
        session_id=s2.session_id, kind="write_file", filename="page2.html",
        status="executed", result_path=str(bad)))
    assert loop._verify_artifact_outputs(s2, store2) is False  # unclosed full doc


# --- Finding 3: prose 'Edit the ...' must not split a file body --------------


def test_collect_does_not_split_body_on_prose_marker_line(tmp_path):
    store, s = _draft_session(tmp_path, (
        "ARTIFACT: README.md\n"
        "# App\n\n## Setup\nEdit the .env file with your key.\n\n## Usage\nRun the app.\n"
        "ARTIFACT: app.py\n"
        "print('hi')\n"
    ))
    loop._collect_proposals(s, store)
    writes = {a.filename: a.content for a in s.proposed_actions}
    assert set(writes) == {"README.md", "app.py"}
    assert "Edit the .env file" in writes["README.md"]  # body not truncated at 'Edit'
    assert "## Usage" in writes["README.md"] and "Run the app." in writes["README.md"]
    assert writes["app.py"] == "print('hi')"


# --- Finding 5: doctests / '===' rules inside EDIT don't mis-split -----------


def test_edit_marker_tolerates_doctest_in_new(tmp_path):
    store, s = _draft_session(tmp_path, (
        "EDIT: mod.py\n"
        "<<<<<<< OLD\n"
        "def f(): pass\n"
        "=======\n"
        'def f():\n    """\n    >>> f()\n    1\n    """\n    return 1\n'
        ">>>>>>> NEW\n"
    ))
    loop._collect_proposals(s, store)
    edit = next(a for a in s.proposed_actions if a.kind == "edit_file")
    assert ">>> f()" in edit.args["new"]                 # doctest survived
    assert edit.args["new"].strip().endswith("return 1")  # NEW captured to the end


@pytest.fixture()
def service(tmp_path):
    return GangOf8Service(data_dir=tmp_path)


# --- easy task: one lead pass, no delegation ---------------------------------


def test_easy_task_runs_without_delegation(service):
    session = service.run("What is SQLite?", source="test")
    assert session.status == SessionStatus.done
    assert session.final is not None and session.final.answer
    # no talent was pulled in
    assert session.council.get(Role.researcher).active is False
    # exactly one lead contribution drove the task (plus the summarizer compose)
    lead_contribs = [c for c in session.contributions if c.role == Role.lead]
    assert len(lead_contribs) == 1


# --- delegation end to end ---------------------------------------------------


class DelegatingLead:
    """Lead asks a specialist once, then finishes using the result."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()
        self.lead_calls = 0
        self.researcher_calls = 0

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            self.lead_calls += 1
            if "Results from the talents" in prompt:
                return AdapterResult(
                    content="ARTIFACT: out.txt\nfinal answer informed by research\n",
                    duration_ms=1)
            return AdapterResult(
                content="CONSULT: researcher - need the current best practice for X",
                duration_ms=1)
        if role == Role.researcher:
            self.researcher_calls += 1
            return AdapterResult(content="Best practice is Y.", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_lead_delegates_then_finishes(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    adapter = DelegatingLead()
    svc.registry.register(adapter)

    # a standard-complexity task (>8 words) so the budget has room for a
    # delegation plus the lead's follow-up call
    session = svc.run(
        "Write a short report comparing two storage options and recommend the "
        "best approach for our small team.",
        source="test",
    )

    assert session.status == SessionStatus.done
    assert adapter.lead_calls == 2, "lead is re-called once after the consult"
    assert adapter.researcher_calls == 1, "the talent was pulled in exactly once"
    # the delegated specialist was activated and its answer recorded
    assert svc.manager.load(session.session_id).council.get(Role.researcher).active is True
    assert any(c.role == Role.researcher and "Best practice is Y" in c.content
               for c in session.contributions)
    # the lead's final artifact was written
    assert any(a.filename == "out.txt" and a.status == "executed"
               for a in session.proposed_actions)


# --- sub-agent tier: a consulted specialist consults one level deeper ---------


class NestedDelegatingLead:
    """lead → architect (L1) → code_generator (L2). code_generator ALSO emits a
    CONSULT to red_team, which MUST be ignored (depth cap = 2)."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()
        self.lead_calls = 0
        self.architect_calls = 0
        self.codegen_calls = 0
        self.redteam_calls = 0

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            self.lead_calls += 1
            if "Results from the talents" in prompt:
                return AdapterResult(content="ARTIFACT: out.txt\nfinal answer\n", duration_ms=1)
            return AdapterResult(
                content="CONSULT: architect - design the storage module", duration_ms=1)
        if role == Role.architect:
            self.architect_calls += 1
            return AdapterResult(
                content="Design: layered.\nCONSULT: code_generator - implement the core fn",
                duration_ms=1)
        if role == Role.code_generator:
            self.codegen_calls += 1
            # This deeper CONSULT must NOT be honored — we are at the depth cap.
            return AdapterResult(
                content="Impl done in Python.\nCONSULT: red_team - probe for abuse",
                duration_ms=1)
        if role == Role.red_team:
            self.redteam_calls += 1
            return AdapterResult(content="should never run", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_specialist_consults_subagent_bounded_by_depth(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    adapter = NestedDelegatingLead()
    svc.registry.register(adapter)

    session = svc.run(
        "Write a short report comparing two storage options and recommend the "
        "best approach for our small team.",
        source="test",
    )

    assert session.status == SessionStatus.done
    assert adapter.lead_calls == 2, "lead re-called once after its consult resolves"
    assert adapter.architect_calls == 1, "level-1 specialist pulled in once"
    assert adapter.codegen_calls == 1, "the sub-agent tier fired (architect → code_generator)"
    assert adapter.redteam_calls == 0, "depth cap blocks a third level (code_generator → red_team)"
    # the sub-agent's answer is folded up so the lead can use it
    assert any(c.role == Role.code_generator and "Impl done" in c.content
               for c in session.contributions)


# --- truncation: finish a cut-off file, don't re-draft -----------------------


def test_looks_truncated_detects_unclosed_html(tmp_path):
    good = tmp_path / "good.html"
    good.write_text("<html><body>hi</body></html>\n", encoding="utf-8")
    assert loop._looks_truncated(good) is False

    cut = tmp_path / "cut.html"
    cut.write_text("<html><body><p>start", encoding="utf-8")
    assert loop._looks_truncated(cut) is True


def test_clean_artifact_body_drops_fence_and_trailing_prose():
    """The real bug: the agent wrapped the file in a ```fence and appended an
    explanation; the closing fence + prose must NOT end up in the file."""
    raw = (
        "```html\n"
        "<!DOCTYPE html>\n<html><head><title>x</title></head>\n"
        "<body><h1>Hi</h1></body>\n</html>\n"
        "```\n"
        "Done. This is a complete, working single-file calendar. What it does: ...\n"
    )
    body = loop._clean_artifact_body(raw, "index.html")
    assert body.startswith("<!DOCTYPE html>")
    assert body.rstrip().endswith("</html>")
    assert "```" not in body
    assert "Done. This is a complete" not in body
    # a markdown file (no html markers) still strips a wrapping fence + keeps body
    md = loop._clean_artifact_body("```markdown\n# Title\n\nbody\n```", "notes.md")
    assert md == "# Title\n\nbody"


def test_clean_artifact_body_preserves_a_readme_with_code_blocks():
    """A README that legitimately contains ``` code blocks (it is NOT wrapped in a
    single fence) must be delivered verbatim — the extractor must never mangle its
    fences by treating the first block's opener as a wrapper (Finding 4)."""
    raw = "```bash\nnpm install\n```\n\nThen run:\n\n```js\nconsole.log(1);\n```\n"
    body = loop._clean_artifact_body(raw, "README.md")
    assert body == raw.strip()  # preserved exactly — not unwrapped, not truncated


def test_clean_artifact_body_strips_a_clean_single_wrap():
    """A whole file wrapped in exactly one ```lang … ``` fence IS unwrapped."""
    assert loop._clean_artifact_body("```python\nprint(1)\n```", "a.py") == "print(1)"
    # trailing prose after the single closing fence is dropped too
    assert loop._clean_artifact_body(
        "```python\nprint(1)\n```\nThat's the file.", "a.py") == "print(1)"


def test_clean_artifact_body_html_with_close_tag_in_a_js_string():
    """rfind targets the REAL closing tag, not a </html> sitting in a JS string."""
    raw = (
        "<!DOCTYPE html><html><body><script>var s = \"</html>\";</script></body></html>\n"
        "\n```\nsome trailing notes\n"
    )
    body = loop._clean_artifact_body(raw, "index.html")
    assert body.endswith("</html>")
    assert "trailing notes" not in body
    assert 'var s = "</html>"' in body  # the inner string is preserved


def test_clean_artifact_body_raw_unfenced_is_unchanged():
    raw = "print('hello')\n"
    assert loop._clean_artifact_body(raw, "main.py") == "print('hello')"


def test_artifact_end_marker_excludes_everything_after_file():
    reply = (
        "ARTIFACT: src/games/asteroids.js\n"
        "globalThis.Asteroids = class Asteroids {};\n"
        "END_ARTIFACT\n"
        "## What I built\nA polished arcade implementation.\n"
    )
    writes = [a for a in loop._parse_proposals("s_end", reply)
              if a.kind == "write_file"]
    assert len(writes) == 1
    assert writes[0].filename == "src/games/asteroids.js"
    assert writes[0].content == "globalThis.Asteroids = class Asteroids {};"


def test_raw_javascript_trailing_markdown_is_trimmed_conservatively():
    raw = (
        "(function(global){\n"
        "  global.Asteroids = function Asteroids() {};\n"
        "})(globalThis);\n\n"
        "**What's in the module**\n"
        "- complete gameplay and controls\n"
    )
    body = loop._clean_artifact_body(raw, "src/games/asteroids.js")
    assert body.rstrip().endswith("})(globalThis);")
    assert "What's in the module" not in body


class PreambleArtifactLead:
    """Lead emits a short rationale THEN a complete file — and nothing else. The
    summarizer must NOT be called for a build."""

    name = "mock"

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            return AdapterResult(content=(
                "I'll build this as a single self-contained file using vanilla JS.\n"
                "ARTIFACT: index.html\n"
                "<!DOCTYPE html><html><head><title>Cal</title></head>"
                "<body><table><tr><td>1</td></tr></table></body></html>\n"
            ), duration_ms=1)
        raise AssertionError(f"a build must not call a second model (got {role})")


def test_build_uses_deterministic_summary_and_one_call(tmp_path):
    # panel=[] → solo mode: the lead is the only model called, so a build spends
    # exactly one agent call and the summary stays deterministic.
    svc = GangOf8Service(data_dir=tmp_path, panel=[])
    svc.registry.register(PreambleArtifactLead())

    s = svc.run("Produce index.html with a calendar", source="test")

    assert s.status == SessionStatus.done
    assert s.agent_calls == 1, "a build should not spend a second call on a summarizer"
    assert s.final.confidence == "high"
    assert "Files written:" in s.final.answer and "index.html" in s.final.answer
    assert "Open index.html" in s.final.answer
    # the lead's rationale is preserved, but the raw file body is NOT dumped in
    assert "self-contained file using vanilla JS" in s.final.answer
    assert "<table" not in s.final.answer


class BlankFileLead:
    """Emits an ARTIFACT whose body is only whitespace."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            return AdapterResult(content="ARTIFACT: report.md\n   \n", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_blank_artifact_on_noncode_task_is_not_a_confident_success(tmp_path):
    """Regression: a non-code (content) task that produces an empty/blank file
    must FAIL verification and report low confidence — never high confidence on an
    empty file. (Verification used to run only for code tasks.)"""
    svc = GangOf8Service(data_dir=tmp_path)
    svc.registry.register(BlankFileLead())

    session = svc.run("Write a short report summarizing the storage options.", source="test")

    assert session.status == SessionStatus.failed
    assert session.outcome == "failed_verification"
    assert session.final.confidence == "low", "an empty file must not be high confidence"
    assert "failed artifact verification" in session.final.answer
    # the blank write was rejected, so nothing landed
    assert session.files_changed == []


class TruncatedLead:
    """First emits an index.html that is cut off mid-body; on a continuation
    request it returns only the closing tail."""

    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        if role == Role.lead:
            if "cut off" in prompt.lower():
                return AdapterResult(content="<p>rest</p>\n</body>\n</html>\n", duration_ms=1)
            return AdapterResult(
                content=(
                    "ARTIFACT: index.html\n"
                    "<!doctype html>\n<html><head><title>x</title></head>\n<body>\n<p>start"
                ),
                duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


def test_truncated_artifact_is_continued_to_completion(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    svc.registry.register(TruncatedLead())

    # "Produce ... index.html" → code task, not greenfield (no build/create verb),
    # so it runs straight through without the target-ask gate.
    session = svc.run("Produce index.html showing a calendar", source="test")

    assert session.status == SessionStatus.done
    written = executor.artifacts_dir(tmp_path, session.session_id) / "index.html"
    text = written.read_text(encoding="utf-8")
    assert "<p>start" in text and "rest" in text   # original + continuation
    assert "</html>" in text                        # the file was completed
    # it must NOT have been reported as a failed-verification run
    assert "failed artifact verification" not in (session.final.answer or "")
