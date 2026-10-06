#!/usr/bin/env bash
# setup_vault.sh — One-shot idempotent Vault configuration for Voyager Travel demo.
# Configures the database secrets engine, four scoped Vault roles + policies,
# and AppRole credentials for each microservice.
#
# Prerequisites:
#   - A Vault dev-mode server running and reachable
#   - VAULT_ADDR set (e.g. http://127.0.0.1:8200)
#   - VAULT_TOKEN set to the root/dev token
#   - DATABASE_URL set (default: postgresql://voyager:voyager@127.0.0.1:5432/voyager)
#
# Usage: ./setup_vault.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Guard: verify required tooling and environment
# ---------------------------------------------------------------------------
if ! command -v vault &>/dev/null; then
    echo "ERROR: 'vault' CLI not found on PATH." >&2
    echo "Install Vault: https://developer.hashicorp.com/vault/downloads" >&2
    exit 1
fi

: "${VAULT_ADDR:?ERROR: VAULT_ADDR is not set. Example: export VAULT_ADDR=http://127.0.0.1:8200}"
: "${VAULT_TOKEN:?ERROR: VAULT_TOKEN is not set. Use the root token from your Vault dev server.}"
DATABASE_URL="${DATABASE_URL:-postgresql://voyager:voyager@127.0.0.1:5432/voyager}"

