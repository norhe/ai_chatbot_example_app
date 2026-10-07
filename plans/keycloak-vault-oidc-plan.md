# Keycloak + Vault OIDC / JWT Auth Plan

## Overview

This plan adds an OIDC identity layer to the Voyager Travel project. Keycloak runs in Podman as
the OIDC / JWT provider. Each of the four agent microservices is registered as a dedicated
service-account client in Keycloak and uses the **OAuth2 client credentials flow** to obtain a
short-lived JWT. Vault is configured with the **JWT auth method** pointing at Keycloak's JWKS
endpoint; each service logs in to Vault by presenting its Keycloak JWT instead of a pre-generated
AppRole `secret_id`.

The existing AppRole path is preserved in full. A single environment variable
(`VAULT_AGENT_MODE=jwt`) switches each service between the legacy AppRole flow and the new
JWT/OIDC flow, making it easy to demonstrate and compare the two approaches side-by-side.

### What does NOT change

- `db.py` — credential fetching logic is unchanged (still driven by `VAULT_DB_ROLE`).
- `services/start_services.sh` / `stop_services.sh` — unchanged.
- The four Flask microservices' `/execute` and `/health` handlers.

### New / modified deliverables

| File | Change |
|---|---|
| `setup_keycloak.sh` | **New** — Start Keycloak in Podman; configure realm, clients, and service-account credentials via the Keycloak Admin REST API (`curl`; no GUI needed) |
| `setup_vault.sh` | **Modified** — Extend the existing script to also enable the JWT auth method, point it at Keycloak, and create four JWT roles reusing existing policies. All Vault config lives in one script. |
| `agents/vault_helper.py` | **Modified** — Add `vault_jwt_login(client_id, client_secret, vault_role)` alongside existing functions |
| `agents/*_service.py` (×4) | **Modified** — Add `VAULT_AGENT_MODE=jwt` branch in `_init_vault()` |
| `start_app.sh` | **Modified** — Export `VAULT_AGENT_MODE` and per-service OIDC client credentials when JWT mode is selected |

---

## Architecture

```
Keycloak (Podman, port 8080 — bound to 0.0.0.0)
  └─ Realm: voyager
       ├─ Client: voyager-read           (service account, client credentials flow)
       ├─ Client: voyager-booking-write  (service account, client credentials flow)
       ├─ Client: voyager-payment-read   (service account, client credentials flow)
       └─ Client: voyager-payment-write  (service account, client credentials flow)

Vault (dev mode, port 8200)
  ├─ auth/approle/  ← unchanged, AppRole path still works
  ├─ auth/jwt/
  │    ├─ jwks_url = http://<host>:8080/realms/voyager/protocol/openid-connect/certs
  │    ├─ role: voyager-read-jwt           bound_subject=service-account-voyager-read
  │    ├─ role: voyager-booking-write-jwt  bound_subject=service-account-voyager-booking-write
  │    ├─ role: voyager-payment-read-jwt   bound_subject=service-account-voyager-payment-read
  │    └─ role: voyager-payment-write-jwt  bound_subject=service-account-voyager-payment-write
  └─ database/  ← unchanged

Agent service startup (JWT mode):
  1. POST http://<keycloak>:8080/realms/voyager/protocol/openid-connect/token
         grant_type=client_credentials  client_id=<id>  client_secret=<secret>
     → Keycloak returns JWT
  2. vault write auth/jwt/login role=<role> jwt=<token>
     → Vault returns a Vault token bound to the existing DB creds policy
  3. Service stores Vault token; db.get_connection() works identically to AppRole mode
```

---

## Sub-Tasks

### Sub-Task 1 — `setup_keycloak.sh` (new file)

**Intent**
Provide a fully scriptable, idempotent shell script that:
1. Pulls and runs the Keycloak container in Podman (dev/start mode, no TLS required for local dev).
2. Binds the container port to `0.0.0.0:8080` so Vault and services can reach it from any interface.
3. Waits for the admin API to be ready.
4. Creates a `voyager` realm.
5. Creates four confidential OIDC clients with service accounts enabled (client credentials flow)
   using the Keycloak Admin REST API via `curl`.
