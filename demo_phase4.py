"""Phase 4 demo: implementer proposes an artifact; nothing is written until
the human approves; the file lands in the session's sandbox. Mock backend."""

from conclave_os.adapters.mock import MockAdapter
from conclave_os.models import Role
from conclave_os.registry import AdapterResult
from conclave_os.service import ConclaveService

DRAFT = (
    "ARTIFACT: recommendation.md\n"
    "# Session Log Storage Recommendation\n\n"
    "Use SQLite as the primary store, with a per-session JSONL mirror.\n"
)


class Demo(MockAdapter):
    def call(self, role, prompt, timeout_s):
        if role == Role.implementer:
            return AdapterResult(content=DRAFT, duration_ms=1)
        return super().call(role, prompt, timeout_s)


svc = ConclaveService()
svc.registry.register(Demo())

session = svc.run("Write a short report recommending SQLite or plain JSON for session logs.")
print(f"1) session {session.session_id} paused: {session.status.value}")
approval = session.approvals[0]
print(f"2) approval requested: {approval.action}")
print(f"   file exists yet? {bool(session.files_changed)}")

done = svc.approve(session.session_id, approval.approval_id, approved=True)
print(f"3) after human approval: {done.status.value}")
print(f"4) artifact written: {done.files_changed[0]}")
print("5) content:")
print(open(done.files_changed[0], encoding="utf-8").read())
