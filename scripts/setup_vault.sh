#!/usr/bin/env bash
# setup_vault.sh — One-shot idempotent Vault configuration for Voyager Travel demo.
# Configures the database secrets engine, four scoped Vault roles + policies,
# and AppRole credentials for each microservice.
#
# Prerequisites:
#   - A Vault dev-mode server running and reachable
#   - VAULT_ADDR set (e.g. http://127.0.0.1:8200)
#   - VAULT_TOKEN set to the root/dev token
#   - DATABASE_URL set (postgresql://user:pass@host:5432/dbname)
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
: "${DATABASE_URL:?ERROR: DATABASE_URL is not set. Example: export DATABASE_URL=postgresql://user:pass@host:5432/dbname}"

echo "==> Vault target: ${VAULT_ADDR}"
echo "==> Verifying Vault is reachable..."
vault status -format=json | grep -q '"initialized"' || { echo "ERROR: Cannot reach Vault at ${VAULT_ADDR}"; exit 1; }

# ---------------------------------------------------------------------------
# 1. Enable the database secrets engine (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "==> Enabling database secrets engine..."
if vault secrets list -format=json | grep -q '"database/"'; then
    echo "    database/ already enabled — skipping."
else
    vault secrets enable -path=database database
    echo "    database/ enabled."
fi

# ---------------------------------------------------------------------------
# 2. Configure the voyager-postgres connection
# ---------------------------------------------------------------------------
echo ""
echo "==> Configuring voyager-postgres database connection..."
vault write database/config/voyager-postgres \
    plugin_name=postgresql-database-plugin \
    connection_url="${DATABASE_URL}" \
    allowed_roles="voyager_read,voyager_booking_write,voyager_payment_read,voyager_payment_write"
echo "    voyager-postgres connection configured."

# ---------------------------------------------------------------------------
# 3. Write the four database roles
# ---------------------------------------------------------------------------
echo ""
echo "==> Writing database roles..."

vault write database/roles/voyager_read \
    db_name=voyager-postgres \
    default_ttl=1h \
    max_ttl=24h \
    creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON bookings TO \"{{name}}\";"

vault write database/roles/voyager_booking_write \
    db_name=voyager-postgres \
    default_ttl=1h \
    max_ttl=24h \
    creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT, INSERT, UPDATE ON bookings TO \"{{name}}\";"

vault write database/roles/voyager_payment_read \
    db_name=voyager-postgres \
    default_ttl=1h \
    max_ttl=24h \
    creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON payment_cards TO \"{{name}}\";"

vault write database/roles/voyager_payment_write \
    db_name=voyager-postgres \
    default_ttl=1h \
    max_ttl=24h \
    creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT, INSERT, UPDATE, DELETE ON payment_cards TO \"{{name}}\";"

echo "    Four database roles written."

# ---------------------------------------------------------------------------
# 4. Write the four Vault policies
# ---------------------------------------------------------------------------
echo ""
echo "==> Writing Vault policies..."

vault policy write voyager-read-policy - <<'EOF'
path "database/creds/voyager_read" {
  capabilities = ["read"]
}
EOF

vault policy write voyager-booking-write-policy - <<'EOF'
path "database/creds/voyager_booking_write" {
  capabilities = ["read"]
}
EOF

vault policy write voyager-payment-read-policy - <<'EOF'
path "database/creds/voyager_payment_read" {
  capabilities = ["read"]
}
EOF

vault policy write voyager-payment-write-policy - <<'EOF'
path "database/creds/voyager_payment_write" {
  capabilities = ["read"]
}
EOF

echo "    Four policies written."

# ---------------------------------------------------------------------------
# 5. Enable AppRole auth method (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "==> Enabling AppRole auth method..."
if vault auth list -format=json | grep -q '"approle/"'; then
    echo "    approle/ already enabled — skipping."
else
    vault auth enable approle
    echo "    approle/ enabled."
fi

# ---------------------------------------------------------------------------
# 6. Create AppRole credentials directory
# ---------------------------------------------------------------------------
mkdir -p vault_approle_creds
chmod 700 vault_approle_creds

# ---------------------------------------------------------------------------
# 7. Create one AppRole per service, write creds files
# ---------------------------------------------------------------------------
echo ""
echo "==> Creating AppRole roles and writing credential files..."

_write_approle_creds() {
    local role_name="$1"
    local vault_policy="$2"
    local service_name="$3"

    vault write auth/approle/role/"${role_name}" \
        token_policies="${vault_policy}" \
        token_ttl=1h \
        token_max_ttl=24h

    local role_id
    role_id=$(vault read -field=role_id auth/approle/role/"${role_name}"/role-id)

    local secret_id
    secret_id=$(vault write -field=secret_id -f auth/approle/role/"${role_name}"/secret-id)

    local creds_file="vault_approle_creds/${service_name}.env"
    printf "VAULT_ROLE_ID=%s\nVAULT_SECRET_ID=%s\n" "${role_id}" "${secret_id}" > "${creds_file}"
    chmod 600 "${creds_file}"
    echo "    ${creds_file} written."
}

_write_approle_creds "voyager-read"           "voyager-read-policy"          "read_service"
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
for f in vault_approle_creds/*.env; do
    echo "   ${f}"
done
echo ""
echo " Next step: ./start_app.sh"
echo "============================================================"