echo "==> Vault target: ${VAULT_ADDR}"
echo "==> Verifying Vault is reachable..."
if ! vault status -format=json 2>&1 | grep -q '"initialized"'; then
    echo "ERROR: Cannot reach Vault at ${VAULT_ADDR} or Vault is not initialized." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Enable the database secrets engine (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "==> Enabling database secrets engine..."
if vault secrets list -format=json 2>/dev/null | grep -q '"database/"'; then
    echo "    database/ already enabled — skipping."
else
    if ! vault secrets enable -path=database database 2>&1; then
        echo "ERROR: Failed to enable database secrets engine." >&2
        exit 1
    fi
    echo "    database/ enabled."
fi

# ---------------------------------------------------------------------------
# 2. Configure the voyager-postgres connection
# ---------------------------------------------------------------------------
echo ""
echo "==> Configuring voyager-postgres database connection..."
_db_cfg_out=""
if ! _db_cfg_out=$(vault write -format=json database/config/voyager-postgres \
        plugin_name=postgresql-database-plugin \
        connection_url="${DATABASE_URL}" \
        allowed_roles="voyager_booking_read,voyager_booking_write,voyager_payment_read,voyager_payment_write" \
        2>&1); then
    echo "ERROR: Failed to configure voyager-postgres database connection." >&2
    echo "${_db_cfg_out}" >&2
    exit 1
fi
echo "    voyager-postgres connection configured."

# ---------------------------------------------------------------------------
# 3. Write the four database roles
# ---------------------------------------------------------------------------
echo ""
echo "==> Writing database roles..."

_write_db_role() {
    local role_name="$1" statements="$2"
    local _out
    if ! _out=$(vault write -format=json database/roles/"${role_name}" \
            db_name=voyager-postgres \
            default_ttl=1h \
            max_ttl=24h \
            creation_statements="${statements}" \
            2>&1); then
        echo "ERROR: Failed to write database role '${role_name}'." >&2
        echo "${_out}" >&2
        exit 1
    fi
}

_write_db_role "voyager_booking_read" \
    "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON bookings TO \"{{name}}\";"
_write_db_role "voyager_booking_write" \
    "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT, INSERT, UPDATE ON bookings TO \"{{name}}\";"
_write_db_role "voyager_payment_read" \
    "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON payment_cards TO \"{{name}}\";"
_write_db_role "voyager_payment_write" \
    "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT, INSERT, UPDATE, DELETE ON payment_cards TO \"{{name}}\";"

echo "    Four database roles written."

# ---------------------------------------------------------------------------
# 4. Write the four Vault policies
# ---------------------------------------------------------------------------
echo ""
echo "==> Writing Vault policies..."

_write_policy() {
    local policy_name="$1" policy_body="$2"
    local _out
    if ! _out=$(echo "${policy_body}" | vault policy write "${policy_name}" - 2>&1); then
        echo "ERROR: Failed to write Vault policy '${policy_name}'." >&2
        echo "${_out}" >&2
        exit 1
    fi
}

_write_policy "voyager-booking-read-policy"  'path "database/creds/voyager_booking_read"  { capabilities = ["read"] }'
_write_policy "voyager-booking-write-policy" 'path "database/creds/voyager_booking_write" { capabilities = ["read"] }'
_write_policy "voyager-payment-read-policy"  'path "database/creds/voyager_payment_read"  { capabilities = ["read"] }'
_write_policy "voyager-payment-write-policy" 'path "database/creds/voyager_payment_write" { capabilities = ["read"] }'

echo "    Four policies written."

# ---------------------------------------------------------------------------
# 5. Enable AppRole auth method (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "==> Enabling AppRole auth method..."
if vault auth list -format=json 2>/dev/null | grep -q '"approle/"'; then
    echo "    approle/ already enabled — skipping."
else
    if ! vault auth enable approle 2>&1; then
        echo "ERROR: Failed to enable AppRole auth method." >&2
        exit 1
    fi
    echo "    approle/ enabled."
fi

# ---------------------------------------------------------------------------
# 6. Create AppRole credentials directory
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPROLE_CREDS_DIR="${REPO_ROOT}/scripts/vault_approle_creds"
mkdir -p "${APPROLE_CREDS_DIR}"
chmod 700 "${APPROLE_CREDS_DIR}"

# ---------------------------------------------------------------------------
# 7. Create one AppRole per service, write creds files
# ---------------------------------------------------------------------------
echo ""
echo "==> Creating AppRole roles and writing credential files..."

_write_approle_creds() {
    local role_name="$1"
    local vault_policy="$2"
    local service_name="$3"

    local _out
    if ! _out=$(vault write -format=json auth/approle/role/"${role_name}" \
            token_policies="${vault_policy}" \
            token_ttl=1h \
            token_max_ttl=24h \
            2>&1); then
        echo "ERROR: Failed to create AppRole '${role_name}'." >&2
        echo "${_out}" >&2
        return 1
    fi

    local role_id
    if ! role_id=$(vault read -field=role_id auth/approle/role/"${role_name}"/role-id 2>&1); then
        echo "ERROR: Failed to read role_id for AppRole '${role_name}'." >&2
        echo "${role_id}" >&2
        return 1
    fi

    local secret_id
    if ! secret_id=$(vault write -field=secret_id -f auth/approle/role/"${role_name}"/secret-id 2>&1); then
        echo "ERROR: Failed to generate secret_id for AppRole '${role_name}'." >&2
        echo "${secret_id}" >&2
        return 1
    fi

    local creds_file="${APPROLE_CREDS_DIR}/${service_name}.env"
    printf "VAULT_ROLE_ID=%s\nVAULT_SECRET_ID=%s\n" "${role_id}" "${secret_id}" > "${creds_file}"
    chmod 600 "${creds_file}"
    echo "    ${creds_file} written."
}

_write_approle_creds "voyager-booking-read"   "voyager-booking-read-policy"  "booking_read_service"
_write_approle_creds "voyager-booking-write"  "voyager-booking-write-policy" "booking_mutation_service"
_write_approle_creds "voyager-payment-read"   "voyager-payment-read-policy"  "payment_read_service"
_write_approle_creds "voyager-payment-write"  "voyager-payment-write-policy" "payment_write_service"

# ---------------------------------------------------------------------------
# 8. Success summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Vault setup complete!"
echo "============================================================"
echo " Database engine : ${VAULT_ADDR}/v1/database/"
echo " Credential files:"
for f in "${APPROLE_CREDS_DIR}"/*.env; do
    echo "   ${f}"
done
echo ""
echo " Next step: ./start_app.sh"
echo "============================================================"
