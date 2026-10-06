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
#                        (e.g. "voyager-booking-read")
#   entity_name        — Vault Identity entity name
#                        (e.g. "voyager-booking-read-agent")
#   display_name       — label shown in the agent registry
#                        (e.g. "voyager-booking-read-service-iam")
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
    if ! client_secret=$(${KCADM} get \
            "clients/${kc_client_uuid}/client-secret" \
            -r "${REALM}" 2>&1 | jq -r '.value'); then
        echo "ERROR: Failed to retrieve client secret for '${kc_client_id}'." >&2
        return 1
    fi
    if [[ -z "${client_secret}" || "${client_secret}" == "null" ]]; then
        echo "ERROR: Client secret for '${kc_client_id}' is empty or null." >&2
        return 1
    fi

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
    access_token=$(echo "${token_response}" | jq -r '.access_token') || true
    subject=$(echo "${access_token}" | \
        jq -R 'split(".")[1] | @base64d | fromjson | .sub' -r) || true

    echo "    sub claim  : ${subject}"

    # 3. Create (or confirm) the Vault Identity entity
    if vault read -format=json identity/entity/name/"${entity_name}" &>/dev/null; then
        echo "    Entity '${entity_name}' already exists — skipping creation."
    else
        local entity_write_out
        if ! entity_write_out=$(vault write -format=json identity/entity \
                name="${entity_name}" \
                policies=default \
                external_id="${subject}" \
                2>&1); then
            echo "ERROR: Failed to create Vault entity '${entity_name}'." >&2
            echo "${entity_write_out}" >&2
            return 1
        fi
        echo "    Entity '${entity_name}' created."
    fi

    local entity_id entity_read_out
    if ! entity_read_out=$(vault read -format=json \
            identity/entity/name/"${entity_name}" 2>&1); then
        echo "ERROR: Failed to read entity '${entity_name}' after creation." >&2
        echo "${entity_read_out}" >&2
        return 1
    fi
    entity_id=$(echo "${entity_read_out}" | jq -r '.data.id') || true
    echo "    entity_id  : ${entity_id}"

    # 4. Create (or confirm) the entity alias
    #    Uses the Agentic AI shorthand: supply issuer= and external_id= so Vault
    #    synthesises the mount_accessor from the OAuth RS profile automatically —
    #    no separate auth/jwt mount or accessor lookup required.
    #      name=        human-readable alias label (need not match the sub)
    #      canonical_id= the entity UUID created above
    #      issuer=      must exactly match issuer_id in the OAuth RS profile
    #      external_id= the JWT sub claim; Vault uses this to match tokens
    #
    # IMPORTANT: the issuer= field is mandatory — omitting it lets the write
    #            succeed but causes silent auth failures at JWT lookup time.
    local entity_aliases_json alias_name="${entity_name}-alias"
    entity_aliases_json=$(vault read -format=json \
        identity/entity/name/"${entity_name}" 2>/dev/null \
        | jq -r '.data.aliases // []') || entity_aliases_json="[]"

    # Find any existing alias by name on this entity.
    local existing_alias_id existing_alias_external_id
    existing_alias_id=$(echo "${entity_aliases_json}" \
        | jq -r --arg n "${alias_name}" \
            '.[] | select(.name == $n) | .id' \
        | head -1) || existing_alias_id=""
    existing_alias_external_id=$(echo "${entity_aliases_json}" \
        | jq -r --arg n "${alias_name}" \
            '.[] | select(.name == $n) | .external_id // ""' \
        | head -1) || existing_alias_external_id=""

    if [[ "${existing_alias_external_id}" == "${subject}" ]]; then
        # Alias exists and is already wired to the correct sub — nothing to do.
        echo "    Entity alias already exists (id: ${existing_alias_id}) — skipping."
    else
        if [[ -n "${existing_alias_id}" ]]; then
            # Stale alias (wrong or missing external_id/issuer) — delete it first.
            echo "    Stale alias found (id: ${existing_alias_id}, external_id='${existing_alias_external_id}') — replacing."
            vault delete "identity/entity-alias/id/${existing_alias_id}" >/dev/null
        fi

        local alias_out alias_id
        if ! alias_out=$(vault write -format=json identity/entity-alias \
                name="${alias_name}" \
                canonical_id="${entity_id}" \
                issuer="${ISSUER}" \
                external_id="${subject}" \
                2>&1); then
            echo "ERROR: Failed to create entity alias for '${entity_name}'." >&2
            echo "${alias_out}" >&2
            return 1
        fi
        alias_id=$(echo "${alias_out}" | jq -r '.data.id') || true
        echo "    Entity alias created (id: ${alias_id})."
    fi

    # 5. Register in the agent registry
    local reg_out
    if ! reg_out=$(vault write -format=json agent-registry/register \
        display_name="${display_name}" \
        entity_id="${entity_id}" \
        ceiling_policies="${CEILING_POLICY_NAME}" \
        2>&1); then
        echo "ERROR: Failed to register agent '${display_name}' in agent-registry." >&2
        echo "${reg_out}" >&2
        return 1
    fi
    echo "    Agent registry entry written."

    # 6. Validate: vault token lookup resolves the JWT via the OAuth RS profile.
    #    Per the Agentic AI docs, when VAULT_TOKEN is a JWT Vault intercepts the
    #    lookup-self call, validates the JWT against the OAuth RS profile, resolves
    #    the entity alias created above, and returns the entity_id — same as a
    #    native Vault token lookup.  A 403 here means the alias is still missing
    #    or the issuer/external_id values don't match what the JWT contains.
    echo "    Validating token with vault token lookup..."
    local lookup_out lookup_entity_id
    lookup_out=$(VAULT_TOKEN="${access_token}" vault token lookup -format=json 2>&1) || true
    lookup_entity_id=$(echo "${lookup_out}" | jq -r '.data.entity_id // empty' 2>/dev/null) || true

    if [[ "${lookup_entity_id}" == "${entity_id}" ]]; then
        echo "    ✔ Token resolves to entity_id ${entity_id}"
    else
        echo "WARNING: token lookup returned entity_id '${lookup_entity_id}'," >&2
        echo "         expected '${entity_id}'. Check alias and profile config." >&2
        if [[ -n "${lookup_out}" ]]; then
            echo "         vault token lookup output: ${lookup_out}" >&2
        fi
    fi
}

# ---------------------------------------------------------------------------
# Register each of the four Voyager service agents
#
#   args: <keycloak_client_id>   <vault_entity_name>            <registry_display_name>
# ---------------------------------------------------------------------------
_register_agent "voyager-booking-read"  "voyager-booking-read-agent"   "voyager-booking-read-service-iam"
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
echo "   voyager-booking-read-service-iam"
echo "   voyager-booking-write-service-iam"
echo "   voyager-payment-read-service-iam"
echo "   voyager-payment-write-service-iam"
echo ""
echo " Next step: ./start_app.sh"
echo "============================================================"
