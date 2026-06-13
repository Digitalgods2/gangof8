"""Quick session inspector: python inspect_session.py <session_id>"""
import sys

from conclave_os.service import ConclaveService

s = ConclaveService().get(sys.argv[1])
print("agent_calls:", s["agent_calls"], " rounds:", len(s["rounds"]))
print("--- contributions ---")
for c in s["contributions"]:
    print(f"r{c['round']} {c['role']:>11} @{c['agent']:<12} {c['duration_ms'] / 1000:6.1f}s  {len(c['content'])} chars")
print("--- disagreements ---")
for d in s["disagreements"]:
    print("topic:", d["topic"][:90])
    print("  basis:", d["ruling_basis"], "| critic_test:", (d["critic_test"] or "(none)")[:90])
print("--- unresolved ---")
for u in s["unresolved"]:
    print("-", u)
