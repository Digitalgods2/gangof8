#!/bin/bash
# Gang of 8 — windowless launcher (macOS).
# Double-click in Finder: starts the dashboard server detached in the
# background (no Terminal window left open) and opens the dashboard in your
# browser. Use "Stop Gang of 8.command" to stop it, since there's no window
# to close.

cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$(pwd)"

# Stop whatever is already listening on the dashboard port before launching.
PIDS=$(lsof -ti tcp:8790 -sTCP:LISTEN 2>/dev/null)
if [ -n "$PIDS" ]; then
    kill -9 $PIDS 2>/dev/null
fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="python3"
fi

# ---- backend: "cli" = real local agents (costs tokens) | "mock" = free/offline
BACKEND="cli"

LOG="$ROOT/gangof8-server.log"
PIDFILE="$ROOT/.gangof8-server.pid"

# start the server detached, redirecting output to a log file
nohup "$PY" cli.py serve --backend "$BACKEND" >"$LOG" 2>&1 &
disown
echo $! >"$PIDFILE"

# give it a moment to bind, then open the dashboard
sleep 3
open "http://127.0.0.1:8790/"

# close this Terminal window/tab now that the server is detached
osascript -e 'tell application "Terminal" to close (first window whose selected tab’s tty is "'"$(tty)"'")' >/dev/null 2>&1 &
