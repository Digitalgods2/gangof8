"""Council web access: web_search (Gemini grounding) + web_fetch (URL → text),
governed as no-approval 'web' skills usable mid-deliberation. SSRF-guarded.
"""

import pytest

from gangof8 import config, web


@pytest.fixture(autouse=True)
def _web_on(monkeypatch):
    monkeypatch.setattr(config, "WEB_ENABLED", True)
from gangof8.executor import ExecutionError, execute
from gangof8.governance import Governance
from gangof8.logstore import LogStore
from gangof8.models import ProposedAction, Role
from gangof8.sessions import SessionManager
from gangof8.skills import get_skill


@pytest.fixture()
def session(tmp_path):
    return SessionManager(LogStore(tmp_path)).create("web task", source="test")


# --- registry metadata --------------------------------------------------------


def test_web_skill_metadata():
    for name, arg in (("web_search", "query"), ("web_fetch", "url")):
        s = get_skill(name)
        assert s.category == "web"
        assert s.requires_approval is False
        assert s.inputs == [arg]
        assert Role.researcher in s.allowed_roles


# --- SSRF guard ---------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "http://localhost/x", "http://127.0.0.1/x", "https://127.0.0.1:8790/secret",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://169.254.1.1/",
    "file:///etc/passwd", "ftp://example.com/x",
])
def test_web_fetch_refuses_internal_and_non_http(bad):
    with pytest.raises(web.WebError):
        web._guard_url(bad)


def test_web_fetch_skill_blocks_localhost(session, tmp_path):
    action = ProposedAction(
        session_id=session.session_id, kind="web_fetch", role=Role.researcher,
        args={"url": "http://127.0.0.1:8790/sessions"},
    )
    with pytest.raises(ExecutionError):
        execute(session, action, tmp_path)


# --- handlers call the web module (network stubbed) ---------------------------


def test_web_search_handler_returns_grounded_answer(session, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "web_search", lambda q: f"ANSWER about {q}\nSources:\n- ex: http://ex")
    action = ProposedAction(
        session_id=session.session_id, kind="web_search", role=Role.researcher,
        args={"query": "latest Go version"},
    )
    out = execute(session, action, tmp_path)
    assert "ANSWER about latest Go version" in out and "Sources:" in out


def test_web_fetch_handler_returns_text(session, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "web_fetch", lambda u: f"text of {u}")
    action = ProposedAction(
        session_id=session.session_id, kind="web_fetch", role=Role.researcher,
        args={"url": "https://example.com"},
    )
    assert execute(session, action, tmp_path) == "text of https://example.com"


def test_html_to_text_strips_tags_and_scripts():
    html = "<html><head><style>x{}</style></head><body><h1>Hi</h1><script>bad()</script><p>Body &amp; more</p></body></html>"
    text = web._html_to_text(html)
    assert "Hi" in text and "Body & more" in text
    assert "bad()" not in text and "<" not in text


# --- governed mid-deliberation (web is allowed alongside read) ----------------


def test_web_overview_proactively_researches_factual_questions(tmp_path, monkeypatch):
    """Internet access being AVAILABLE isn't enough — the coordinator proactively
    web-searches a fact-needing question so the council has real data even if the
    researcher seat fails. Skipped when a local source (established folder) exists."""
    from gangof8 import loop
    from gangof8.models import Classification, Complexity, Risk, TaskType

    monkeypatch.setattr(web, "web_search",
                        lambda q: "Current: M51 is well placed tonight.\nSources:\n- ex: http://e")
    s = SessionManager(LogStore(tmp_path)).create("what galaxies are visible tonight?", source="test")
    s.classification = Classification(
        task_type=TaskType.question, complexity=Complexity.standard, risk=Risk.none, needs_facts=True)
    ov = loop._web_overview(s)
    assert "WEB RESEARCH" in ov and "M51 is well placed" in ov
    # a local source to examine ⇒ no web overview (the file overview applies instead)
    s.established_root = str(tmp_path)
    assert loop._web_overview(s) == ""


def test_web_search_runs_mid_deliberation(session, tmp_path, monkeypatch):
    from gangof8 import loop
    from gangof8.models import Contribution, CouncilMember

    monkeypatch.setattr(web, "web_search", lambda q: "web result for " + q)
    store = LogStore(tmp_path)
    gov = Governance(store)
    member = CouncilMember(role=Role.researcher, agent="mock", active=True)
    contribution = Contribution(round=0, role=Role.researcher, agent="mock",
                                content="Let me check.\nSKILL: web_search latest fastapi release")
    prompts = []

    def call(m, p):
        prompts.append(p)
        return Contribution(round=0, role=m.role, agent="mock", content="informed")

    loop._resolve_skill_requests(session, member, "P", contribution, call, gov, store)
    assert "web result for latest fastapi release" in prompts[0]
    assert any(a.kind == "web_search" and a.status == "executed" for a in session.proposed_actions)
