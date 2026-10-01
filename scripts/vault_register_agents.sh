#!/usr/bin/env bash
# vault_register_agents.sh — Register each Voyager service agent in Vault's
#                            agent registry, wired to the Keycloak voyager realm.
#
# For each of the four Voyager services this script:
#   1. Fetches a client-credentials token from Keycloak for that service's client
#   2. Extracts the 'sub' claim (the stable Keycloak service-account subject)
#   3. Creates (or confirms) a Vault Identity entity with external_id = sub
#   4. Creates an entity alias using issuer= so Vault resolves it at runtime
#   5. Registers the entity in the Vault agent registry with the ceiling policy
#   6. Validates the token with vault token lookup
#
# Prerequisites:
#   - vault CLI on PATH, jq on PATH, curl on PATH
#   - VAULT_ADDR set (default: http://127.0.0.1:8200)
#   - VAULT_TOKEN set to a Vault admin token
#   - scripts/vault_agentic_keycloak_setup.sh already run (creates OAuth profile
#     and ceiling policy)
#   - Keycloak container 'voyager-keycloak' running on http://127.0.0.1:8080
#     (run scripts/setup_keycloak_kcadm.sh and scripts/keycloak_ai_vault_integration.sh first)
#
# Usage:
#   export VAULT_TOKEN="<admin-token>"
#   ./scripts/vault_register_agents.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Guard: verify required tooling
# ---------------------------------------------------------------------------
for _cmd in vault jq curl; do
    if ! command -v "${_cmd}" &>/dev/null; then
        echo "ERROR: '${_cmd}' not found on PATH." >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Environment — shared with other setup scripts
# ---------------------------------------------------------------------------
VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR

: "${VAULT_TOKEN:?ERROR: VAULT_TOKEN is not set. Export a Vault admin token before running.}"
export VAULT_TOKEN

KEYCLOAK_BASE_URL="${KEYCLOAK_BASE_URL:-http://127.0.0.1:8080}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
REALM="voyager"
KCADM="podman exec voyager-keycloak /opt/keycloak/bin/kcadm.sh"

ISSUER="${KEYCLOAK_BASE_URL}/realms/${REALM}"
TOKEN_ENDPOINT="${KEYCLOAK_BASE_URL}/realms/${REALM}/protocol/openid-connect/token"
CEILING_POLICY_NAME="voyager-agent-ceiling"
OAUTH_PROFILE_NAME="keycloak"

echo "==> Vault target  : ${VAULT_ADDR}"
echo "==> Keycloak realm: ${ISSUER}"
echo ""

# ---------------------------------------------------------------------------
# Verify the OAuth profile and ceiling policy exist before proceeding
# ---------------------------------------------------------------------------
echo "==> Verifying prerequisites..."

if ! vault read -format=json sys/config/oauth-resource-server/"${OAUTH_PROFILE_NAME}" &>/dev/null; then
    echo "ERROR: OAuth profile '${OAUTH_PROFILE_NAME}' not found in Vault." >&2
    echo "       Run scripts/vault_agentic_keycloak_setup.sh first." >&2
    exit 1
fi

if ! vault policy read "${CEILING_POLICY_NAME}" &>/dev/null; then
    echo "ERROR: Ceiling policy '${CEILING_POLICY_NAME}' not found in Vault." >&2
    echo "       Run scripts/vault_agentic_keycloak_setup.sh first." >&2
    exit 1
fi

echo "    OAuth profile and ceiling policy confirmed."

# ---------------------------------------------------------------------------
# Authenticate kcadm once for the entire run
# ---------------------------------------------------------------------------
echo ""
echo "==> Authenticating kcadm with Keycloak..."
${KCADM} config credentials \
    --server "${KEYCLOAK_BASE_URL}" \
    --realm master \
    --user "${KEYCLOAK_ADMIN}" \
    --password "${KEYCLOAK_ADMIN_PASSWORD}" \
    &>/dev/null
echo "    kcadm authenticated."

