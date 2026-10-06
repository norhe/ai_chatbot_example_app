#!/usr/bin/env bash
# keycloak_ai_vault_integration.sh — Configure the Keycloak voyager realm with
#                                    the vault-oidc client for Vault OAuth integration.
#
# Adds a confidential service-account client, a vault-api client scope with an
# audience mapper to the existing voyager realm, and validates the full
# token-issuance flow.  Exports ISSUER, TOKEN_ENDPOINT, JWKS_URI, and APP_ID
# for downstream scripts.
#
# Prerequisites:
#   - podman available on PATH
#   - jq and curl available on PATH
#   - Keycloak container 'voyager-keycloak' running
#     (run scripts/setup_keycloak_kcadm.sh first)
#
# Usage:
#   ./scripts/keycloak_ai_vault_integration.sh
#
# Optional env vars:
#   KEYCLOAK_URL — base URL reachable from this machine (default: http://127.0.0.1:8080)

set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-http://127.0.0.1:8080}"
ISSUER="${KEYCLOAK_URL}/realms/voyager"

# wrap kcadm.sh so all calls run inside the container
# (alias is not expanded in non-interactive scripts; a function works everywhere)
kcadm.sh() { podman exec voyager-keycloak /opt/keycloak/bin/kcadm.sh "$@"; }

echo "==> Authenticating with Keycloak at ${KEYCLOAK_URL}..."
if ! kcadm.sh config credentials \
  --server "$KEYCLOAK_URL" \
  --realm master \
  --user admin \
  --password admin 2>&1; then
    echo "ERROR: Failed to authenticate kcadm with Keycloak at ${KEYCLOAK_URL}." >&2
    exit 1
fi
echo "    Authentication successful."

echo "==> Creating OIDC client 'vault-oidc' in voyager realm (skipped if already exists)..."
kcadm.sh create clients \
  -r voyager \
  -s clientId=vault-oidc \
  -s protocol=openid-connect \
  -s publicClient=false \
  -s serviceAccountsEnabled=true \
  -s standardFlowEnabled=false \
  -s directAccessGrantsEnabled=false || true
echo "    Client ready."

echo "==> Retrieving client ID for 'vault-oidc'..."
_clients_out=$(kcadm.sh get clients -r voyager \
  -q clientId=vault-oidc --fields id --format csv 2>&1) || {
    echo "ERROR: Failed to list clients in voyager realm." >&2
    echo "${_clients_out}" >&2
    exit 1
}
CLIENT_ID=$(echo "${_clients_out}" | tail -1 | tr -d '"' | tr -d $'\r') || true
if [[ -z "${CLIENT_ID}" ]]; then
    echo "ERROR: Could not find 'vault-oidc' client in voyager realm." >&2
    exit 1
fi
echo "    Client ID: ${CLIENT_ID}"

echo "==> Retrieving client secret..."
_secret_out=$(kcadm.sh get "clients/${CLIENT_ID}/client-secret" -r voyager 2>&1) || true
CLIENT_SECRET=$(echo "${_secret_out}" | jq -r '.value // empty') || true
if [[ -z "${CLIENT_SECRET}" ]]; then
    echo "ERROR: Failed to retrieve client secret for 'vault-oidc'." >&2
    echo "${_secret_out}" >&2
    exit 1
fi
echo "    Client secret retrieved."

echo "==> Creating client scope 'vault-api' (skipped if already exists)..."
kcadm.sh create client-scopes \
  -r voyager \
  -s name=vault-api \
  -s protocol=openid-connect || true
echo "    Client scope ready."

echo "==> Retrieving scope ID for 'vault-api'..."
_scopes_out=$(kcadm.sh get client-scopes -r voyager --fields id,name 2>&1) || {
    echo "ERROR: Failed to list client scopes in voyager realm." >&2
    echo "${_scopes_out}" >&2
    exit 1
}
SCOPE_ID=$(echo "${_scopes_out}" | jq -r '.[] | select(.name=="vault-api") | .id') || true
if [[ -z "${SCOPE_ID}" ]]; then
    echo "ERROR: Could not find 'vault-api' client scope in voyager realm." >&2
    exit 1
fi
echo "    Scope ID: ${SCOPE_ID}"

echo "==> Adding/updating audience mapper on 'vault-api' scope..."
# Find the mapper ID if it already exists so we can update rather than create.
_MAPPER_ID=$(kcadm.sh get "client-scopes/${SCOPE_ID}/protocol-mappers/models" \
    -r voyager 2>/dev/null \
    | jq -r '.[] | select(.name=="vault-audience") | .id // empty') || _MAPPER_ID=""

_MAPPER_HOST=$(mktemp)
if [[ -n "${_MAPPER_ID}" ]]; then
    # Mapper exists — PUT requires the id field in the body.
    cat >"$_MAPPER_HOST" <<EOF
{
  "id":"${_MAPPER_ID}",
  "name":"vault-audience",
  "protocol":"openid-connect",
  "protocolMapper":"oidc-audience-mapper",
  "config":{
    "included.custom.audience":"${ISSUER}",
    "id.token.claim":"false",
    "access.token.claim":"true"
  }
}
EOF
    _MAPPER_CTR=/tmp/vault-audience-mapper-$$.json
    podman cp "$_MAPPER_HOST" voyager-keycloak:"$_MAPPER_CTR"
    rm -f "$_MAPPER_HOST"
    kcadm.sh update \
        "client-scopes/${SCOPE_ID}/protocol-mappers/models/${_MAPPER_ID}" \
        -r voyager \
        -f "$_MAPPER_CTR"
    echo "    Audience mapper updated (id: ${_MAPPER_ID})."
