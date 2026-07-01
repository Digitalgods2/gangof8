"""Direct local-CLI agent adapter (the `cli` backend).

Subprocess is stubbed so no real CLI runs: we verify claude JSON parsing, error
handling, and that the `cli` backend wires CliAdapters into the registry. This
is the path that lets Conclave OS generate real file content itself instead of
descriptions.
"""

import json
from pathlib import Path

import pytest

from conclave_os.adapters import cli as cli_mod
from conclave_os.adapters.cli import CliAdapter
from conclave_os.models import Role
from conclave_os.registry import AgentError
from conclave_os.service import ConclaveService


class _Proc:
    """Fake subprocess.Popen — _exec now uses Popen().communicate()."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self._stdout, self._stderr = returncode, stdout, stderr
        self._calls = None

    def communicate(self, input=None, timeout=None):
        if self._calls is not None:
            self._calls["input"] = input
        return self._stdout, self._stderr

    def kill(self):
        if self._calls is not None:
            self._calls["killed"] = True


@pytest.fixture()
def stub_run(monkeypatch):
    calls = {}

    def _set(proc):
        proc._calls = calls

        def fake_popen(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["cwd"] = kwargs.get("cwd")
            return proc
        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        # resolve any CLI name to a fake path so tests don't need a real install
        monkeypatch.setattr(cli_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        return calls

    return _set


def test_claude_returns_result_field(stub_run):
    calls = stub_run(_Proc(stdout=json.dumps({"subtype": "success", "is_error": False,
                                              "result": "from fastapi import FastAPI\n"})))
    out = CliAdapter("claude").call(Role.implementer, "make main.py", timeout_s=60)
    assert out.content == "from fastapi import FastAPI"
    assert calls["cmd"][0].endswith("claude")  # resolved via PATH
    assert calls["cmd"][1] == "-p"
    assert "--tools" in calls["cmd"]  # tools disabled — no side effects
    assert calls["input"] == "make main.py"  # prompt goes on stdin


def test_cli_subprocess_runs_from_a_neutral_cwd(stub_run):
    """The agent CLI must never inherit the server's cwd (the repo): a model
    with latent tool instincts would perceive — and could ungovernedly read —
    whatever folder the server runs in. It runs from an empty sandbox dir."""
    from conclave_os import config

    calls = stub_run(_Proc(stdout=json.dumps({"is_error": False, "result": "ok"})))
    CliAdapter("claude").call(Role.lead, "hello", timeout_s=60)
    cwd = Path(calls["cwd"])
    assert cwd == config.SANDBOX_ROOT / "cli-neutral"
    assert cwd.is_dir()
    assert Path.cwd() != cwd, "must not be the server's own cwd"


def test_claude_is_error_raises(stub_run):
    stub_run(_Proc(stdout=json.dumps({"is_error": True, "result": "rate limited"})))
    with pytest.raises(AgentError, match="claude CLI error"):
        CliAdapter("claude").call(Role.researcher, "x", timeout_s=30)


def test_claude_empty_result_raises(stub_run):
    stub_run(_Proc(stdout=json.dumps({"is_error": False, "result": ""})))
    with pytest.raises(AgentError, match="empty output"):
        CliAdapter("claude").call(Role.researcher, "x", timeout_s=30)


def test_nonzero_exit_raises(stub_run):
    stub_run(_Proc(returncode=1, stdout="", stderr="boom"))
    with pytest.raises(AgentError, match="exited 1"):
        CliAdapter("claude").call(Role.researcher, "x", timeout_s=30)


def test_non_json_raises(stub_run):
    stub_run(_Proc(stdout="not json"))
    with pytest.raises(AgentError, match="non-JSON"):
        CliAdapter("claude").call(Role.researcher, "x", timeout_s=30)


def test_claude_vision_sends_image_content_block(stub_run, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    stream_out = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "VISIONTEST"}]}}),
        json.dumps({"type": "result", "is_error": False, "result": "VISIONTEST"}),
    ])
    calls = stub_run(_Proc(stdout=stream_out))
    out = CliAdapter("claude").call(
        Role.researcher, "read the text", timeout_s=60,
        images=[{"path": str(img), "media_type": "image/png"}],
    )
    assert out.content == "VISIONTEST"
    # vision uses the stream-json input/output path
    assert "stream-json" in calls["cmd"]
    assert "--input-format" in calls["cmd"]
    # the image is sent as a base64 content block in the message on stdin
    msg = json.loads(calls["input"])
    blocks = msg["message"]["content"]
    assert any(b.get("type") == "image" for b in blocks)
    assert any(b.get("type") == "text" for b in blocks)


def test_codex_vision_attaches_image_flag(monkeypatch, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG fake")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        # codex writes its final message to the --output-last-message path
        outfile = cmd[cmd.index("--output-last-message") + 1]
        Path(outfile).write_text("CODE-READ", encoding="utf-8")
        return _Proc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    out = CliAdapter("codex").call(
        Role.critic, "read it", timeout_s=60,
        images=[{"path": str(img), "media_type": "image/png"}],
    )
    assert out.content == "CODE-READ"
    assert any(a == f"--image={img}" for a in captured["cmd"])


def test_claude_without_images_uses_plain_json(stub_run):
    calls = stub_run(_Proc(stdout=json.dumps({"is_error": False, "result": "hi"})))
    CliAdapter("claude").call(Role.researcher, "x", timeout_s=30)  # no images
    assert "stream-json" not in calls["cmd"]


def test_gemini_vision_uses_genai_sdk(monkeypatch, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG fake")
    monkeypatch.setenv("GEMINI_API_KEY", "testkey")
    from google import genai as genai_mod

    captured = {}

    class FakeResp:
        text = "SDK-VISION"

    class FakeModels:
        def generate_content(self, model, contents):
            captured["model"] = model
            captured["contents"] = contents
            return FakeResp()

    class FakeClient:
        def __init__(self, *a, **k):
            self.models = FakeModels()

    monkeypatch.setattr(genai_mod, "Client", FakeClient)
    out = CliAdapter("gemini").call(
        Role.researcher, "read it", timeout_s=60,
        images=[{"path": str(img), "media_type": "image/png"}],
    )
    assert out.content == "SDK-VISION"
    assert captured["model"] == "gemini-2.5-flash"
    assert len(captured["contents"]) == 2          # one image Part + the prompt
    assert captured["contents"][-1] == "read it"   # prompt is the last content


def test_gemini_text_without_key_falls_back_to_cli(stub_run, monkeypatch):
    # no API key → gemini text uses the CLI
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    calls = stub_run(_Proc(stdout="cli answer"))
    out = CliAdapter("gemini").call(Role.researcher, "x", timeout_s=30)  # no images
    assert out.content == "cli answer"
    assert calls["cmd"][0].endswith("gemini")
    assert "-p" in calls["cmd"]


def test_gemini_text_uses_sdk_when_key_present(monkeypatch):
    # with a key, gemini text goes through the SDK (NOT the flaky CLI) — no
    # subprocess, no command-line-length limit, no headless hang.
    monkeypatch.setenv("GEMINI_API_KEY", "testkey")
    from google import genai as genai_mod

    captured = {}

    class FakeResp:
        text = "SDK-TEXT"

    class FakeModels:
        def generate_content(self, model, contents):
            captured["contents"] = contents
            return FakeResp()

    class FakeClient:
        def __init__(self, *a, **k):
            self.models = FakeModels()

    monkeypatch.setattr(genai_mod, "Client", FakeClient)
    # Popen must NOT be called — if it is, the test fails loudly
    monkeypatch.setattr(cli_mod.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CLI used despite key")))
    out = CliAdapter("gemini").call(Role.researcher, "summarize this", timeout_s=30)
    assert out.content == "SDK-TEXT"
    assert captured["contents"] == ["summarize this"]  # text-only: just the prompt


def test_unknown_agent_raises():
    with pytest.raises(AgentError, match="unknown CLI agent"):
        CliAdapter("llama").call(Role.researcher, "x", timeout_s=30)


def test_killed_subprocess_surfaces_as_cancellation(stub_run):
    """When a cancel kills the CLI process, _exec raises SessionCancelled (not a
    generic AgentError), so the loop treats it as a cancel, not a seat failure."""
    from conclave_os import cancellation
    from conclave_os.cancellation import SessionCancelled

    cancellation.set_current_session("s_test")
    cancellation.request("s_test")  # simulate the cancel having fired
    try:
        # process returns nonzero (as a killed process would)
        stub_run(_Proc(returncode=1, stdout="", stderr=""))
        with pytest.raises(SessionCancelled):
            CliAdapter("claude").call(Role.researcher, "x", timeout_s=30)
    finally:
        cancellation.clear("s_test")
        cancellation.set_current_session(None)


def test_cli_backend_registers_cli_adapters(tmp_path):
    svc = ConclaveService(data_dir=tmp_path, backend="cli")
    assert svc.backend == "cli"
    assert "claude" in svc.registry.names()
    # the registered adapter is a CliAdapter, not the mock one
    assert isinstance(svc.registry._adapters["claude"], CliAdapter)
