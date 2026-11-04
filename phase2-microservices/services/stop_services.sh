#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Stopping Voyager Travel agent services..."
for pidfile in "$SCRIPT_DIR"/pids/*.pid; do
    [ -f "$pidfile" ] || continue
    name=$(basename "$pidfile" .pid)
    pid=$(cat "$pidfile")
    if kill "$pid" 2>/dev/null; then
        echo "  Stopped $name (PID $pid)"
    else
        echo "  $name (PID $pid) was not running"
    fi
    rm -f "$pidfile"
done
echo "Done."
