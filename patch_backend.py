"""One-off: backfill the backend field on a pre-Phase-4 session.
python patch_backend.py <session_id> <backend>"""
import json
import sqlite3
import sys

sid, backend = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("data/conclave_os.db")
row = conn.execute("SELECT json FROM sessions WHERE session_id=?", (sid,)).fetchone()
data = json.loads(row[0])
data["backend"] = backend
conn.execute("UPDATE sessions SET json=? WHERE session_id=?", (json.dumps(data), sid))
conn.commit()
print(f"patched {sid}: backend -> {backend}")