6. Sets known, deterministic client secrets so `start_app.sh` can inject them as environment variables.
   No protocol mappers are needed — Keycloak automatically sets `sub` to `service-account-<clientId>`
   for service account tokens, which Vault uses directly as `bound_subject`.

**Expected Outcomes**
- `podman ps` shows a running `voyager-keycloak` container.
- `curl http://localhost:8080/realms/voyager/.well-known/openid-configuration` returns 200.
- `curl http://localhost:8080/realms/voyager/protocol/openid-connect/certs` returns a JWKS document.
- Each of the four clients can obtain a JWT via the `client_credentials` grant (verifiable with `curl`).

**Todo List**
1. Write Podman run command:
   - Image: `quay.io/keycloak/keycloak:latest`
   - Container name: `voyager-keycloak`
   - Port binding: `0.0.0.0:8080:8080` (all interfaces)
   - Environment: `KEYCLOAK_ADMIN=admin`, `KEYCLOAK_ADMIN_PASSWORD=admin`
   - Command: `start-dev` (embedded H2, no TLS needed for local dev)
   - Keycloak's official image already runs as UID 1000 — no extra `--user` flag needed
2. Write a `wait_for_keycloak` poll loop that retries `GET /realms/master` with `curl` until HTTP 200.
3. Obtain an admin bearer token via `curl`:
   `POST /realms/master/protocol/openid-connect/token`
   with `grant_type=password`, `client_id=admin-cli`, `username=admin`, `password=admin`.
4. Create the `voyager` realm via Admin REST API (`POST /admin/realms`).
   Handle idempotency: treat HTTP 409 as "already exists, continue".
5. For each of the four clients (`voyager-read`, `voyager-booking-write`,
   `voyager-payment-read`, `voyager-payment-write`):
   a. Create the client (`POST /admin/realms/voyager/clients`) with:
      - `clientAuthenticatorType: client-secret`
      - `serviceAccountsEnabled: true`
      - `standardFlowEnabled: false`
      - `directAccessGrantsEnabled: false`
   b. Set a deterministic client secret (`PUT /admin/realms/voyager/clients/{id}/client-secret`)
      using values like `voyager-read-secret`, `voyager-booking-write-secret`, etc.
   No mapper step — Keycloak's default service-account `sub` format is used as-is.
6. Print a summary of realm, client IDs, and secrets at the end for operator reference.

**Relevant Context**
- Keycloak Admin REST API base: `http://localhost:8080/admin/realms`
- Keycloak automatically sets `sub` to `service-account-<clientId>` for service account tokens
  (e.g. `service-account-voyager-read`). No mapper needed — this is the value Vault matches.
- `KEYCLOAK_BASE_URL` env var (default `http://localhost:8080`) makes the script host-agnostic.

**Status** `[ ] pending`

---

### Sub-Task 2 — Extend `setup_vault.sh` with JWT auth method config

**Intent**
Append JWT auth method configuration to the **existing** `setup_vault.sh` so all Vault setup
lives in one script. The existing AppRole + database secrets engine sections are untouched.
The new section, which runs after the existing AppRole section:
1. Enables the `jwt` auth method.
2. Points it at Keycloak's JWKS endpoint.
3. Creates four Vault JWT roles — one per service — that reuse the four policies already written
   by the existing AppRole section (`voyager-read-policy`, etc.).

No new Vault policies are needed. The JWT roles share exactly the same policies as the AppRole
roles, ensuring both auth paths grant identical database access.

