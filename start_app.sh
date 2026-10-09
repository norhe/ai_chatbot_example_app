#!/usr/bin/env bash
# start_app.sh — Launch all five Voyager Travel processes with Vault AppRole credentials.
#
# Prerequisites:
#   - Vault dev server running; setup_vault.sh already executed
#   - DATABASE_URL set in the environment
#   - vault_approle_creds/<service>.env files present (written by setup_vault.sh)
#
# Usage: ./start_app.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Keycloak auth configuration
# ---------------------------------------------------------------------------
export KEYCLOAK_BASE_URL="${KEYCLOAK_BASE_URL:-http://127.0.0.1:8080}"
export KEYCLOAK_REALM="${KEYCLOAK_REALM:-voyager}"

# ---------------------------------------------------------------------------
# Vault address (default to local dev server)
# ---------------------------------------------------------------------------
export VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"

# ---------------------------------------------------------------------------
# Parse DB_HOST / DB_PORT / DB_NAME from DATABASE_URL
# e.g. postgresql://user:pass@localhost:5432/voyager
# ---------------------------------------------------------------------------
DATABASE_URL="${DATABASE_URL:-postgresql://voyager:voyager@127.0.0.1:5432/voyager}"

eval "$(python3 -c "
from urllib.parse import urlparse
u = urlparse('${DATABASE_URL}')
print('export DB_HOST=' + (u.hostname or ''))
print('export DB_PORT=' + str(u.port or 5432))
print('export DB_NAME=' + u.path.lstrip('/'))"
)"

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
BOOKING_READ_SERVICE_PORT="${BOOKING_READ_SERVICE_PORT:-5101}"
BOOKING_WRITE_SERVICE_PORT="${BOOKING_WRITE_SERVICE_PORT:-5102}"
PAYMENT_READ_SERVICE_PORT="${PAYMENT_READ_SERVICE_PORT:-5103}"
PAYMENT_WRITE_SERVICE_PORT="${PAYMENT_WRITE_SERVICE_PORT:-5104}"

# ---------------------------------------------------------------------------
# Helper: launch a service and store its PID in the named variable.
# Uses a bash nameref so the PID is set in the caller's scope without
# a subshell (which would make the process a grandchild and break the
# Ctrl-C trap).
# ---------------------------------------------------------------------------
_launch_service() {
    local service_name="$1"
    local script_path="$2"
    local port_var="$3"
    local -n _pid_ref="$4"
    local creds_file="scripts/vault_approle_creds/${service_name}.env"

    if [[ ! -f "${creds_file}" ]]; then
        echo "WARNING: ${creds_file} not found — ${service_name} will fall back to DATABASE_URL." >&2
        python3 "${script_path}" &
    else
        # shellcheck source=/dev/null
        set -a
        source "${creds_file}"
        set +a
        export VAULT_ADDR DB_HOST DB_PORT DB_NAME DATABASE_URL
        export "${port_var}"
        python3 "${script_path}" &
    fi
    _pid_ref=$!
}

# ---------------------------------------------------------------------------
# Launch all services, collect PIDs
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Starting Voyager Travel services..."
echo " Vault:            ${VAULT_ADDR}"
echo " Database host:    ${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "============================================================"

ORCHESTRATOR_PID=""
BOOKING_READ_PID=""
BOOKING_WRITE_PID=""
PAYMENT_READ_PID=""
PAYMENT_WRITE_PID=""

# Orchestrator (app.py) — uses DATABASE_URL directly; no Vault role needed
python3 app.py &
ORCHESTRATOR_PID=$!

_launch_service "booking_read_service"     "agents/booking_read_service.py"     "BOOKING_READ_SERVICE_PORT=${BOOKING_READ_SERVICE_PORT}"    BOOKING_READ_PID
_launch_service "booking_mutation_service" "agents/booking_mutation_service.py" "BOOKING_WRITE_SERVICE_PORT=${BOOKING_WRITE_SERVICE_PORT}" BOOKING_WRITE_PID
_launch_service "payment_read_service"     "agents/payment_read_service.py"     "PAYMENT_READ_SERVICE_PORT=${PAYMENT_READ_SERVICE_PORT}" PAYMENT_READ_PID
_launch_service "payment_write_service"    "agents/payment_write_service.py"    "PAYMENT_WRITE_SERVICE_PORT=${PAYMENT_WRITE_SERVICE_PORT}" PAYMENT_WRITE_PID

echo ""
echo " Orchestrator (app.py)         PID ${ORCHESTRATOR_PID}  → http://127.0.0.1:5000"
echo " booking_read_service          PID ${BOOKING_READ_PID}   → http://127.0.0.1:${BOOKING_READ_SERVICE_PORT}"
echo " booking_mutation_service      PID ${BOOKING_WRITE_PID}   → http://127.0.0.1:${BOOKING_WRITE_SERVICE_PORT}"
echo " payment_read_service          PID ${PAYMENT_READ_PID}   → http://127.0.0.1:${PAYMENT_READ_SERVICE_PORT}"
echo " payment_write_service         PID ${PAYMENT_WRITE_PID}   → http://127.0.0.1:${PAYMENT_WRITE_SERVICE_PORT}"
echo "============================================================"
echo " Press Ctrl-C to stop all services."
echo ""

# ---------------------------------------------------------------------------
# Trap SIGINT / SIGTERM — kill all child processes cleanly
# ---------------------------------------------------------------------------
_shutdown() {
    echo ""
    echo "==> Shutting down all services..."
    for pid in "${ORCHESTRATOR_PID}" "${BOOKING_READ_PID}" "${BOOKING_WRITE_PID}" "${PAYMENT_READ_PID}" "${PAYMENT_WRITE_PID}"; do
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
    echo "==> All services stopped."
    exit 0
}

trap _shutdown SIGINT SIGTERM

# Wait for all background processes
wait
