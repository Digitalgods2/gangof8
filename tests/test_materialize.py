"""Per-file artifact materialization.

An agent sometimes describes multi-file output in one draft ("I'll deliver
main.py, README.md, requirements.txt") instead of emitting it. When an
output task yields no full ARTIFACT blocks, the
coordinator fetches each intended file with its own focused single-file call and
proposes one approval-gated write_file per file.
"""

import re

import pytest

from gangof8 import executor, loop
from gangof8.adapters.mock import MockAdapter
from gangof8.models import Role, SessionStatus
from gangof8.registry import AdapterResult
from gangof8.service import GangOf8Service

# real-agent-style: the draft only NAMES the files; bodies arrive one at a time.
DRAFT_DESCRIPTION = (
    "I will deliver a minimal app: main.py with the entrypoint, README.md with "
    "instructions, and requirements.txt with dependencies. All ready for write-through."
)
BODIES = {
    "main.py": "```python\nprint('hello world')\n```",  # fenced → must be stripped
    "README.md": "# Hello App\nRun: python main.py",
    "requirements.txt": "fastapi\nuvicorn",
}
_FN_IN_PROMPT = re.compile(r"contents of the file '([^']+)'")


class MaterializeAdapter:
    name = "mock"

    def __init__(self):
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s):
        if role == Role.lead:
            m = _FN_IN_PROMPT.search(prompt)
            if m:  # focused single-file materialization call
                return AdapterResult(content=BODIES.get(m.group(1), "x"), duration_ms=1)
            return AdapterResult(content=DRAFT_DESCRIPTION, duration_ms=1)  # the draft
        if role == Role.critic:
            return AdapterResult(content="acceptable", duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def service(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    svc.registry.register(MaterializeAdapter())
    return svc


# --- unit helpers -------------------------------------------------------------


def test_strip_code_fence():
    assert loop._strip_code_fence("```python\nprint(1)\n```") == "print(1)"
    assert loop._strip_code_fence("```\nplain\n```") == "plain"
    assert loop._strip_code_fence("no fence here") == "no fence here"


def test_intended_filenames_from_task(tmp_path):
    from gangof8.logstore import LogStore
    from gangof8.sessions import SessionManager

    session = SessionManager(LogStore(tmp_path)).create(
        "Build an app with main.py, README.md, and requirements.txt", source="test"
    )
    names = loop._intended_filenames(session)
    assert names == ["main.py", "README.md", "requirements.txt"]


def test_intended_filenames_falls_back_to_established_revision_target(tmp_path):
    """A revision follow-up ('slow the ghosts down') names no file, and a
    flubbed lead draft may name none either — the established file the panel
    discussed by name (two+ mentions) is the intended target; a one-off
    mention of an unrelated established file is not."""
    from gangof8.logstore import LogStore
    from gangof8.models import Contribution
    from gangof8.sessions import SessionManager

    est = tmp_path / "est"
    est.mkdir()
    (est / "index.html").write_text("<html></html>", encoding="utf-8")
    (est / "output.txt").write_text("stray", encoding="utf-8")
    session = SessionManager(LogStore(tmp_path)).create(
        "computer characters move too fast, slow the game down", source="test")
    session.established_root = str(est)
    session.contributions.extend([
        Contribution(round=0, role=Role.panelist, agent="a",
                     content="Change the ghost tick timing in index.html."),
        Contribution(round=0, role=Role.panelist, agent="b",
                     content="index.html needs a speed-select screen; the last "
                             "run mistakenly shipped output.txt."),
        Contribution(round=0, role=Role.lead, agent="c",
                     content="SKILL: search_project ghost speed"),
    ])
    assert loop._intended_filenames(session) == ["index.html"]


# --- end to end ---------------------------------------------------------------


def test_materializes_each_described_file(service):
    session = service.run(
        "Produce a tiny app with main.py, README.md, and requirements.txt.", source="test"
    )
    # materialized write_files are FREE now — they execute and the session completes
    assert session.status == SessionStatus.done
    proposed = {a.filename: a for a in session.proposed_actions}
    assert set(proposed) == {"main.py", "README.md", "requirements.txt"}
    # the fenced body was stripped to the raw file content
    assert proposed["main.py"].content == "print('hello world')"
    assert "Hello App" in proposed["README.md"].content
    assert all(a.status == "executed" for a in session.proposed_actions)
    assert not [a for a in session.approvals if a.status == "pending"]


def test_materialized_files_written_after_approval(service, tmp_path):
    session = service.run(
        "Produce a tiny app with main.py, README.md, and requirements.txt.", source="test"
    )
    sid = session.session_id
    done = session
    for approval in [a for a in session.approvals if a.status == "pending"]:
        done = service.approve(sid, approval.approval_id, approved=True)
    assert done.status == SessionStatus.done
    sandbox = executor.artifacts_dir(tmp_path, sid)
    assert {p.name for p in sandbox.iterdir()} == {"main.py", "README.md", "requirements.txt"}
    assert (sandbox / "main.py").read_text(encoding="utf-8") == "print('hello world')"
    assert len(done.files_changed) == 3


def test_no_materialization_when_artifacts_emitted(tmp_path):
    """If the implementer DOES emit full ARTIFACT blocks, those are used as-is
    and no per-file materialization happens."""
    class DirectAdapter:
        name = "mock"

        def __init__(self):
            self._inner = MockAdapter()

        def call(self, role, prompt, timeout_s):
            if role == Role.lead:
                return AdapterResult(content="ARTIFACT: only.py\nprint('direct')\n", duration_ms=1)
            if role == Role.critic:
                return AdapterResult(content="acceptable", duration_ms=1)
            return self._inner.call(role, prompt, timeout_s)

    service = GangOf8Service(data_dir=tmp_path)
    service.registry.register(DirectAdapter())
    session = service.run("Produce an app: only.py", source="test")
    assert {a.filename for a in session.proposed_actions} == {"only.py"}
    assert session.proposed_actions[0].content == "print('direct')"
