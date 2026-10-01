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
#   KEYCLOAK_URL — base URL reachable from this machine (default: http://localhost:8080)

set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
ISSUER="${KEYCLOAK_URL}/realms/voyager"

# wrap kcadm.sh so all calls run inside the container
# (alias is not expanded in non-interactive scripts; a function works everywhere)
kcadm.sh() { podman exec voyager-keycloak /opt/keycloak/bin/kcadm.sh "$@"; }

echo "==> Authenticating with Keycloak at ${KEYCLOAK_URL}..."
kcadm.sh config credentials \
  --server "$KEYCLOAK_URL" \
  --realm master \
  --user admin \
  --password admin
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
CLIENT_ID=$(kcadm.sh get clients -r voyager \
  -q clientId=vault-oidc --fields id --format csv | tail -1 | tr -d '"\r')
echo "    Client ID: ${CLIENT_ID}"

echo "==> Retrieving client secret..."
CLIENT_SECRET=$(kcadm.sh get clients/${CLIENT_ID}/client-secret -r voyager | jq -r .value)
echo "    Client secret retrieved."

echo "==> Creating client scope 'vault-api' (skipped if already exists)..."
kcadm.sh create client-scopes \
  -r voyager \
  -s name=vault-api \
  -s protocol=openid-connect || true
echo "    Client scope ready."

echo "==> Retrieving scope ID for 'vault-api'..."
SCOPE_ID=$(kcadm.sh get client-scopes \
  -r voyager --fields id,name | jq -r \
  '.[] | select(.name=="vault-api") | .id')
echo "    Scope ID: ${SCOPE_ID}"

echo "==> Adding audience mapper to scope (skipped if already exists)..."
_MAPPER_HOST=$(mktemp)
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
kcadm.sh create client-scopes/$SCOPE_ID/protocol-mappers/models \
  -r voyager \
  -f "$_MAPPER_CTR" || true
podman exec voyager-keycloak rm -f "$_MAPPER_CTR"
echo "    Audience mapper ready."

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