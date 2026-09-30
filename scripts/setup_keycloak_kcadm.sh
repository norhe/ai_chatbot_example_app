#!/usr/bin/env bash
# setup_keycloak_kcadm.sh — Start Keycloak in Podman and configure the voyager realm/clients
#                           using kcadm.sh from inside the running container.
#
# Idempotent: existing realm and clients are left unchanged.
#
# Prerequisites:
#   - podman available on PATH
#
# Usage:
#   ./scripts/setup_keycloak_kcadm.sh
#
# Optional env vars:
#   KEYCLOAK_BASE_URL  — base URL reachable from this machine (default: http://127.0.0.1:8080)
#   KEYCLOAK_ADMIN     — admin username (default: admin)
#   KEYCLOAK_ADMIN_PASSWORD — admin password; read from env, never hardcoded in source

set -euo pipefail

KEYCLOAK_BASE_URL="${KEYCLOAK_BASE_URL:-http://127.0.0.1:8080}"
CONTAINER_NAME="voyager-keycloak"
KEYCLOAK_IMAGE="quay.io/keycloak/keycloak"
KCADM="podman exec ${CONTAINER_NAME} /opt/keycloak/bin/kcadm.sh"

KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"

# ---------------------------------------------------------------------------
# Guard: verify podman is available and admin password is set
# ---------------------------------------------------------------------------
if ! command -v podman &>/dev/null; then
    echo "ERROR: 'podman' not found on PATH." >&2
    exit 1
fi

if [[ -z "${KEYCLOAK_ADMIN_PASSWORD}" ]]; then
    echo "ERROR: KEYCLOAK_ADMIN_PASSWORD environment variable is not set." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Start Keycloak container (idempotent)
# ---------------------------------------------------------------------------
echo "==> Starting Keycloak container '${CONTAINER_NAME}'..."

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
        -p 127.0.0.1:8080:8080 \
        -e KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN}" \
        -e KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD}" \
        "${KEYCLOAK_IMAGE}" \
        start-dev; then
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
# 2. Wait for Keycloak to be ready (poll via kcadm config credentials)
# ---------------------------------------------------------------------------
echo "==> Waiting for Keycloak to be ready at ${KEYCLOAK_BASE_URL}..."

_wait_for_keycloak() {
    local retries=30
    local interval=5
    local i=0
    until ${KCADM} config credentials \
            --server "${KEYCLOAK_BASE_URL}" \
            --realm master \
            --user "${KEYCLOAK_ADMIN}" \
            --password "${KEYCLOAK_ADMIN_PASSWORD}" \
            &>/dev/null; do
        i=$((i + 1))
        if [[ ${i} -ge ${retries} ]]; then
            echo "ERROR: Keycloak did not become ready after $((retries * interval))s." >&2
            exit 1
        fi
        echo "    Not ready yet — retrying in ${interval}s... (attempt ${i}/${retries})"
        sleep "${interval}"
    done
}
_wait_for_keycloak
echo "    Keycloak is ready."

# ---------------------------------------------------------------------------
# 3. Authenticate kcadm (session stored in container-local config)
# ---------------------------------------------------------------------------
echo "==> Authenticating kcadm..."

${KCADM} config credentials \
    --server "${KEYCLOAK_BASE_URL}" \
    --realm master \
    --user "${KEYCLOAK_ADMIN}" \
    --password "${KEYCLOAK_ADMIN_PASSWORD}"

echo "    kcadm authenticated."

# ---------------------------------------------------------------------------
# 4. Create the voyager realm (idempotent)
# ---------------------------------------------------------------------------
echo "==> Creating 'voyager' realm..."

if ${KCADM} get realms/voyager &>/dev/null; then
    echo "    Realm 'voyager' already exists — skipping."
else
    ${KCADM} create realms \
        -s realm=voyager \
        -s enabled=true
    echo "    Realm 'voyager' created."
fi

# ---------------------------------------------------------------------------
# 5. Create four service-account clients
# ---------------------------------------------------------------------------
_create_client() {
    local client_id="$1"
    local client_secret="$2"

    echo "==> Configuring client '${client_id}'..."

    # Check if the client already exists
    local existing
    existing=$(${KCADM} get clients \
        --target-realm voyager \
        --query "clientId=${client_id}" \
        --fields id \
        --format csv \
        --noquotes 2>/dev/null | head -1)

    local client_uuid
    if [[ -n "${existing}" ]]; then
        echo "    Client '${client_id}' already exists — updating secret only."
        client_uuid="${existing}"
    else
        ${KCADM} create clients \
            --target-realm voyager \
            -s "clientId=${client_id}" \
            -s enabled=true \
            -s clientAuthenticatorType=client-secret \
            -s serviceAccountsEnabled=true \
            -s standardFlowEnabled=false \
            -s directAccessGrantsEnabled=false \
            -s publicClient=false
        echo "    Client '${client_id}' created."

        client_uuid=$(${KCADM} get clients \
            --target-realm voyager \
            --query "clientId=${client_id}" \
            --fields id \
            --format csv \
            --noquotes 2>/dev/null | head -1)
    fi

    # Set deterministic client secret
    ${KCADM} update "clients/${client_uuid}" \
        --target-realm voyager \
        -s "secret=${client_secret}"

    echo "    Client secret set for '${client_id}'."
}

_create_client "voyager-read"          "voyager-read-secret"
_create_client "voyager-booking-write" "voyager-booking-write-secret"
_create_client "voyager-payment-read"  "voyager-payment-read-secret"
_create_client "voyager-payment-write" "voyager-payment-write-secret"

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Keycloak setup complete!"
echo "============================================================"
echo " Realm             : voyager"
echo " OIDC discovery    : ${KEYCLOAK_BASE_URL}/realms/voyager/.well-known/openid-configuration"
echo " JWKS endpoint     : ${KEYCLOAK_BASE_URL}/realms/voyager/protocol/openid-connect/certs"
echo ""
echo " Clients and secrets:"
echo "   voyager-read           : voyager-read-secret"
echo "   voyager-booking-write  : voyager-booking-write-secret"
echo "   voyager-payment-read   : voyager-payment-read-secret"
echo "   voyager-payment-write  : voyager-payment-write-secret"
echo ""
echo " Verify a token:"
echo "   curl -s -d 'grant_type=client_credentials' \\"
echo "        -d 'client_id=voyager-read' \\"
echo "        -d 'client_secret=voyager-read-secret' \\"
echo "        '${KEYCLOAK_BASE_URL}/realms/voyager/protocol/openid-connect/token' | python3 -m json.tool"
echo ""
echo " Next step: ./scripts/setup_vault.sh"
echo "============================================================"
