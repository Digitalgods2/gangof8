"""Council web access: web_search (Gemini grounding) + web_fetch (URL → text),
governed as no-approval 'web' skills usable mid-deliberation. SSRF-guarded.
"""

import pytest

from conclave_os import web
from conclave_os.executor import ExecutionError, execute
from conclave_os.governance import Governance
from conclave_os.logstore import LogStore
from conclave_os.models import ProposedAction, Role
from conclave_os.sessions import SessionManager
from conclave_os.skills import get_skill


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


def test_web_search_runs_mid_deliberation(session, tmp_path, monkeypatch):
    from conclave_os import loop
    from conclave_os.models import Contribution, CouncilMember

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
