# HashiCorp Vault Integration Plan

## Top-Level Overview

Introduce HashiCorp Vault (dev-mode) as the secrets broker between the four Flask
microservices and PostgreSQL. Vault's **database secrets engine** issues short-lived,
ephemeral PostgreSQL credentials on demand; each microservice authenticates to Vault
using **AppRole** and receives a role-scoped credential set that carries only the
minimum permissions its tool type requires.

The work is broken into four sequential sub-tasks:

1. Write `setup_vault.sh` — one-shot idempotent script that configures a running
   dev-mode Vault server (database engine, roles, policies, AppRole mounts and role IDs).
2. Update `db.py` — replace static `DATABASE_URL` lookup with a Vault dynamic
   credential fetch using `hvac`; credentials are fetched on every `get_connection()`
   call and the Vault token is kept alive via `renew_self` up to its max TTL.
3. Create `agents/vault_helper.py` + update each `agents/*_service.py` — AppRole login
   at startup, background token renewal thread, `VAULT_TOKEN` + `VAULT_DB_ROLE` set in
   process env.
4. Write `start_app.sh` — sources per-service AppRole credential files and launches all
   five processes (orchestrator + 4 services); update `requirements.txt` and
   `.gitignore`.

**Out of scope:** TLS for Vault, persistent Vault storage (dev mode only), production
secret rotation scheduling, Docker Compose orchestration.

---

## Role ↔ Permission Matrix

| Vault Role              | Service                         | PostgreSQL Privileges                          |
|-------------------------|---------------------------------|------------------------------------------------|
| `voyager_read`          | read_service (5101)             | SELECT on bookings                             |
| `voyager_booking_write` | booking_mutation_service (5102) | SELECT, INSERT, UPDATE on bookings             |
| `voyager_payment_read`  | payment_read_service (5103)     | SELECT on payment_cards                        |
| `voyager_payment_write` | payment_write_service (5104)    | SELECT, INSERT, UPDATE, DELETE on payment_cards|

Each role maps to a dedicated Vault policy, AppRole `role_id`, and a PostgreSQL
creation statement that grants only the privileges listed above.

---

## Sub-Task 1 — `setup_vault.sh`

**Intent:** Provide a single executable script that configures a locally running Vault
dev server with everything needed for the demo. Running it a second time should be
safe (idempotent). No schema migration complexity — the existing `DATABASE_URL`
superuser connection is passed straight through to Vault's plugin config.

**Expected Outcomes:**
- Database secrets engine enabled at `database/`
- A `voyager-postgres` connection configured using the `DATABASE_URL` env var directly
  as the connection string (no URL parsing needed)
- Four Vault roles (`voyager_read`, `voyager_booking_write`, `voyager_payment_read`,
  `voyager_payment_write`) each with per-role creation SQL and TTLs
- Four Vault policies granting `["read"]` on the matching `database/creds/<role>` path
- AppRole auth method enabled; one AppRole per service; `role_id` and a one-time
  `secret_id` written to `vault_approle_creds/<service>.env` in `KEY=VALUE` format
- Script prints a clear success summary

**Todo List:**
1. Create `setup_vault.sh` with `#!/usr/bin/env bash` header and `set -euo pipefail`
2. Guard: verify `vault` CLI is on PATH and `VAULT_ADDR` / `VAULT_TOKEN` are set; print
   usage hint if not
3. Enable the `database` secrets engine at path `database/` (idempotent — use
   `vault secrets list` check or `-output-policy` trick to skip if already enabled)
4. Write the `voyager-postgres` connection config:
   - `plugin_name=postgresql-database-plugin`
   - `connection_url` set to `DATABASE_URL` env var value directly
   - `allowed_roles` lists all four role names
5. Write four `database/roles/<role>` entries with:
   - `db_name=voyager-postgres`
   - `default_ttl=1h`, `max_ttl=24h`
   - Per-role `creation_statements` using SQL `CREATE ROLE ... LOGIN PASSWORD ... VALID
     UNTIL ...` + `GRANT` statements from the permission matrix above
6. Write four Vault policies as inline here-docs — each grants `capabilities = ["read"]`
   on `database/creds/<role>`
7. Enable `approle` auth method (idempotent)
8. For each of the four roles:
   - Create the AppRole with `token_ttl=1h` and `token_max_ttl=24h` and bind it to the
     matching policy
   - Read the `role_id`
   - Generate a `secret_id`
   - Write both to `vault_approle_creds/<service_name>.env` as:
     ```
     VAULT_ROLE_ID=<role_id>
     VAULT_SECRET_ID=<secret_id>
     ```
