"""Show full content of a session's contributions for a role:
python scripts/show_contribution.py <session_id> <role>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conclave_os.service import ConclaveService

s = ConclaveService().get(sys.argv[1])
for c in s["contributions"]:
    if c["role"] == sys.argv[2]:
        print(f"===== round {c['round']} {c['role']} @{c['agent']} ({len(c['content'])} chars) =====")
        print(c["content"])
        print()
