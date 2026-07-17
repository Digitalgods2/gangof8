#!/bin/bash
# Stop whatever is serving the Gang of 8 dashboard on port 8790 (macOS).

cd "$(dirname "${BASH_SOURCE[0]}")"

PIDS=$(lsof -ti tcp:8790 -sTCP:LISTEN 2>/dev/null)

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