9. Create the `vault_approle_creds/` directory with mode `700` before writing files;
   write each `.env` with mode `600`
10. Print a success summary showing each credential file path

**Relevant Context:**
- `DATABASE_URL` format already in use: `postgresql://user:pass@host:5432/dbname`
- Vault CLI: `vault secrets enable -path=database database`, `vault write database/config/...`,
  `vault write database/roles/...`, `vault policy write`, `vault auth enable approle`,
  `vault write auth/approle/role/...`, `vault read -field=role_id auth/approle/role/.../role-id`,
  `vault write -field=secret_id -f auth/approle/role/.../secret-id`
- `vault_approle_creds/` must be gitignored (handled in Sub-Task 4)

**Status:** [ ] pending

---

## Sub-Task 2 — Update `db.py` for Vault dynamic credentials

**Intent:** Replace the static `DATABASE_URL` lookup in `get_connection()` with a Vault
dynamic credential fetch. Credentials are fetched fresh on every `get_connection()` call
(short-lived, ephemeral creds — no caching). The Vault token used is kept alive by a
background renewal loop (see Sub-Task 3); `db.py` itself only reads `VAULT_TOKEN` from
the environment, it does not manage token lifecycle.

Falls back to `DATABASE_URL` when `VAULT_DB_ROLE` is not set, preserving the ability
to run `python db.py` directly with a superuser connection for schema init and seeding.

**Expected Outcomes:**
- `get_connection()` calls `hvac` to fetch a fresh ephemeral `(username, password)` when
  `VAULT_DB_ROLE` env var is set
- Connection string is assembled from `DB_HOST`, `DB_PORT`, `DB_NAME` env vars plus the
  Vault-issued credentials
- No credential values appear in logs
- Falls back cleanly to `DATABASE_URL` when Vault env vars are absent

**Todo List:**
1. Add `import hvac` at the top of `db.py`
2. Add private helper `_get_vault_db_credentials(role: str) -> tuple[str, str]`:
   - Creates `hvac.Client(url=os.environ["VAULT_ADDR"], token=os.environ["VAULT_TOKEN"])`
   - Calls `client.secrets.database.generate_credentials(name=role)`
   - Returns `(data["username"], data["password"])` from the response
3. Update `get_connection()`:
   - If `VAULT_DB_ROLE` is set: call `_get_vault_db_credentials`, build connection string
     as `postgresql://{user}:{password}@{DB_HOST}:{DB_PORT}/{DB_NAME}` using
     `DB_HOST`, `DB_PORT`, `DB_NAME` env vars
   - Otherwise: existing `DATABASE_URL` path unchanged
4. Update the module docstring to document the new env vars:
   `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_DB_ROLE`, `DB_HOST`, `DB_PORT`, `DB_NAME`

**Relevant Context:**
- `db.get_connection()` at [`db.py:17-28`](db.py:17) — only function that changes
- `hvac` database secrets path: `client.secrets.database.generate_credentials(name)`
  returns `{"data": {"username": "...", "password": "..."}}`
- All CRUD functions call `get_connection()` on every invocation — no connection pooling
  to worry about

**Status:** [ ] pending

---

## Sub-Task 3 — `agents/vault_helper.py` + update microservice entry points

**Intent:** Each microservice must authenticate to Vault at startup using its AppRole
credentials, set `VAULT_TOKEN` and `VAULT_DB_ROLE` in the process environment, and keep
the token alive via a background `renew_self` loop that runs until the token reaches its
`max_ttl` (at which point the service exits cleanly so the process manager can restart
it with a fresh login).

**Expected Outcomes:**
- New file `agents/vault_helper.py` contains:
  - `vault_approle_login(role_id, secret_id) -> hvac.Client` — performs AppRole login,
    returns an authenticated client with the token already set
  - `start_token_renewal(client, logger)` — starts a daemon `threading.Thread` that
    calls `client.auth.token.renew_self()` at half the remaining TTL interval; when
    `renew_self` raises (token at max TTL or revoked), logs an error and calls
    `os._exit(1)` to force a clean process restart
- Each of the four `agents/*_service.py` files gains an `_init_vault()` function that:
  - Reads `VAULT_ROLE_ID`, `VAULT_SECRET_ID`, `VAULT_ADDR` from env; skips Vault setup
    if any are absent and logs a warning (falls back to `DATABASE_URL`)
  - Calls `vault_approle_login` and sets `os.environ["VAULT_TOKEN"]` to the client token
  - Sets `os.environ["VAULT_DB_ROLE"]` to the service's role name constant
  - Sets `os.environ["DB_HOST"]`, `DB_PORT`, `DB_NAME` from env (these are passed in
    by `start_app.sh`)
  - Calls `start_token_renewal(client, logger)`
