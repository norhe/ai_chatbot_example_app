#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

mkdir -p "$SCRIPT_DIR/pids" "$SCRIPT_DIR/logs"

start_service() {
    local name=$1 script=$2 port=$3

    echo "Starting $name on :$port..."
    cd "$PROJECT_DIR"
    uv run python "$script" > "$SCRIPT_DIR/logs/${name}.log" 2>&1 &
    echo $! > "$SCRIPT_DIR/pids/${name}.pid"

    # Wait up to 5 seconds for the health endpoint to respond
    local attempts=0
    until curl -sf "http://127.0.0.1:${port}/health" > /dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ $attempts -ge 10 ]; then
            echo "ERROR: $name failed to start. Check services/logs/${name}.log"
            exit 1
        fi
        sleep 0.5
    done
    echo "  OK: $name running (PID $(cat "$SCRIPT_DIR/pids/${name}.pid"))"
}

echo "Starting Voyager Travel agent services..."
start_service read_service              agents/read_service.py              5101
start_service booking_mutation_service  agents/booking_mutation_service.py  5102
start_service payment_read_service      agents/payment_read_service.py      5103
start_service payment_write_service     agents/payment_write_service.py     5104
echo "All services started."
