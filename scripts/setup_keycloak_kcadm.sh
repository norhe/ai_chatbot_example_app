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
    _realm_out=""
    if ! _realm_out=$(${KCADM} create realms \
            -s realm=voyager \
            -s enabled=true 2>&1); then
        echo "ERROR: Failed to create 'voyager' realm in Keycloak." >&2
        echo "${_realm_out}" >&2
        exit 1
    fi
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
    local existing _existing_raw
    _existing_raw=$(${KCADM} get clients \
        --target-realm voyager \
        --query "clientId=${client_id}" \
        --fields id \
        --format csv \
        --noquotes 2>/dev/null) || true
    existing=$(echo "${_existing_raw}" | head -1) || true

    local client_uuid
    if [[ -n "${existing}" ]]; then
        echo "    Client '${client_id}' already exists — updating secret only."
        client_uuid="${existing}"
    else
        local _create_out
        if ! _create_out=$(${KCADM} create clients \
                --target-realm voyager \
                -s "clientId=${client_id}" \
                -s enabled=true \
                -s clientAuthenticatorType=client-secret \
                -s serviceAccountsEnabled=true \
                -s standardFlowEnabled=false \
                -s directAccessGrantsEnabled=false \
                -s publicClient=false \
                -s 'attributes."access.token.signed.response.alg"=RS256' \
                -s 'attributes."access.token.header.type"=at+JWT' 2>&1); then
            echo "ERROR: Failed to create Keycloak client '${client_id}'." >&2
            echo "${_create_out}" >&2
            return 1
        fi
        echo "    Client '${client_id}' created."

        local _uuid_raw
        _uuid_raw=$(${KCADM} get clients \
            --target-realm voyager \
            --query "clientId=${client_id}" \
            --fields id \
            --format csv \
            --noquotes 2>/dev/null) || true
        client_uuid=$(echo "${_uuid_raw}" | head -1) || true
        if [[ -z "${client_uuid}" ]]; then
            echo "ERROR: Client '${client_id}' was created but UUID could not be retrieved." >&2
            return 1
        fi
    fi

    # Set deterministic client secret
    local _secret_out
    if ! _secret_out=$(${KCADM} update "clients/${client_uuid}" \
            --target-realm voyager \
            -s "secret=${client_secret}" 2>&1); then
        echo "ERROR: Failed to set client secret for '${client_id}'." >&2
        echo "${_secret_out}" >&2
        return 1
    fi

    echo "    Client secret set for '${client_id}'."
}

_create_client "voyager-booking-read"  "voyager-booking-read-secret"
_create_client "voyager-booking-write" "voyager-booking-write-secret"
_create_client "voyager-payment-read"  "voyager-payment-read-secret"
_create_client "voyager-payment-write" "voyager-payment-write-secret"

# ---------------------------------------------------------------------------
# 6. Create the voyager-user public client (Authorization Code + PKCE)
# ---------------------------------------------------------------------------
echo "==> Configuring public client 'voyager-user'..."

_existing_user_client_raw=$(${KCADM} get clients \
    --target-realm voyager \
    --query "clientId=voyager-user" \
    --fields id \
    --format csv \
    --noquotes 2>/dev/null) || true
_existing_user_client=$(echo "${_existing_user_client_raw}" | head -1) || true

if [[ -n "${_existing_user_client}" ]]; then
    echo "    Client 'voyager-user' already exists — skipping."
else
    _user_client_out=""
    if ! _user_client_out=$(${KCADM} create clients \
            --target-realm voyager \
            -s "clientId=voyager-user" \
            -s enabled=true \
            -s publicClient=true \
            -s standardFlowEnabled=true \
            -s directAccessGrantsEnabled=false \
            -s serviceAccountsEnabled=false \
            -s 'redirectUris=["http://127.0.0.1:5000/*"]' \
            -s 'webOrigins=["http://127.0.0.1:5000"]' \
            -s 'attributes."pkce.code.challenge.method"=S256' \
            -s 'attributes."access.token.signed.response.alg"=RS256' \
            -s 'attributes."access.token.header.type"=at+JWT' 2>&1); then
        echo "ERROR: Failed to create 'voyager-user' client." >&2
        echo "${_user_client_out}" >&2
        exit 1
    fi
    echo "    Client 'voyager-user' created."
fi

# ---------------------------------------------------------------------------
# 7. Create realm roles (idempotent)
# ---------------------------------------------------------------------------
echo "==> Creating realm roles..."

_create_realm_role() {
    local role_name="$1"
    if ${KCADM} get roles --target-realm voyager --fields name --format csv --noquotes 2>/dev/null \
            | grep -qx "${role_name}"; then
        echo "    Role '${role_name}' already exists — skipping."
    else
        local _role_out=""
        if ! _role_out=$(${KCADM} create roles \
                --target-realm voyager \
                -s "name=${role_name}" 2>&1); then
            echo "ERROR: Failed to create role '${role_name}'." >&2
            echo "${_role_out}" >&2
            exit 1
        fi
        echo "    Role '${role_name}' created."
    fi
}

_create_realm_role "admin"
_create_realm_role "bookings_admin"
_create_realm_role "payments_admin"
_create_realm_role "user"

# ---------------------------------------------------------------------------
# 8. Create demo users and assign roles (idempotent)
# ---------------------------------------------------------------------------
echo "==> Creating demo users..."