# ---------------------------------------------------------------------------
# _register_agent <keycloak_client_id> <entity_name> <display_name>
#
#   keycloak_client_id — the client ID in the voyager realm
#                        (e.g. "voyager-read")
#   entity_name        — Vault Identity entity name
#                        (e.g. "voyager-read-agent")
#   display_name       — label shown in the agent registry
#                        (e.g. "voyager-read-service-iam")
# ---------------------------------------------------------------------------
_register_agent() {
    local kc_client_id="$1"
    local entity_name="$2"
    local display_name="$3"

    echo ""
    echo "------------------------------------------------------------"
    echo " Registering: ${display_name}"
    echo "------------------------------------------------------------"

    # 1. Retrieve this client's secret from Keycloak
    local kc_client_uuid
    kc_client_uuid=$(${KCADM} get clients \
        -r "${REALM}" \
        --query "clientId=${kc_client_id}" \
        --fields id \
        --format csv \
        --noquotes 2>/dev/null | head -1)

    if [[ -z "${kc_client_uuid}" ]]; then
        echo "ERROR: Keycloak client '${kc_client_id}' not found in realm '${REALM}'." >&2
        echo "       Run scripts/keycloak_ai_vault_integration.sh first." >&2
        return 1
    fi

    local client_secret
    client_secret=$(${KCADM} get \
        "clients/${kc_client_uuid}/client-secret" \
        -r "${REALM}" | jq -r '.value')

    # 2. Fetch a client-credentials token and extract the sub claim
    local token_response
    token_response=$(curl -s \
        -X POST \
        "${TOKEN_ENDPOINT}" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "grant_type=client_credentials" \
        -d "client_id=${kc_client_id}" \
        -d "client_secret=${client_secret}")

    if ! echo "${token_response}" | jq -e '.access_token' &>/dev/null; then
        echo "ERROR: Failed to obtain token for '${kc_client_id}'." >&2
        echo "${token_response}" | jq . >&2
        return 1
    fi

    local access_token subject
    access_token=$(echo "${token_response}" | jq -r '.access_token')
    subject=$(echo "${access_token}" | \
        jq -R 'split(".")[1] | @base64d | fromjson | .sub' -r)

    echo "    sub claim  : ${subject}"

    # 3. Create (or confirm) the Vault Identity entity
    if vault read -format=json identity/entity/name/"${entity_name}" &>/dev/null; then
        echo "    Entity '${entity_name}' already exists — skipping creation."
    else
        vault write identity/entity \
            name="${entity_name}" \
            policies=default \
            external_id="${subject}" \
            &>/dev/null
        echo "    Entity '${entity_name}' created."
    fi

    local entity_id
    entity_id=$(vault read -format=json \
        identity/entity/name/"${entity_name}" | \
        jq -r '.data.id')
    echo "    entity_id  : ${entity_id}"

    # 4. Create the entity alias (issuer= lets Vault synthesize mount_accessor)
    #    Vault deduplicates aliases by (canonical_id + mount_accessor + name),
    #    so writing the same alias twice is idempotent.
    vault write identity/entity-alias \
        name="${subject}" \
        canonical_id="${entity_id}" \
        issuer="${ISSUER}" \
        external_id="${subject}" \
        &>/dev/null
    echo "    Alias created (sub → entity)."

    # 5. Register in the agent registry
    vault write agent-registry/register \
        display_name="${display_name}" \
        entity_id="${entity_id}" \
        ceiling_policies="${CEILING_POLICY_NAME}" \
        &>/dev/null
    echo "    Agent registry entry written."

    # 6. Validate: vault token lookup resolves the entity alias end-to-end
    echo "    Validating token with vault token lookup..."
    local lookup_entity_id
    lookup_entity_id=$(VAULT_TOKEN="${access_token}" vault token lookup -format=json \
        2>/dev/null | jq -r '.data.entity_id')

    if [[ "${lookup_entity_id}" == "${entity_id}" ]]; then
        echo "    ✔ Token resolves to entity_id ${entity_id}"
    else
        echo "WARNING: token lookup returned entity_id '${lookup_entity_id}'," >&2
        echo "         expected '${entity_id}'. Check alias and profile config." >&2
    fi
}

# ---------------------------------------------------------------------------
# Register each of the four Voyager service agents
#
#   args: <keycloak_client_id>   <vault_entity_name>            <registry_display_name>
# ---------------------------------------------------------------------------
_register_agent "voyager-read"          "voyager-read-agent"           "voyager-read-service-iam"
_register_agent "voyager-booking-write" "voyager-booking-write-agent"  "voyager-booking-write-service-iam"
_register_agent "voyager-payment-read"  "voyager-payment-read-agent"   "voyager-payment-read-service-iam"
_register_agent "voyager-payment-write" "voyager-payment-write-agent"  "voyager-payment-write-service-iam"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Vault agent registration complete!"
echo "============================================================"
echo " Realm   : ${ISSUER}"
echo " Ceiling : ${CEILING_POLICY_NAME}"
echo ""
echo " Registered agents:"
echo "   voyager-read-service-iam"
echo "   voyager-booking-write-service-iam"
echo "   voyager-payment-read-service-iam"
echo "   voyager-payment-write-service-iam"
echo ""
echo " Next step: ./start_app.sh"
echo "============================================================"
