"""Phase 5: service mode + dashboard.

Background submission returns immediately and the session completes on a
worker thread; the dashboard page is served at /; approvals and answers can
resume sessions in the background.
"""

import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from gangof8.service import GangOf8Service
from gangof8.settings import SETTINGS_SCHEMA_VERSION

TASK = (
    "Compare SQLite vs. plain JSON files for storing session logs in a local "
    "service, and recommend one."
)
# a task that pauses: the lead promotes into the established folder the task
# references, hitting the ONE approval gate (the pre-run risk gate is gone)
PROMOTING = "Write a short report recommending SQLite, delivered into {est}"

PROMOTE_DRAFT = (
    "ARTIFACT: report.md\n"
    "# Storage Recommendation\n\nUse SQLite.\n"
    "PROMOTE: report.md\n"
)


class _PromotingLead:
    name = "mock"

    def __init__(self):
        from gangof8.adapters.mock import MockAdapter
        self._inner = MockAdapter()

    def call(self, role, prompt, timeout_s, images=None):
        from gangof8.models import Role
        from gangof8.registry import AdapterResult
        # promote only for the delivery-flavored task; plain tasks stay mock
        if role == Role.lead and "delivered into" in prompt:
            return AdapterResult(content=PROMOTE_DRAFT, duration_ms=1)
        return self._inner.call(role, prompt, timeout_s)


@pytest.fixture()
def est(tmp_path):
    d = tmp_path / "established"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path, est):
    from gangof8 import main as main_mod

    main_mod.service = GangOf8Service(data_dir=tmp_path / "data")
    main_mod.service.registry.register(_PromotingLead())
    return TestClient(main_mod.app)


