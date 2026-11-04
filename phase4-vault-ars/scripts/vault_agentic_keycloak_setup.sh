#!/usr/bin/env bash
# vault_agentic_keycloak_setup.sh — One-time Vault infrastructure setup for
#                                   the Keycloak-backed Agentic IAM workflow.
#
# Performs the Vault-side setup that is shared across all agents:
#   1. Create the OAuth Resource Server profile (jwt_type, JWKS, audience)
#   2. Verify the profile and export config_id
#   3. Write the voyager-agent-ceiling policy (upper bound for all agents)
#
# Per-agent registration (entity, alias, agent-registry) is handled separately
# by scripts/vault_register_agents.sh, which must be run after this script.
#
# Prerequisites:
#   - vault CLI on PATH, jq on PATH
#   - VAULT_ADDR set (default: http://127.0.0.1:8200)
#   - VAULT_TOKEN set to a Vault admin token
#   - Keycloak voyager realm configured
#     (run scripts/setup_keycloak_kcadm.sh and scripts/keycloak_ai_vault_integration.sh first)
#
# Usage:
#   export VAULT_TOKEN="<admin-token>"
#   ./scripts/vault_agentic_keycloak_setup.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Guard: verify required tooling
# ---------------------------------------------------------------------------
for _cmd in vault jq; do
    if ! command -v "${_cmd}" &>/dev/null; then
        echo "ERROR: '${_cmd}' not found on PATH." >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 1. Environment — derived from values shared across other setup scripts
# ---------------------------------------------------------------------------
VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR

: "${VAULT_TOKEN:?ERROR: VAULT_TOKEN is not set. Export a Vault admin token before running.}"
export VAULT_TOKEN

KEYCLOAK_BASE_URL="${KEYCLOAK_BASE_URL:-http://127.0.0.1:8080}"
REALM="voyager"

ISSUER="${KEYCLOAK_BASE_URL}/realms/${REALM}"
JWKS_URI="${KEYCLOAK_BASE_URL}/realms/${REALM}/protocol/openid-connect/certs"

OAUTH_PROFILE_NAME="keycloak"
CEILING_POLICY_NAME="voyager-agent-ceiling"

echo "==> Vault target  : ${VAULT_ADDR}"
echo "==> Keycloak realm: ${ISSUER}"
echo ""

# ---------------------------------------------------------------------------
# 2. Create the OAuth Resource Server profile
#    - jwt_type: access_token  (RFC 9068 — clients emit typ=at+JWT via access.token.header.type)
#    - audiences: issuer URL   (valid URI per RFC 7519; matches Keycloak audience mapper)
#    - PS256 preferred, RS256 as fallback (per Vault recommendation)
# ---------------------------------------------------------------------------
echo "==> Writing OAuth Resource Server profile '${OAUTH_PROFILE_NAME}'..."

_existing_profile=$(vault read -format=json \
    sys/config/oauth-resource-server/"${OAUTH_PROFILE_NAME}" 2>/dev/null) || true
_existing_issuer=$(echo "${_existing_profile}" | jq -r '.data.issuer_id // empty' 2>/dev/null) || true
if [[ "${_existing_issuer}" == "${ISSUER}" ]]; then
    echo "    Profile '${OAUTH_PROFILE_NAME}' already exists with correct issuer — skipping."
else
    if [[ -n "${_existing_issuer}" ]]; then
        echo "    Profile exists but issuer_id is '${_existing_issuer}' — overwriting with '${ISSUER}'."
    fi
    OAUTH_JSON=$(mktemp --suffix=.json)
    trap 'rm -f "${OAUTH_JSON}"' EXIT

    cat > "${OAUTH_JSON}" <<EOF
{
  "issuer_id": "${ISSUER}",
  "use_jwks": true,
  "jwks_uri": "${JWKS_URI}",
  "jwt_type": "access_token",
  "audiences": [
    "${ISSUER}"
  ],
  "user_claim": "sub",
  "supported_algorithms": [
    "PS256",
    "RS256"
  ]
}
EOF

    _write_out=""
    if ! _write_out=$(vault write -format=json \
            sys/config/oauth-resource-server/"${OAUTH_PROFILE_NAME}" \
            @"${OAUTH_JSON}" 2>&1); then
        echo "ERROR: Failed to write OAuth Resource Server profile '${OAUTH_PROFILE_NAME}'." >&2
        echo "${_write_out}" >&2
        exit 1
    fi

    echo "    Profile '${OAUTH_PROFILE_NAME}' written."
fi

# ---------------------------------------------------------------------------
# 3. Verify the profile and capture config_id
# ---------------------------------------------------------------------------
echo ""
echo "==> Verifying OAuth Resource Server profile..."
if ! vault read sys/config/oauth-resource-server/"${OAUTH_PROFILE_NAME}" 2>&1; then
    echo "ERROR: Could not read back OAuth profile '${OAUTH_PROFILE_NAME}' — write may have failed." >&2
    exit 1
fi

PROFILE_ID=$(vault read -format=json \
    sys/config/oauth-resource-server/"${OAUTH_PROFILE_NAME}" | \
    jq -r '.data.config_id')
echo ""
echo "    config_id: ${PROFILE_ID}"

# ---------------------------------------------------------------------------
# 4. Write the ceiling policy
#    Upper bound on all permissions any registered agent may ever exercise.
#    Scoped to read dynamic database credentials for the four voyager roles.
# ---------------------------------------------------------------------------
echo ""
echo "==> Writing ceiling policy '${CEILING_POLICY_NAME}'..."

_policy_out=""
if ! _policy_out=$(vault policy write "${CEILING_POLICY_NAME}" - 2>&1 <<'EOF'
# Ceiling policy for all voyager AI agents.
# This is the upper bound on all permissions granted via the agent registry.
# Tokens issued to any agent can never exceed the paths listed here,
# regardless of what the JWT authorization_details claim requests.

# Read-only database credentials (voyager read service)
path "database/creds/voyager_booking_read" {
  capabilities = ["read"]
}

# Booking write database credentials
path "database/creds/voyager_booking_write" {
  capabilities = ["read"]
}

# Payment read database credentials
path "database/creds/voyager_payment_read" {
  capabilities = ["read"]
}

# Payment write database credentials
path "database/creds/voyager_payment_write" {
  capabilities = ["read"]
}

# Allow agents to renew their own leases
path "sys/leases/renew" {
  capabilities = ["update"]
}

# Allow agents to look up their own token
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
EOF
); then
    echo "ERROR: Failed to write ceiling policy '${CEILING_POLICY_NAME}'." >&2
    echo "${_policy_out}" >&2
    exit 1
fi

echo "    Ceiling policy '${CEILING_POLICY_NAME}' written."

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Vault Agentic IAM infrastructure setup complete!"
echo "============================================================"
echo " OAuth profile    : ${OAUTH_PROFILE_NAME}"
echo "   issuer_id      : ${ISSUER}"
echo "   jwks_uri       : ${JWKS_URI}"
echo "   jwt_type       : jwt"
echo "   audiences      : ${ISSUER}"
echo "   config_id      : ${PROFILE_ID}"
echo ""
echo " Ceiling policy   : ${CEILING_POLICY_NAME}"
echo ""
echo " Next step: ./scripts/vault_register_agents.sh"
echo "============================================================"
