#!/bin/bash
# Gang of 8 — background launcher (Linux).
# Run from a terminal (./launch-gangof8-background.sh): starts the dashboard
# server detached (the terminal is free to close immediately) and opens the
# dashboard in your default browser. Use ./stop-gangof8.sh to stop it.

cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$(pwd)"

port_pids() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti tcp:8790 -sTCP:LISTEN 2>/dev/null
    elif command -v fuser >/dev/null 2>&1; then
        fuser 8790/tcp 2>/dev/null
    fi
}

# Stop whatever is already listening on the dashboard port before launching.
PIDS="$(port_pids)"
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
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:8790/" >/dev/null 2>&1
else
    echo "(xdg-open not found — open http://127.0.0.1:8790/ manually)"
fi

echo "Gang of 8 running in the background. Dashboard: http://127.0.0.1:8790/"
echo "Logs: $LOG"
echo "Stop it with ./stop-gangof8.sh"