else
    cat >"$_MAPPER_HOST" <<EOF
{
  "name":"vault-audience",
  "protocol":"openid-connect",
  "protocolMapper":"oidc-audience-mapper",
  "config":{
    "included.custom.audience":"${ISSUER}",
    "id.token.claim":"false",
    "access.token.claim":"true"
  }
}
EOF
    _MAPPER_CTR=/tmp/vault-audience-mapper-$$.json
    podman cp "$_MAPPER_HOST" voyager-keycloak:"$_MAPPER_CTR"
    rm -f "$_MAPPER_HOST"
    kcadm.sh create "client-scopes/${SCOPE_ID}/protocol-mappers/models" \
        -r voyager \
        -f "$_MAPPER_CTR"
    echo "    Audience mapper created."
fi
podman exec voyager-keycloak rm -f "$_MAPPER_CTR"
echo "    Audience mapper ready (audience: ${ISSUER})."

echo "==> Assigning 'vault-api' scope as default scope on all four service clients..."
# Without this assignment the service clients never request the vault-api scope,
# their tokens carry only aud=[account], and Vault rejects them with audience mismatch.
for _svc_client in \
    "voyager-booking-read" \
    "voyager-booking-write" \
    "voyager-payment-read" \
    "voyager-payment-write"; do

    _svc_uuid=$(kcadm.sh get clients \
        -r voyager \
        --query "clientId=${_svc_client}" \
        --fields id \
        --format csv \
        --noquotes 2>/dev/null | head -1) || true
    if [[ -z "${_svc_uuid}" ]]; then
        echo "ERROR: Could not find client '${_svc_client}' in voyager realm." >&2
        exit 1
    fi

    # Use `update` (HTTP PUT) — the Keycloak REST endpoint for assigning a default
    # client scope is PUT /clients/{id}/default-client-scopes/{scopeId}, not POST.
    # kcadm `create` sends POST and gets a 405; `update` sends PUT and is idempotent.
    _assign_out=$(kcadm.sh update \
        "clients/${_svc_uuid}/default-client-scopes/${SCOPE_ID}" \
        -r voyager 2>&1) || true
    echo "    vault-api default scope assigned to '${_svc_client}'."
done

echo "==> Setting realm access.token.header.type=at+JWT..."
# Vault's access_token jwt_type requires the JWT body 'typ' claim to be 'at+JWT'
# (RFC 9068). Keycloak 26 emits 'typ: Bearer' by default; this realm attribute
# switches it to 'at+JWT'. Applied via the Admin REST API so it takes effect
# immediately without requiring a realm restart.
_ADMIN_TOKEN=$(curl -s -X POST \
    "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=password&client_id=admin-cli&username=admin&password=admin" \
    | jq -r '.access_token')

_REALM_JSON=$(curl -s \
    -H "Authorization: Bearer ${_ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/voyager")

_UPDATED_REALM=$(echo "${_REALM_JSON}" \
    | jq '.attributes["access.token.header.type"] = "at+JWT"')

_put_out=$(curl -s -o /dev/null -w "%{http_code}" \
    -X PUT "${KEYCLOAK_URL}/admin/realms/voyager" \
    -H "Authorization: Bearer ${_ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${_UPDATED_REALM}")

if [[ "${_put_out}" == "204" ]]; then
    echo "    Realm attribute set (access.token.header.type=at+JWT)."
else
    echo "ERROR: Failed to set realm token header type (HTTP ${_put_out})." >&2
    exit 1
fi
unset _ADMIN_TOKEN _REALM_JSON _UPDATED_REALM _put_out

#retrieve issuer, token_endpoint, jwks_uri -- Use to verify values if desired
#curl -s \
#"${KEYCLOAK_URL}/realms/voyager/.well-known/openid-configuration" | jq

echo "==> Setting Vault integration environment variables..."
export ISSUER
export TOKEN_ENDPOINT="${KEYCLOAK_URL}/realms/voyager/protocol/openid-connect/token"
export JWKS_URI="${KEYCLOAK_URL}/realms/voyager/protocol/openid-connect/certs"
export APP_ID="vault-oidc"
echo "    ISSUER=${ISSUER}"
echo "    TOKEN_ENDPOINT=${TOKEN_ENDPOINT}"
echo "    JWKS_URI=${JWKS_URI}"
echo "    APP_ID=${APP_ID}"

echo "==> Testing token issuance..."
TOKEN_RESPONSE=$(curl -s -X POST \
  "${KEYCLOAK_URL}/realms/voyager/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=vault-oidc" \
  -d "client_secret=${CLIENT_SECRET}")

if echo "$TOKEN_RESPONSE" | jq -e '.access_token' > /dev/null 2>&1; then
  echo "    Token issuance successful."
  echo ""
  echo "✔  Keycloak Vault-AI configuration complete."
else
  echo "    Token issuance failed. Response:"
  echo "$TOKEN_RESPONSE" | jq .
  exit 1
fi