**Expected Outcomes**
- Running `setup_vault.sh` once configures both AppRole and JWT auth methods.
- `vault auth list` shows both `approle/` and `jwt/`.
- `vault read auth/jwt/config` returns `jwks_url` pointing at Keycloak.
- Four Vault JWT roles exist under `auth/jwt/role/` (one per service).
- A test JWT from Keycloak (`voyager-read` client) can log in:
  `vault write auth/jwt/login role=voyager-read-jwt jwt=<token>` returns a valid Vault token.
- The existing AppRole login path still works without change.

**Todo List**
1. Add a new section header comment in `setup_vault.sh`: `# === JWT Auth Method (Keycloak) ===`.
2. Read `KEYCLOAK_BASE_URL` with a default of `http://localhost:8080`.
3. Enable JWT auth method (idempotent — use `|| true`):
   ```bash
   vault auth enable jwt || true
   ```
4. Configure the JWT auth method backend:
   ```bash
   vault write auth/jwt/config \
     jwks_url="${KEYCLOAK_BASE_URL}/realms/voyager/protocol/openid-connect/certs" \
     bound_issuer="${KEYCLOAK_BASE_URL}/realms/voyager"
   ```
5. For each of the four services, write a Vault JWT role:
   - `role_type=jwt`
   - `user_claim=sub`
   - `bound_subject=service-account-<clientId>` (Keycloak's default service-account format)
     e.g. `service-account-voyager-read`, `service-account-voyager-booking-write`, etc.
   - `bound_audiences=account` (default Keycloak audience on access tokens)
   - `policies=<existing-policy-name>` (same policy the AppRole already uses)
   - `ttl=1h`, `max_ttl=24h`
6. Print verification hints at the end of the script.

**Relevant Context**
- [`setup_vault.sh`](setup_vault.sh) — existing file; append after the AppRole section.
- Existing policy names: `voyager-read-policy`, `voyager-booking-write-policy`,
  `voyager-payment-read-policy`, `voyager-payment-write-policy`.
- `bound_subject` values are Keycloak's automatic `service-account-<clientId>` strings —
  no mapper in `setup_keycloak.sh` is required for this to work.

**Status** `[ ] pending`

---

### Sub-Task 3 — `agents/vault_helper.py` additions

**Intent**
Add `vault_jwt_login(client_id, client_secret, vault_role)` as a new public function in
[`agents/vault_helper.py`](agents/vault_helper.py) without modifying or removing the existing
`vault_approle_login` or `start_token_renewal` functions. The new function:
1. Calls Keycloak's token endpoint using `client_credentials` grant to obtain a JWT.
2. Presents that JWT to Vault's `auth/jwt/login` endpoint via `hvac`.
3. Returns an authenticated `hvac.Client` — identical interface to `vault_approle_login`.

**Expected Outcomes**
- `vault_jwt_login` is importable from `agents.vault_helper`.
- Calling it with valid credentials returns a client whose `.is_authenticated()` returns `True`.
- Calling `vault_approle_login` still works identically (no regression).

**Todo List**
1. Add a new function `vault_jwt_login(client_id: str, client_secret: str, vault_role: str) -> hvac.Client`:
   a. Read `VAULT_ADDR` from environment and `KEYCLOAK_BASE_URL` (default `http://localhost:8080`).
   b. `POST {KEYCLOAK_BASE_URL}/realms/voyager/protocol/openid-connect/token`
      with form body: `grant_type=client_credentials`, `client_id=<id>`, `client_secret=<secret>`.
      Use `requests` (already a project dependency).
   c. Extract `access_token` from the JSON response.
   d. Call `hvac.Client(url=vault_addr).auth.jwt.jwt_login(role=vault_role, jwt=access_token)`.
   e. Set `client.token` from `result["auth"]["client_token"]` and return the client.
2. Log structured events without logging the JWT value or client secret:
   - Before the Keycloak call: `vault_jwt_login attempt` with `client_id` and `vault_role`
   - On success: `vault_jwt_login success`
   - On failure: `vault_jwt_login failed` with the exception message
3. Confirm `requests` is already imported at the top of the file before adding another import.

**Relevant Context**
- [`agents/vault_helper.py`](agents/vault_helper.py) — existing file; add the new function after `start_token_renewal`.
- `hvac.Client.auth.jwt.jwt_login` is available in `hvac>=2.0.0`; project uses `hvac>=2.3.0`.
- `requests==2.32.3` is already in [`pyproject.toml`](pyproject.toml).

**Status** `[ ] pending`

---

### Sub-Task 4 — Service file `_init_vault()` updates (×4)

**Intent**
Each of the four agent services reads `VAULT_AGENT_MODE` at startup. When the value is `jwt`, it
calls `vault_jwt_login` instead of `vault_approle_login`. All other service behaviour — token
renewal, setting `VAULT_TOKEN` and `VAULT_DB_ROLE`, Flask startup — is identical in both modes.

**Expected Outcomes**
- `VAULT_AGENT_MODE=jwt` with `VAULT_CLIENT_ID`, `VAULT_CLIENT_SECRET`, `VAULT_JWT_ROLE` set
  causes the service to authenticate via Keycloak JWT.
- Unsetting `VAULT_AGENT_MODE` (or any value other than `jwt`) preserves the existing AppRole
  behaviour exactly.
- Both modes set `VAULT_TOKEN`, set `VAULT_DB_ROLE`, and start the `start_token_renewal` daemon.

**Todo List**
Apply the following changes to each of:
`agents/read_service.py`, `agents/booking_mutation_service.py`,
`agents/payment_read_service.py`, `agents/payment_write_service.py`

1. Add `vault_jwt_login` to the existing import from `agents.vault_helper`.
2. In `_init_vault()`, replace the direct call to `vault_approle_login` with a branch:
   ```python
   mode = os.environ.get("VAULT_AGENT_MODE", "approle")
   if mode == "jwt":
       client_id     = os.environ.get("VAULT_CLIENT_ID")
       client_secret = os.environ.get("VAULT_CLIENT_SECRET")
       vault_role    = os.environ.get("VAULT_JWT_ROLE")
       if not (client_id and client_secret and vault_role):
           logger.warning("VAULT_AGENT_MODE=jwt but VAULT_CLIENT_ID/SECRET/JWT_ROLE not set; skipping Vault")
           return
       client = vault_jwt_login(client_id, client_secret, vault_role)
   else:
       role_id   = os.environ.get("VAULT_ROLE_ID")
       secret_id = os.environ.get("VAULT_SECRET_ID")
       if not (role_id and secret_id and os.environ.get("VAULT_ADDR")):
           logger.warning(...)  # existing warning, unchanged
           return
       client = vault_approle_login(role_id, secret_id)
   ```
3. The lines after the if/else (setting `os.environ["VAULT_TOKEN"]`, `os.environ["VAULT_DB_ROLE"]`,
   calling `start_token_renewal`) remain unchanged and apply to both paths.

**Relevant Context**
- [`agents/read_service.py`](agents/read_service.py) — sets `VAULT_DB_ROLE = "voyager_read"`
- [`agents/booking_mutation_service.py`](agents/booking_mutation_service.py) — `VAULT_DB_ROLE = "voyager_booking_write"`
- [`agents/payment_read_service.py`](agents/payment_read_service.py) — `VAULT_DB_ROLE = "voyager_payment_read"`
- [`agents/payment_write_service.py`](agents/payment_write_service.py) — `VAULT_DB_ROLE = "voyager_payment_write"`

**Status** `[ ] pending`

---

### Sub-Task 5 — `start_app.sh` JWT mode support

**Intent**
Add a `VAULT_AGENT_MODE=jwt` launch path to `start_app.sh` that exports per-service OIDC
client credentials (`VAULT_CLIENT_ID`, `VAULT_CLIENT_SECRET`, `VAULT_JWT_ROLE`) into each
subprocess environment instead of sourcing AppRole `.env` files. The existing AppRole launch
path is preserved unchanged.

**Expected Outcomes**
- `VAULT_AGENT_MODE=jwt ./start_app.sh` starts all four services in JWT mode with no `.env`
  files on disk.
- `./start_app.sh` (without `VAULT_AGENT_MODE`) preserves existing AppRole behaviour exactly.

**Todo List**
1. Near the top of `start_app.sh`, read `VAULT_AGENT_MODE` (default empty → AppRole path).
2. Add a comment block documenting the JWT mode env vars:
   `VAULT_AGENT_MODE`, `KEYCLOAK_BASE_URL`, and the per-service triplets
   `VAULT_CLIENT_ID_<SERVICE>`, `VAULT_CLIENT_SECRET_<SERVICE>`, `VAULT_JWT_ROLE_<SERVICE>`.
3. In the per-service launch section, add a branch:
   - **JWT mode** (`VAULT_AGENT_MODE=jwt`): export `VAULT_CLIENT_ID`, `VAULT_CLIENT_SECRET`,
     `VAULT_JWT_ROLE` from service-specific env vars (e.g. `VAULT_CLIENT_ID_READ`,
     `VAULT_CLIENT_SECRET_READ`, `VAULT_JWT_ROLE_READ`) and export `VAULT_AGENT_MODE=jwt`.
     No `.env` file is sourced.
   - **AppRole mode** (default): source `vault_approle_creds/<service>.env` as before.
4. Export `KEYCLOAK_BASE_URL` (with default `http://localhost:8080`) to every subprocess.

**Relevant Context**
- [`start_app.sh`](start_app.sh) — existing file; dev defaults from `setup_keycloak.sh`:
  `voyager-read-secret`, `voyager-booking-write-secret`, etc.
- No new files are generated in JWT mode — credentials come from the operator's environment.

**Status** `[ ] pending`

---

## Environment Variables Summary

### JWT mode (new)

| Variable | Set by | Description |
|---|---|---|
| `VAULT_AGENT_MODE=jwt` | Operator | Activates JWT path in all services |
| `KEYCLOAK_BASE_URL` | Operator (default `http://localhost:8080`) | Keycloak base URL |
| `VAULT_CLIENT_ID` | `start_app.sh` (per service) | Keycloak client ID for this service |
| `VAULT_CLIENT_SECRET` | `start_app.sh` (per service) | Keycloak client secret for this service |
| `VAULT_JWT_ROLE` | `start_app.sh` (per service) | Vault JWT role name to log in with |

### AppRole mode (unchanged)

| Variable | Set by | Description |
|---|---|---|
| `VAULT_ROLE_ID` | `vault_approle_creds/<svc>.env` | AppRole role ID |
| `VAULT_SECRET_ID` | `vault_approle_creds/<svc>.env` | AppRole secret ID |
| `VAULT_ADDR` | Operator | Vault server address |

---

## Script Execution Order

```
1. vault server -dev           # Vault — separate terminal
2. ./setup_keycloak.sh         # Start Keycloak container + configure realm/clients
3. ./setup_vault.sh            # Configure Vault: DB engine + AppRole + JWT auth method
4a. ./start_app.sh             # Launch services in AppRole mode (default)
  OR
4b. VAULT_AGENT_MODE=jwt \
    VAULT_CLIENT_ID_READ=voyager-read \
    VAULT_CLIENT_SECRET_READ=voyager-read-secret \
    ... ./start_app.sh         # Launch services in JWT mode
```

---

## Status

| Sub-task | Status |
|---|---|
| Sub-Task 1 — `setup_keycloak.sh` (new) | ⬜ Pending |
| Sub-Task 2 — Extend `setup_vault.sh` with JWT config | ⬜ Pending |
| Sub-Task 3 — `vault_helper.py` additions | ⬜ Pending |
| Sub-Task 4 — Service file updates (×4) | ⬜ Pending |
| Sub-Task 5 — `start_app.sh` JWT mode | ⬜ Pending |