# Usage: _create_demo_user <username> <first> <last> <role>
_create_demo_user() {
    local username="$1"
    local first="$2"
    local last="$3"
    local role="$4"

    local existing_id_raw existing_id
    existing_id_raw=$(${KCADM} get users \
        --target-realm voyager \
        --query "username=${username}" \
        --fields id \
        --format csv \
        --noquotes 2>/dev/null) || true
    existing_id=$(echo "${existing_id_raw}" | head -1) || true

    if [[ -n "${existing_id}" ]]; then
        echo "    User '${username}' already exists — skipping creation."
    else
        local _user_out=""
        if ! _user_out=$(${KCADM} create users \
                --target-realm voyager \
                -s "username=${username}" \
                -s "firstName=${first}" \
                -s "lastName=${last}" \
                -s enabled=true 2>&1); then
            echo "ERROR: Failed to create user '${username}'." >&2
            echo "${_user_out}" >&2
            exit 1
        fi
        echo "    User '${username}' created."

        # Fetch the new user UUID
        existing_id_raw=$(${KCADM} get users \
            --target-realm voyager \
            --query "username=${username}" \
            --fields id \
            --format csv \
            --noquotes 2>/dev/null) || true
        existing_id=$(echo "${existing_id_raw}" | head -1) || true

        # Set password (non-temporary)
        local _pass_out=""
        if ! _pass_out=$(${KCADM} set-password \
                --target-realm voyager \
                --username "${username}" \
                --new-password "voyager123" 2>&1); then
            echo "ERROR: Failed to set password for '${username}'." >&2
            echo "${_pass_out}" >&2
            exit 1
        fi
        echo "    Password set for '${username}'."
    fi

    # Assign role (idempotent via add-roles — Keycloak ignores duplicates)
    local _role_out=""
    if ! _role_out=$(${KCADM} add-roles \
            --target-realm voyager \
            --uusername "${username}" \
            --rolename "${role}" 2>&1); then
        echo "WARNING: Failed to assign role '${role}' to '${username}': ${_role_out}" >&2
    else
        echo "    Role '${role}' assigned to '${username}'."
    fi
}

_create_demo_user "alice"         "Alice"    "Johnson"  "user"
_create_demo_user "bob"           "Bob"      "Martinez" "user"
_create_demo_user "carol"         "Carol"    "Williams" "user"
_create_demo_user "david"         "David"    "Lee"      "user"
_create_demo_user "bookings_mgr"  "Bookings" "Manager"  "bookings_admin"
_create_demo_user "payments_mgr"  "Payments" "Manager"  "payments_admin"
_create_demo_user "admin_user"    "Admin"    "User"     "admin"

# ---------------------------------------------------------------------------
# 9. Export demo user sub (UUID) values to scripts/demo_user_subs.env
# ---------------------------------------------------------------------------
echo "==> Exporting demo user sub UUIDs to scripts/demo_user_subs.env..."

SUBS_FILE="$(dirname "$0")/demo_user_subs.env"

{
    echo "# Auto-generated by setup_keycloak_kcadm.sh — do not commit"
    for _username in alice bob carol david bookings_mgr payments_mgr admin_user; do
        _sub_raw=$(${KCADM} get users \
            --target-realm voyager \
            --query "username=${_username}" \
            --fields id \
            --format csv \
            --noquotes 2>/dev/null) || true
        _sub=$(echo "${_sub_raw}" | head -1) || true
        _env_key="${_username^^}_SUB"
        _env_key="${_env_key//-/_}"
        echo "${_env_key}=${_sub}"
    done
} > "${SUBS_FILE}"
echo "    Written: ${SUBS_FILE}"

# ---------------------------------------------------------------------------
# 10. Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Keycloak setup complete!"
echo "============================================================"
echo " Realm             : voyager"
echo " OIDC discovery    : ${KEYCLOAK_BASE_URL}/realms/voyager/.well-known/openid-configuration"
echo " JWKS endpoint     : ${KEYCLOAK_BASE_URL}/realms/voyager/protocol/openid-connect/certs"
echo ""
echo " Service clients:"
echo "   voyager-booking-read   : voyager-booking-read-secret"
echo "   voyager-booking-write  : voyager-booking-write-secret"
echo "   voyager-payment-read   : voyager-payment-read-secret"
echo "   voyager-payment-write  : voyager-payment-write-secret"
echo ""
echo " User-facing client: voyager-user (public, PKCE)"
echo "   Redirect URI: http://127.0.0.1:5000/*"
echo ""
echo " Realm roles: admin | bookings_admin | payments_admin | user"
echo ""
echo " Demo users (password: voyager123):"
echo "   alice, bob, carol, david  → user"
echo "   bookings_mgr              → bookings_admin"
echo "   payments_mgr              → payments_admin"
echo "   admin_user                → admin"
echo ""
echo " Demo user sub UUIDs written to: scripts/demo_user_subs.env"
echo ""
echo " Verify a token:"
echo "   curl -s -d 'grant_type=client_credentials' \\"
echo "        -d 'client_id=voyager-booking-read' \\"
echo "        -d 'client_secret=voyager-booking-read-secret' \\"
echo "        '${KEYCLOAK_BASE_URL}/realms/voyager/protocol/openid-connect/token' | python3 -m json.tool"
echo ""
echo " Next step: ./scripts/setup_vault.sh"
echo "============================================================"
