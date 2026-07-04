"""Panel-artifact salvage: when the lead fails to produce a file (e.g. the claude
CLI times out authoring a big single-file game), recover the complete file a
panelist pasted inline instead of shipping nothing."""

from conclave_os import loop
from conclave_os.logstore import LogStore
from conclave_os.models import Contribution, Role, Session, SessionStatus, Task
from conclave_os.registry import AdapterResult, AgentError
from conclave_os.service import ConclaveService


def _session(task_text: str) -> Session:
    return Session(session_id="s_x",
                   task=Task(task_id="t", session_id="s_x", text=task_text))


def _html(n: int = 600) -> str:
    return "<!DOCTYPE html>\n<html><head><title>G</title></head><body>" \
        + "<canvas></canvas>" * n + "</body></html>"


# --- extraction ----------------------------------------------------------------


def test_best_panel_artifact_extracts_fenced_file():
    s = _session("write a single-file pacman game, save as pacman.html")
    s.contributions.append(Contribution(
        round=0, role=Role.panelist, agent="glm",
        content="Here is the game:\n```html\n" + _html() + "\n```\nOpen it to play."))
    art = loop._best_panel_artifact(s)
    assert art is not None
    agent, fn, body = art
    assert agent == "glm"
    assert fn == "pacman.html"
    assert body.startswith("<!DOCTYPE html>") and body.rstrip().endswith("</html>")
    # trailing prose after the fence is not in the file body
    assert "Open it to play" not in body


def test_best_panel_artifact_picks_largest_and_ignores_lead():
    s = _session("build game.html")
    s.contributions.append(Contribution(round=0, role=Role.panelist, agent="glm",
                                        content="```html\n" + _html(50) + "\n```"))
    s.contributions.append(Contribution(round=0, role=Role.panelist, agent="qwen",
                                        content="```html\n" + _html(900) + "\n```"))
    # a lead contribution is never salvaged from (only panel seats)
    s.contributions.append(Contribution(round=0, role=Role.lead, agent="claude",
                                        content="```html\n" + _html(2000) + "\n```"))
    agent, fn, body = loop._best_panel_artifact(s)
    assert agent == "qwen"          # larger of the two PANEL artifacts
    assert fn == "game.html"


def test_best_panel_artifact_none_when_no_real_file():
    s = _session("answer a question")
    s.contributions.append(Contribution(round=0, role=Role.panelist, agent="glm",
                                        content="I think the answer is 42. ```py\nx=1\n```"))
    assert loop._best_panel_artifact(s) is None   # tiny snippet, not a deliverable


def test_salvage_rejects_patch_snippets_on_a_revision(tmp_path):
    """A revision where panelists give only patches (no complete file) must
    salvage NOTHING — never ship a fragment as output.txt (the regression)."""
    est = tmp_path / "out"
    est.mkdir()
    (est / "index.html").write_text(
        "<!DOCTYPE html><html><body>old game</body></html>", encoding="utf-8")
    s = _session("make the ghosts move slower, add a speed selector")
    s.established_root = str(est)
    # a big advice post, but only code SNIPPETS inside — not a whole file
    s.contributions.append(Contribution(round=0, role=Role.panelist, agent="qwen",
        content="Great idea. Near the top add:\n```js\nconst SPEED={slow:1,med:2,fast:3};\n```\n"
                "Then in the loop gate movement:\n```js\nif (t-last>SPEED[mode]) move();\n```\n"
                "This keeps rendering smooth. " + "More detail. " * 200))
    assert loop._best_panel_artifact(s) is None
    store = LogStore(tmp_path / "data")
    s2 = _session("make it slower")
    s2.established_root = str(est)
    s2.contributions.append(Contribution(round=0, role=Role.panelist, agent="qwen",
        content="```js\nconst x=1;\n```"))
    loop._salvage_from_panel(s2, store)
    assert not s2.proposed_actions   # nothing salvaged, no output.txt


def test_salvage_creates_write_and_promote(tmp_path):
    store = LogStore(tmp_path)
    s = _session("build a single file game.html")
    s.contributions.append(Contribution(round=0, role=Role.panelist, agent="qwen",
                                        content="```html\n" + _html() + "\n```"))
    loop._salvage_from_panel(s, store)
    kinds = [a.kind for a in s.proposed_actions]
    assert kinds.count("write_file") == 1 and kinds.count("promote") == 1
    wf = next(a for a in s.proposed_actions if a.kind == "write_file")
    assert wf.filename == "game.html" and wf.content.startswith("<!DOCTYPE html>")


# --- end-to-end: a failing lead still delivers -------------------------------


def test_run_salvages_a_delivered_file_when_lead_fails(tmp_path):
    """The lead times out, but a panelist wrote the whole file — the run must end
    with that file staged for delivery, not as a bare failure."""
    est = tmp_path / "out"
    est.mkdir()
    html = _html()

    class LeadTimesOut:
        name = "mock"  # the lead + talent seats in mock backend

        def call(self, role, prompt, timeout_s, images=None):
            if role == Role.lead:
                raise AgentError("claude CLI timed out after 600s")
            return AdapterResult(content="a brief panel take", duration_ms=1)

    class PanelWithFile:
        name = "glm"

        def call(self, role, prompt, timeout_s, images=None):
            return AdapterResult(
                content="Here is the complete game:\n```html\n" + html
                        + "\n```\nSave as game.html and open it.",
                duration_ms=1)

    class _Svc(ConclaveService):
        def _open(self, *a, **k):
            sess = super()._open(*a, **k)
            sess.established_root = str(est)
            self.store.save_session(sess)
            return sess

    svc = _Svc(data_dir=tmp_path / "data", panel=["glm"])
    svc.registry.register(LeadTimesOut())
    svc.registry.register(PanelWithFile())

    session = svc.run("write a single-file game.html", source="test")

    events = [__import__("json").loads(line)["event"]
              for line in svc.store.session_log_path(session.session_id)
              .read_text(encoding="utf-8").splitlines()]
    assert "panel_artifact_salvaged" in events
    # the salvaged file is staged and waiting on the promote approval gate
    assert session.status == SessionStatus.awaiting_approval
    promo = [a for a in session.proposed_actions if a.kind == "promote"]
    assert promo and promo[0].filename == "game.html"
