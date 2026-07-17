#!/bin/bash
# Gang of 8 launcher (macOS).
# Double-click in Finder: starts the dashboard server in this Terminal window
# and opens the dashboard in your browser. Close this window (or Ctrl-C) to
# stop the server.

cd "$(dirname "${BASH_SOURCE[0]}")"

# Already running? Constructing a second server process would run this
# app's crash-recovery step against the same data, which can park an
# in-flight goal even though nothing actually crashed. Just open the
# existing dashboard instead of starting a duplicate.
if lsof -ti tcp:8790 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Gang of 8 is already running — opening the existing dashboard."
    open "http://127.0.0.1:8790/"
    sleep 2
    exit 0
fi

# ---- backend: "cli" = real local agents (costs tokens) | "mock" = free/offline
BACKEND="cli"

# ---- use the project venv python if present, else system python3
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="python3"
fi

echo "Starting Gang of 8 dashboard (backend: $BACKEND) ..."
echo "Only one instance can use port 8790 at a time."
echo

"$PY" cli.py serve --backend "$BACKEND" &
SERVER_PID=$!

# give the server a moment to come up, then open the dashboard
sleep 3
open "http://127.0.0.1:8790/"

echo "Dashboard: http://127.0.0.1:8790/"
echo "(To run free/offline, change BACKEND to \"mock\" at the top of this file.)"
echo "Press Ctrl-C or close this window to stop the server."

wait "$SERVER_PID"
