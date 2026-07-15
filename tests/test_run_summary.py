from __future__ import annotations

from gangof8.models import Contribution, ProposedAction, Role, Session, Task
from gangof8.reporting import run_summary


def test_run_summary_counts_work_and_hashes_written_files(tmp_path):
    output = tmp_path / "answer.txt"
    output.write_text("finished", encoding="utf-8")
    session = Session(
        session_id="s_summary",
        task=Task(task_id="t_summary", session_id="s_summary", text="summarize"),
        agent_calls=2,
        agent_call_attempts=3,
        agent_attempt_duration_ms=900,
        package_started_at="2026-07-15T13:46:26+00:00",
        updated_at="2026-07-15T13:52:26+00:00",
        status="failed",
        package_output_history={
            "src/app.js": [
                {"attempt": 1, "agent": "deepseek", "kind": "primary", "status": "failed"},
                {"attempt": 2, "agent": "codex", "kind": "failover", "status": "completed"},
            ]
        },
        test_fix_attempts=1,
        contributions=[
            Contribution(round=0, role=Role.panelist, agent="codex", model="gpt-test",
                         content="take", duration_ms=125),
            Contribution(round=0, role=Role.lead, agent="claude", model="sonnet",
                         content="decision", duration_ms=375),
        ],
        proposed_actions=[
            ProposedAction(session_id="s_summary", kind="promote", status="executed"),
        ],
        files_changed=[str(output)],
    )

    summary = run_summary(session)

    assert summary["agent_calls"] == 2
    assert summary["agent_call_attempts"] == 3
    assert summary["contribution_duration_ms"] == 500
    assert summary["agent_attempt_duration_ms"] == 900
    assert summary["package_elapsed_ms"] == 360_000
    assert [item["agent"] for item in summary["package_output_history"]["src/app.js"]] == [
        "deepseek", "codex",
    ]
    assert summary["contributions_by_agent"] == {"codex": 1, "claude": 1}
    assert summary["contributions_by_model"] == {"gpt-test": 1, "sonnet": 1}
    assert summary["actions_by_status"] == {"executed": 1}
    assert summary["test_fix_attempts"] == 1
    assert summary["files"][0]["path"] == str(output)
    assert summary["files"][0]["bytes"] == 8
    assert len(summary["files"][0]["sha256"]) == 64
