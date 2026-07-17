#!/bin/bash
# Stop whatever is serving the Gang of 8 dashboard on port 8790 (Linux).

cd "$(dirname "${BASH_SOURCE[0]}")"

port_pids() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti tcp:8790 -sTCP:LISTEN 2>/dev/null
    elif command -v fuser >/dev/null 2>&1; then
        fuser 8790/tcp 2>/dev/null
    fi
}

PIDS="$(port_pids)"

if [ -n "$PIDS" ]; then
    kill -9 $PIDS 2>/dev/null
    echo "Gang of 8 stopped."
else
    echo "Nothing was running on port 8790."
fi

rm -f .gangof8-server.pid

if [ "$1" != "--no-wait" ]; then
    sleep 2
fi