def _poll_until(client, session_id, statuses, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = client.get(f"/sessions/{session_id}").json()
        if data["status"] in statuses:
            return data
        time.sleep(0.1)
    raise AssertionError(f"session {session_id} never reached {statuses}")


def test_dashboard_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Gang of 8" in r.text
    # the header tagline: the human-approval guarantee stays on screen
    assert "nothing executes without your approval" in r.text


def test_header_lockup_is_served_and_referenced(client):
    """The supplied single-image lockup replaces the header emblem and labels."""
    r = client.get("/gangof8-text.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n" and len(r.content) > 500  # a real PNG
    page = client.get("/").text
    assert '<img src="/gangof8-text.png" class="brand-lockup"' in page
    assert "Coordinator OS" not in page
    assert 'rel="icon"' in page  # the existing favicon is independent of the header lockup


def test_header_has_immediate_ai_brand_seat_toggles(client):
    page = client.get("/").text
    assert '<div class="header-top">' in page
    assert 'id="aiSeatToggles"' in page
    assert page.index('class="header-top"') < page.index('id="aiSeatToggles"')
    for brand in ("OpenAI", "Anthropic", "Gemini", "DeepSeek", "GLM", "Qwen", "Kimi"):
        assert f">{brand}</label>" in page
    assert page.count('data-settings-map="cli_enabled"') == 3
    assert page.count('data-settings-map="openrouter_enabled"') == 4

    app_js = client.get("/app.js").text
    assert 'fetch("/settings", {cache: "no-store"})' in app_js
    assert 'method: "PUT"' in app_js
    assert "seatSettingsPatch(latest, mapName, seat, desired)" in app_js
    assert "renderHeaderSeatToggles(settingsCache)" in app_js
    assert "Could not update ${brand}" in app_js

    css = client.get("/app.css").text
    assert ".header-top" in css
    assert "justify-content:center" in css


def test_header_seat_patch_preserves_every_other_map_value(client):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    script = r"""
const {seatSettingsPatch} = require('./gangof8/static/dashboard-utils.js');
const settings = {
  cli_enabled: {claude:true, codex:true, gemini:false},
  openrouter_enabled: {deepseek:true, glm:false, qwen:true, kimi:false},
};
const cliPatch = seatSettingsPatch(settings, 'cli_enabled', 'codex', false);
if (JSON.stringify(cliPatch.cli_enabled) !== JSON.stringify({claude:true,codex:false,gemini:false}))
  throw new Error('CLI siblings were lost');
const orPatch = seatSettingsPatch(settings, 'openrouter_enabled', 'glm', true);
if (JSON.stringify(orPatch.openrouter_enabled) !== JSON.stringify({deepseek:true,glm:true,qwen:true,kimi:false}))
  throw new Error('OpenRouter siblings were lost');
if (!settings.cli_enabled.codex || settings.openrouter_enabled.glm)
  throw new Error('source settings were mutated');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=os.getcwd(), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_dashboard_assets_served(client):
    page = client.get("/").text
    assert 'src="/dashboard-utils.js"' in page
    assert 'href="/app.css"' in page
    assert 'src="/app.js"' in page
    assert 'id="enhanceBtn" type="button"' in page
    assert 'onclick="enhancePrompt()"' not in page
    assert client.get("/dashboard-utils.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/app.css").headers["content-type"].startswith("text/css")
    app_js = client.get("/app.js")
    assert app_js.headers["content-type"].startswith("text/javascript")
    assert "no-store" in app_js.headers["cache-control"]
    assert "function enhancePrompt" in app_js.text
    assert "void enhancePrompt();" in app_js.text
    assert "const escAttr =" not in app_js.text, "shared helper must not be redeclared"
    assert "this package has one accountable owner" in app_js.text
    assert "contract-linked to P" in app_js.text
    assert "Building package" in app_js.text
    assert "Export saved profile" in app_js.text
    assert "Import profile" in app_js.text
    assert "Load packaged defaults" in app_js.text
    assert 'fetch("/settings/profile"' in app_js.text
    assert 'fetch("/settings/profile/default"' in app_js.text
    assert "activePollInterval()" in app_js.text
    assert "uiPreferences.collapse_finished" in app_js.text
    assert 'replace(/^\\uFEFF/, "")' in app_js.text
    assert app_js.text.index("const runSummary = s.run_summary || {}") < app_js.text.index(
        "const callAttempts = s.agent_call_attempts"
    )
    assert "package elapsed" in app_js.text
    assert "aggregate attempt time" in app_js.text


def test_dashboard_detail_refresh_gate_rejects_stale_responses(client):
    """A pre-cancel detail request cannot repaint over a newer terminal one."""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    script = r"""
const {createLatestRequestGate} = require('./gangof8/static/dashboard-utils.js');
const gate = createLatestRequestGate();
const beforeCancel = gate.begin();
const terminalRefresh = gate.begin();
if (gate.isCurrent(beforeCancel)) throw new Error('older response still owns rendering');
if (!gate.isCurrent(terminalRefresh)) throw new Error('newest response lost ownership');
gate.invalidate();
if (gate.isCurrent(terminalRefresh)) throw new Error('cancel did not invalidate in-flight response');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=os.getcwd(), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    app_js = client.get("/app.js").text
    assert "detailRefreshGate.isCurrent(requestToken)" in app_js
    assert 'cache: "no-store"' in app_js


def test_dashboard_tracks_historical_and_current_goal_attempts(client):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    script = r"""
const {
  goalSessionIds, goalRenderSignature, packageAttemptState,
} = require('./gangof8/static/dashboard-utils.js');
const attempts = [
  {number:1, session_id:'s_old', status:'failed', created_at:'2026-01-01T00:00:00Z'},
  {number:2, session_id:'s_new', status:'deliberating', created_at:'2026-01-01T00:01:00Z', is_current:true},
];
const goal = {goal_id:'g_1', status:'running', actionable_session_id:'s_new',
  milestones:[{package_id:'wp_1', status:'running', session_id:'s_new', attempts}]};
const old = {session_id:'s_old', status:'failed', goal_id:'g_1', work_package_id:'wp_1'};
const ids = goalSessionIds(goal, [old]);
if (!ids.includes('s_old') || !ids.includes('s_new')) throw new Error('attempt history lost');
const state = packageAttemptState(old, goal, [old]);
if (!state.isHistorical || state.selectedNumber !== 1 || state.currentNumber !== 2)
  throw new Error('historical/current attempt identity is wrong');
const before = goalRenderSignature(goal);
goal.milestones[0].status = 'done';
if (before === goalRenderSignature(goal)) throw new Error('parent transition did not invalidate detail');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=os.getcwd(), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    app_js = client.get("/app.js").text
    assert "Retry attempt" in app_js
    assert "Package briefs are captured when an attempt starts" in app_js
    assert "goalRenderSignature(parentGoal)" in app_js


def test_health_reports_backend(client):
    h = client.get("/health").json()
    assert h["ok"] is True
    assert h["backend"] == "mock"


def test_diagnostics_reports_runtime_without_raw_secrets(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-1234")
    d = client.get("/diagnostics").json()
    assert d["backend"] == "mock"
    assert d["settings_version"] == SETTINGS_SCHEMA_VERSION
    assert "data_dir" in d and "sandbox_root" in d
    assert set(d["cli"]) == {"claude", "codex", "gemini"}
    assert d["api_keys"]["openrouter"]["present"] is True
    assert "sk-or-secret-1234" not in str(d)


def test_delete_session_removes_it(client):
    sid = client.post("/tasks", json={"text": "What is 2+2?", "source": "test"}).json()["session_id"]
    assert any(s["session_id"] == sid for s in client.get("/sessions").json())
    r = client.delete(f"/sessions/{sid}")
    assert r.status_code == 200 and r.json()["deleted"] == sid
    assert not any(s["session_id"] == sid for s in client.get("/sessions").json())
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_delete_missing_session_404(client):
    assert client.delete("/sessions/s_nope").status_code == 404


def test_open_file_rejects_unknown_session(client):
    r = client.post("/files/open", json={"session_id": "s_nope", "path": "whatever.txt"})
    assert r.status_code == 404


def test_open_file_rejects_path_not_in_session_outputs(client, tmp_path):
    # a real file the session did NOT write must never be openable via this endpoint
    sid = client.post("/tasks", json={"text": "What is 2+2?", "source": "test"}).json()["session_id"]
    foreign = tmp_path / "not_ours.txt"
    foreign.write_text("secret", encoding="utf-8")
    r = client.post("/files/open", json={"session_id": sid, "path": str(foreign)})
    assert r.status_code == 403


def test_enhance_amplifies_prompt_and_saves_a_copy(client):
    r = client.post("/enhance", json={"text": "write a haiku about rain"})
    assert r.status_code == 200
    d = r.json()
    assert d["enhanced"], "the lead model produced an enhanced prompt"
    assert d["original"] == "write a haiku about rain"
    assert d["saved"] and os.path.isfile(d["saved"]), "a copy was saved to disk"


def test_enhance_rejects_empty_prompt(client):
    assert client.post("/enhance", json={"text": "   "}).status_code == 422


def test_background_submit_completes(client):
    created = client.post("/tasks", json={"text": TASK, "background": True}).json()
    assert created["status"] == "received", "background submit must return immediately"
    data = _poll_until(client, created["session_id"], {"done"})
    assert data["final"]["answer"]


def test_background_approval_resumes(client, est):
    created = client.post(
        "/tasks", json={"text": PROMOTING.format(est=est), "background": True}
    ).json()
    data = _poll_until(client, created["session_id"], {"awaiting_approval"})
    aid = next(a["approval_id"] for a in data["approvals"] if a["status"] == "pending")
    resolved = client.post(
        f"/sessions/{created['session_id']}/approvals/{aid}",
        json={"approved": True, "background": True},
    ).json()
    assert resolved["status"] in ("awaiting_approval", "deliberating", "composing", "done")
    data = _poll_until(client, created["session_id"], {"done"})
    assert data["final"]["answer"]


def test_sessions_list_is_enriched(client, est):
    created = client.post(
        "/tasks", json={"text": PROMOTING.format(est=est), "background": True}
    ).json()
    _poll_until(client, created["session_id"], {"awaiting_approval"})
    listed = client.get("/sessions").json()
    entry = next(s for s in listed if s["session_id"] == created["session_id"])
    assert entry["task_text"].startswith("Write a short report")
    assert entry["pending_approvals"] == 1
    assert entry["pending_inputs"] == 0


def test_sync_submit_still_works(client):
    created = client.post("/tasks", json={"text": TASK}).json()
    assert created["status"] == "done", "default (sync) behavior is unchanged"
