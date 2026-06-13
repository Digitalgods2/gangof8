"""Direct local-CLI agent adapter (the `cli` backend).

Subprocess is stubbed so no real CLI runs: we verify claude JSON parsing, error
handling, and that the `cli` backend wires CliAdapters into the registry. This
is the path that lets Conclave OS generate real file content itself instead of
descriptions.
"""

import json

import pytest

from conclave_os.adapters import cli as cli_mod
from conclave_os.adapters.cli import CliAdapter
from conclave_os.models import Role
from conclave_os.registry import AgentError
from conclave_os.service import ConclaveService


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture()
def stub_run(monkeypatch):
    calls = {}

    def _set(proc):
        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["input"] = kwargs.get("input")
            return proc
        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
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


def test_unknown_agent_raises():
    with pytest.raises(AgentError, match="unknown CLI agent"):
        CliAdapter("llama").call(Role.researcher, "x", timeout_s=30)


def test_cli_backend_registers_cli_adapters(tmp_path):
    svc = ConclaveService(data_dir=tmp_path, backend="cli")
    assert svc.backend == "cli"
    assert "claude" in svc.registry.names()
    # the registered adapter is a CliAdapter, not the mock one
    assert isinstance(svc.registry._adapters["claude"], CliAdapter)