- `_init_vault()` is called in each service's `if __name__ == "__main__":` block,
  before `app.run()`

**Role name constants per service:**
| File | `VAULT_DB_ROLE` value |
|---|---|
| `agents/read_service.py` | `voyager_read` |
| `agents/booking_mutation_service.py` | `voyager_booking_write` |
| `agents/payment_read_service.py` | `voyager_payment_read` |
| `agents/payment_write_service.py` | `voyager_payment_write` |

**Todo List:**
1. Create `agents/vault_helper.py`:
   - `vault_approle_login(role_id, secret_id)`: calls
     `hvac.Client(url=...).auth.approle.login(role_id=role_id, secret_id=secret_id)`,
     sets `client.token` from `result["auth"]["client_token"]`, returns client
   - `start_token_renewal(client, logger)`: reads `lease_duration` from
     `client.auth.token.lookup_self()["data"]["ttl"]`, sleeps for half that interval,
     then calls `client.auth.token.renew_self()`; loops until renewal fails, then calls
     `os._exit(1)` (daemon thread — does not block shutdown)
2. In each of the four service files, add `from vault_helper import vault_approle_login, start_token_renewal`
   (relative import via `sys.path` already set by line 2 of each service)
3. Add `_init_vault(vault_db_role: str)` to each service file (or import a shared
   version from `vault_helper`) that performs the steps described above
4. Call `_init_vault("<role_name>")` in each service's `__main__` block before
   `app.run()`

**Relevant Context:**
- Each service already does `sys.path.insert(0, parent_dir)` at line 2, so
  `vault_helper` in the `agents/` directory is importable as `from vault_helper import ...`
- `hvac.Client.auth.token.renew_self()` raises `hvac.exceptions.Forbidden` (or similar)
  when the token cannot be renewed
- `os._exit(1)` is appropriate in a daemon thread to force full process exit without
  going through normal Python cleanup

**Status:** [ ] pending

---

## Sub-Task 4 — `start_app.sh`, `requirements.txt`, `.gitignore`

**Intent:** Wire everything together. `start_app.sh` sources the per-service AppRole
credential files produced by `setup_vault.sh`, exports shared DB connection env vars,
and launches all five processes. Add `hvac` to dependencies and protect the credential
directory from accidental commits.

**Expected Outcomes:**
- `start_app.sh` is a complete, executable launch script that:
  - Exports `VAULT_ADDR` (defaults to `http://127.0.0.1:8200`)
  - Exports `DB_HOST`, `DB_PORT`, `DB_NAME` parsed from `DATABASE_URL`
  - Sources `vault_approle_creds/<service>.env` for each service before launching it,
    passing `VAULT_ROLE_ID` and `VAULT_SECRET_ID` as per-process env vars
  - Starts the orchestrator (`app.py`) and the four services as background processes,
    writing PID files or using a simple trap for clean shutdown
  - Prints service URLs on startup
- `requirements.txt` includes `hvac>=2.3.0`
- `.gitignore` includes `vault_approle_creds/`

**Todo List:**
1. Write `start_app.sh` with `#!/usr/bin/env bash` and `set -euo pipefail`:
   - Set `VAULT_ADDR` default
   - Parse `DB_HOST`, `DB_PORT`, `DB_NAME` from `DATABASE_URL` using bash parameter
     expansion or `python3 -c "from urllib.parse import urlparse; ..."`
   - For each service: `env $(cat vault_approle_creds/<service>.env | xargs) python3
     agents/<service>.py &` pattern, or equivalent explicit `source` + subshell
   - Collect PIDs; register a `trap` to kill all on `SIGINT`/`SIGTERM`
   - Print a startup banner with port assignments
2. Add `hvac>=2.3.0` to `requirements.txt`
3. Add `vault_approle_creds/` to `.gitignore`

**Relevant Context:**
- `start_app.sh` is currently empty — full write
- Service port env vars: `READ_SERVICE_PORT=5101`, `BOOKING_SERVICE_PORT=5102`,
  `PAYMENT_READ_SERVICE_PORT=5103`, `PAYMENT_WRITE_SERVICE_PORT=5104`
- Orchestrator (`app.py`) runs on port 5000 (default Flask dev port)
- Existing `.gitignore` already covers `.env` files — `vault_approle_creds/` must be
  added as a directory entry

**Status:** [ ] pending
