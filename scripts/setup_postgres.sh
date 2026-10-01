#!/usr/bin/env bash
# setup_postgres.sh — Ensure the voyager-postgres container is present and running.
#
# Idempotent: existing container is left unchanged; a stopped container is
# started; a missing container is created from scratch.
#
# Prerequisites:
#   - podman available on PATH
#
# Usage:
#   ./scripts/setup_postgres.sh
#
# Optional env vars:
#   KEYCLOAK_BASE_URL        — used only for consistency; not needed here
#   POSTGRES_DATA_DIR        — host path for the data volume
#                              (default: <repo-root>/postgres-data)
#   POSTGRES_USER            — database superuser (default: voyager)
#   POSTGRES_PASSWORD        — superuser password; read from env, never hardcoded
#   POSTGRES_DB              — database name (default: voyager)

set -euo pipefail

CONTAINER_NAME="voyager-postgres"
POSTGRES_IMAGE="registry.redhat.io/rhel9/postgresql-16:latest"

# Resolve the repository root (one level up from this script's directory)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-${REPO_ROOT}/postgres-data}"
POSTGRES_USER="${POSTGRES_USER:-voyager}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-voyager}"
POSTGRES_DB="${POSTGRES_DB:-voyager}"

# ---------------------------------------------------------------------------
# Guard: verify podman is available
# ---------------------------------------------------------------------------
if ! command -v podman &>/dev/null; then
    echo "ERROR: 'podman' not found on PATH." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Ensure the data directory exists on the host
# ---------------------------------------------------------------------------
echo "==> Ensuring data directory '${POSTGRES_DATA_DIR}' exists..."
mkdir -p "${POSTGRES_DATA_DIR}"
echo "    Data directory ready."

# ---------------------------------------------------------------------------
# 2. Start container (idempotent)
# ---------------------------------------------------------------------------
echo "==> Starting PostgreSQL container '${CONTAINER_NAME}'..."

_container_status() {
    podman inspect --format '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || true
}

if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
    STATUS=$(_container_status)
    case "${STATUS}" in
        running)
            echo "    Container already running — skipping."
            ;;
        paused)
            echo "    Container is paused — resuming."
            podman unpause "${CONTAINER_NAME}"
            ;;
        *)
            echo "    Container exists (status: ${STATUS:-unknown}) — starting it."
            podman start "${CONTAINER_NAME}"
            ;;
    esac
else
    if ! podman run -d \
        --name "${CONTAINER_NAME}" \
        -p 127.0.0.1:5432:5432 \
        -e POSTGRESQL_USER="${POSTGRES_USER}" \
        -e POSTGRESQL_PASSWORD="${POSTGRES_PASSWORD}" \
        -e POSTGRESQL_DATABASE="${POSTGRES_DB}" \
        -v "${POSTGRES_DATA_DIR}:/var/lib/pgsql/data:Z" \
        "${POSTGRES_IMAGE}"; then
        # A race between 'container exists' and 'run' can leave the container
        # created-but-not-running.  If the container is now present, start it.
        if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
            STATUS=$(_container_status)
            if [[ "${STATUS}" != "running" ]]; then
                echo "    Container appeared during run — starting it."
                podman start "${CONTAINER_NAME}"
            else
                echo "    Container already running after race — continuing."
            fi
        else
            echo "ERROR: 'podman run' failed and container is not present." >&2
            exit 1
        fi
    else
        echo "    Container started."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Wait for PostgreSQL to be ready (poll pg_isready inside the container)
# ---------------------------------------------------------------------------
echo "==> Waiting for PostgreSQL to be ready..."

_wait_for_postgres() {
    local retries=30
    local interval=3
    local i=0
    until podman exec "${CONTAINER_NAME}" \
            pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
            &>/dev/null; do
        i=$((i + 1))
        if [[ ${i} -ge ${retries} ]]; then
            echo "ERROR: PostgreSQL did not become ready after $((retries * interval))s." >&2
            exit 1
        fi
        echo "    Not ready yet — retrying in ${interval}s... (attempt ${i}/${retries})"
        sleep "${interval}"
    done
}
_wait_for_postgres
echo "    PostgreSQL is ready."

# ---------------------------------------------------------------------------
# 4. Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " PostgreSQL container setup complete!"
echo "============================================================"
echo " Container : ${CONTAINER_NAME}"
echo " Image     : ${POSTGRES_IMAGE}"
echo " Endpoint  : 127.0.0.1:5432"
echo " Database  : ${POSTGRES_DB}"
echo " User      : ${POSTGRES_USER}"
echo " Data dir  : ${POSTGRES_DATA_DIR}"
echo ""
echo " Next step: ./scripts/setup_keycloak_kcadm.sh"
echo "============================================================"
