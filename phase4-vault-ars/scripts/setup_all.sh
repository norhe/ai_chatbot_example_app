#!/usr/bin/env bash
# setup_all.sh — Run the full Voyager Agentic IAM setup in the correct order.
#
# Executes each setup script sequentially, stopping on the first failure.
# All scripts are idempotent — safe to re-run if a step needs to be repeated.
#
# Order:
#   1. setup_postgres.sh                Start PostgreSQL container, wait for readiness
#   2. setup_keycloak_kcadm.sh          Start Keycloak, create voyager realm and clients
#   3. keycloak_ai_vault_integration.sh  Add vault-oidc client and audience mapper to voyager realm
#   3b. db.py                           Create tables and seed data (required before Vault roles are used)
#   4. setup_vault.sh                   Enable database engine, roles, policies, AppRole creds
#   5. vault_agentic_keycloak_setup.sh  Create Vault OAuth profile and agent ceiling policy
#   6. vault_register_agents.sh         Register each service agent (entity, alias, registry)
#
# Prerequisites:
#   - podman, vault, jq, curl on PATH
#   - VAULT_ADDR set (default: http://127.0.0.1:8200)
#   - VAULT_TOKEN set to a Vault admin token
#   - DATABASE_URL set (default: postgresql://voyager:voyager@127.0.0.1:5432/voyager)
#   - KEYCLOAK_ADMIN_PASSWORD set (default: admin)
#
# Usage:
#   export VAULT_TOKEN="<admin-token>"
#   export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
#   ./scripts/setup_all.sh

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_step() {
    local num="$1"
    local label="$2"
    echo ""
    echo "############################################################"
    echo "# Step ${num}: ${label}"
    echo "############################################################"
}

_done() {
    local label="$1"
    echo ""
    echo "    ✔ ${label} complete."
}

# ---------------------------------------------------------------------------
# Pre-flight: verify required env vars are present before starting
# ---------------------------------------------------------------------------
echo "==> Pre-flight checks..."

for _cmd in podman vault jq curl; do
    if ! command -v "${_cmd}" &>/dev/null; then
        echo "ERROR: '${_cmd}' not found on PATH." >&2
        exit 1
    fi
done

: "${VAULT_ADDR:?ERROR: VAULT_ADDR is not set.  Example: export VAULT_ADDR=http://127.0.0.1:8200}"
: "${VAULT_TOKEN:?ERROR: VAULT_TOKEN is not set.  Export a Vault admin token before running.}"
DATABASE_URL="${DATABASE_URL:-postgresql://voyager:voyager@127.0.0.1:5432/voyager}"

echo "    All pre-flight checks passed."

# ---------------------------------------------------------------------------
# Step 1 — PostgreSQL container
# ---------------------------------------------------------------------------
_step 1 "PostgreSQL container"
bash "${SCRIPTS_DIR}/setup_postgres.sh"
_done "setup_postgres.sh"

# ---------------------------------------------------------------------------
# Step 2 — Keycloak: voyager realm + service-account clients
# ---------------------------------------------------------------------------
_step 2 "Keycloak base setup (voyager realm)"
bash "${SCRIPTS_DIR}/setup_keycloak_kcadm.sh"
_done "setup_keycloak_kcadm.sh"

# ---------------------------------------------------------------------------
# Step 3 — Keycloak: vault-oidc client + audience mapper in voyager realm
# ---------------------------------------------------------------------------
_step 3 "Keycloak vault-oidc client + audience mapper (voyager realm)"
bash "${SCRIPTS_DIR}/keycloak_ai_vault_integration.sh"
_done "keycloak_ai_vault_integration.sh"

# ---------------------------------------------------------------------------
# Step 3b — Database schema + seed data
# Must run before setup_vault.sh: the Vault role creation statements GRANT on
# the users/bookings/payment_cards tables, which must already exist.
# ---------------------------------------------------------------------------
_step "3b" "Database schema + seed data"
export DATABASE_URL
"${PYTHON:-python3}" "${SCRIPTS_DIR}/../db.py"
_done "db.py (schema + seed)"

# ---------------------------------------------------------------------------
# Step 4 — Vault: database engine, roles, policies, AppRole credentials
# ---------------------------------------------------------------------------
_step 4 "Vault database engine + AppRole credentials"
bash "${SCRIPTS_DIR}/setup_vault.sh"
_done "setup_vault.sh"

# ---------------------------------------------------------------------------
# Step 5 — Vault: OAuth Resource Server profile + ceiling policy
# ---------------------------------------------------------------------------
_step 5 "Vault OAuth profile + ceiling policy"
bash "${SCRIPTS_DIR}/vault_agentic_keycloak_setup.sh"
_done "vault_agentic_keycloak_setup.sh"

# ---------------------------------------------------------------------------
# Step 6 — Vault: per-agent entity, alias, and registry registration
# ---------------------------------------------------------------------------
_step 6 "Vault agent registration"
bash "${SCRIPTS_DIR}/vault_register_agents.sh"
_done "vault_register_agents.sh"

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Full Voyager Agentic IAM setup complete!"
echo "============================================================"
echo ""
echo " Keycloak"
echo "   Voyager realm  : ${KEYCLOAK_BASE_URL:-http://127.0.0.1:8080}/realms/voyager"
echo ""
echo " Vault"
echo "   Database engine : ${VAULT_ADDR}/v1/database/"
echo "   OAuth profile   : keycloak"
echo "   Ceiling policy  : voyager-agent-ceiling"
echo "   AppRole creds   : scripts/vault_approle_creds/"
echo ""
echo " Registered agents:"
echo "   voyager-booking-read-service-iam"
echo "   voyager-booking-write-service-iam"
echo "   voyager-payment-read-service-iam"
echo "   voyager-payment-write-service-iam"
echo ""
echo " Next step: ./start_app.sh"
echo "============================================================"
