# User Management & Authentication Plan

## Overview

Add multi-role user management and Keycloak OIDC authentication to the Voyager Travel
application. Every inbound request (REST endpoints and AI tool calls) must be validated
against a JWT issued by the existing Keycloak `voyager` realm. User identity and role
membership are stored in a new `users` table that mirrors Keycloak roles; both the REST
API and the four agent microservices enforce role-based access control using that table.

### Roles

| Role              | Scope                                                                           |
|-------------------|---------------------------------------------------------------------------------|
| `admin`           | Read and write all bookings and all payment cards                               |
| `bookings_admin`  | Read and write all bookings; no access to payment cards                         |
| `payments_admin`  | Read and write all payment cards; no access to bookings                         |
| `user`            | Read and write only their own bookings and payment cards                        |

### Key Design Decisions

- **Shared auth helper** — a new `auth_helper.py` module at the project root validates
  JWTs and extracts identity/roles for every service; no per-service duplication. Placing
  it at the root (alongside `db.py` and `tools.py`) means both `app.py` and each agent
  service can `import auth_helper` without any `sys.path` manipulation.
- **Hybrid role storage** — roles are Keycloak Realm Roles included in the JWT `realm_access.roles`
  claim AND mirrored in the `users.role` column so DB-layer filtering needs no extra token
  introspection.
- **JIT provisioning** — on first validated login the user record is created automatically;
  demo users are also pre-seeded for development.
- **`user_id` foreign keys** — `bookings` and `payment_cards` gain a nullable `user_id`
  column (nullable so existing seed rows remain valid during migration); all new rows
  require it.
- **Orchestrator `app.py` becomes role-aware** — `/api/bookings` and `/api/payment-cards`
  require a bearer token and filter results based on role/user_id.

---

## Sub-Task 1 — Keycloak Realm Roles & Demo Users

**Status:** `[ ] pending`

### Intent
Create the four roles (`admin`, `bookings_admin`, `payments_admin`, `user`) in the
`voyager` Keycloak realm, add demo user accounts that correspond to the seed data
personas, and assign roles. This gives every subsequent sub-task something real to
test against.

### Expected Outcomes
- Four realm roles exist in Keycloak: `admin`, `bookings_admin`, `payments_admin`, `user`.
- Six demo user accounts exist (one per seed persona + one admin) with known credentials
  and their Keycloak `sub` UUIDs recorded for use in later seeding steps.
- Existing service-account clients (`voyager-booking-read` etc.) are unchanged.
- A new `voyager-user` confidential client with Authorization Code + PKCE flow is created
  for the browser/chat UI so end-users can log in.

### Todo List
1. Extend `scripts/setup_keycloak_kcadm.sh` with a new section that:
   - Creates the four realm roles (idempotent).
   - Creates the `voyager-user` client (public, PKCE, redirect URI `http://127.0.0.1:5000/*`).
   - Creates demo users (`alice`, `bob`, `carol`, `david`, `bookings_mgr`, `payments_mgr`,
     `admin_user`) with password `voyager123` and `temporaryPassword=false`.
   - Assigns appropriate roles: alice/bob/carol/david → `user`; bookings_mgr → `bookings_admin`;
     payments_mgr → `payments_admin`; admin_user → `admin`.
2. Export a helper script or document the `kcadm.sh get users` commands needed to retrieve
   the `sub` (UUID) for each demo user — these values are needed for DB seeding in Sub-Task 2.

### Relevant Context
- `scripts/setup_keycloak_kcadm.sh` — existing kcadm pattern to follow; already handles
  idempotency and kcadm auth.
- Keycloak OIDC discovery: `http://127.0.0.1:8080/realms/voyager/.well-known/openid-configuration`
- Existing clients use `serviceAccountsEnabled=true`; the new user-facing client needs
  `standardFlowEnabled=true`, `publicClient=true`, `pkceEnabled=true` instead.

---

## Sub-Task 2 — Database Schema: `users` Table & Foreign Keys

**Status:** `[ ] pending`

### Intent
Add a `users` table (keyed on Keycloak `sub`) and `user_id` foreign key columns to
`bookings` and `payment_cards`. Update `init_db()` in `db.py` to apply these changes
safely when the tables already exist. Update `seed_db()` to pre-populate demo users and
re-associate existing seed rows to those users.

### Expected Outcomes
- `users` table exists with columns: `id SERIAL PK`, `keycloak_sub VARCHAR(36) UNIQUE NOT NULL`,
  `username VARCHAR(120) NOT NULL`, `role VARCHAR(30) NOT NULL`, `created_at TIMESTAMPTZ`.
- `bookings.user_id` and `payment_cards.user_id` columns reference `users.id` (nullable for
  backward compatibility; new rows will require it).
- `seed_db()` inserts demo users and updates existing seed bookings/cards rows to point to
  the correct user record.
- Running `python db.py` on a fresh or existing database succeeds idempotently.

### Todo List
1. Add `CREATE TABLE IF NOT EXISTS users (...)` to `init_db()` in `db.py`.
2. Add `ALTER TABLE bookings ADD COLUMN IF NOT EXISTS user_id INT REFERENCES users(id)`
   and the same for `payment_cards` to `init_db()`.
3. Update `seed_db()` to:
   - Insert demo users (keycloak_sub will be placeholder UUIDs at this stage; final values
     from Sub-Task 1 can be patched in the script).
   - Use `ON CONFLICT (keycloak_sub) DO NOTHING` for idempotency.
   - After inserting bookings/cards, `UPDATE bookings SET user_id = ... WHERE passenger_name = ...`
     to link seed rows to demo users.
4. Add a new `get_or_create_user(keycloak_sub, username, role)` function to `db.py` —
   used by the auth helper for JIT provisioning.
5. Add a final section to `scripts/setup_keycloak_kcadm.sh` that queries each demo
   user's `sub` UUID via `kcadm.sh get users --query username=<name>` and writes them
   to `scripts/demo_user_subs.env` (format: `ALICE_SUB=<uuid>` etc.). This file is
   gitignored. A wrapper `scripts/seed_users.sh` sources `demo_user_subs.env` and
   calls `python db.py seed-users` (a new CLI subcommand) to replace placeholder UUIDs
   with the real Keycloak-issued values using `ON CONFLICT (keycloak_sub) DO UPDATE`.

### Relevant Context
- `db.py` `init_db()` lines 103–140 — existing schema pattern.
- `db.py` `seed_db()` lines 147–206 — seeding pattern with idempotency guard.
- Seed personas to map: Alice Johnson → alice, Bob Martinez → bob,
  Carol Williams → carol, David Lee → david (payment cards).
- `_row_to_dict()` already handles date serialisation; no changes needed there.

---

## Sub-Task 3 — Shared Auth Helper (`agents/auth_helper.py`)

**Status:** `[ ] pending`

### Intent
Create a single importable module at the project root that all four agent microservices
(and `app.py`) import directly. It validates incoming JWTs against Keycloak's JWKS
endpoint, extracts the `sub` claim and `realm_access.roles`, and performs JIT user
provisioning via `db.get_or_create_user`.

### Expected Outcomes
- `auth_helper.py` (project root) exists and exports:
  - `validate_and_identify(token: str) -> dict | None` — returns `{"user_id": int, "sub": str, "role": str}` on success, `None` on failure.
  - `require_role(*allowed_roles)` — Flask decorator that enforces role membership and
    injects `g.current_user` into the request context.
- JWT validation uses the Keycloak JWKS URI (cached with a short TTL to avoid per-request
  round-trips); signature algorithm is RS256.
- Token expiry, audience (`voyager`), and issuer (`http://127.0.0.1:8080/realms/voyager`)
  are all validated.
- Failed validation logs at WARNING level with the reason; no token values are logged.
- Configuration read from env vars: `KEYCLOAK_BASE_URL`, `KEYCLOAK_REALM` (default `voyager`).

### Todo List
1. Add `PyJWT` and `cryptography` to `pyproject.toml` dependencies (for RS256 JWT decode).
2. Create `auth_helper.py` at the project root with:
   - `_get_jwks()` — fetches and caches the JWKS JSON from Keycloak with a 5-minute TTL
     using a simple `(data, fetched_at)` tuple in module scope; uses `requests.get` with
     a reasonable timeout.
   - `validate_and_identify(token)` — decodes and validates the JWT, calls
     `db.get_or_create_user(sub, preferred_username, primary_role)` and returns the user dict.
   - `require_role(*roles)` Flask decorator — calls `validate_and_identify`, returns 401
     JSON on failure, sets `flask.g.current_user` on success, calls the wrapped view.
3. Because `auth_helper.py` is at the project root alongside `db.py`, no `sys.path`
   changes are needed in `auth_helper.py` itself. Each agent service already does
   `sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))` to
   reach the root — this makes `import auth_helper` available to them automatically.

### Relevant Context
- Existing `validate_token()` stub in each service (e.g., `agents/booking_read_service.py`
  lines 17–21) — this will be replaced in Sub-Task 4.
- `agents/vault_helper.py` — the `sys.path` pattern in each agent service that already
  makes root-level imports work.
- JWT claim path for roles: `realm_access.roles` (list of strings).
- Role priority (when user has multiple): `admin` > `bookings_admin` > `payments_admin` > `user`.

---

## Sub-Task 4 — Wire Auth into the Four Agent Microservices

**Status:** `[ ] pending`

### Intent
Replace the stub `validate_token()` in each of the four agent services with the shared
`auth_helper.require_role` decorator, and update each service's DB queries to pass
`user_id`/`role` context so regular users see only their own data.

### Expected Outcomes
- All four services return `401 Unauthorized` for missing or invalid JWTs.
- `booking_read_service`: `list_bookings` and `get_booking` filter by `user_id` when role
  is `user`; `bookings_admin` and `admin` see all.
- `booking_mutation_service`: `create_booking` and `cancel_booking` enforce ownership —
  a `user` can only cancel their own booking; `bookings_admin` / `admin` can cancel any.
- `payment_read_service`: `list_payment_cards` filters by `user_id` for `user` role;
  `payments_admin` / `admin` see all.
- `payment_write_service`: `add_payment_card`, `update_payment_card`, `remove_payment_card`
  enforce ownership for `user` role; `payments_admin` / `admin` can modify any.
- Token forwarding: `app.py` `dispatch_tool()` must forward the original user's bearer
  token to each service call.

### Todo List
1. Update `db.py` query functions to accept an optional `user_id` parameter for ownership
   filtering:
   - `list_bookings(passenger_name=None, user_id=None)` — add `AND user_id = %s` when set.
   - `get_booking(reference, user_id=None)` — add ownership check.
   - `cancel_booking(reference, user_id=None)` — add ownership check.
   - `list_payment_cards(user_id=None)` — add `AND user_id = %s` when set.
   - `get_payment_card(card_id, user_id=None)` — add ownership check.
   - `update_payment_card(card_id, user_id=None, **fields)` — add ownership check.
   - `remove_payment_card(card_id, user_id=None)` — add ownership check.
   - `add_payment_card(...)` — accept `user_id` as a required parameter and persist it.
   - `create_booking(...)` — accept `user_id` as a required parameter and persist it.
2. In each agent service, remove the `validate_token()` stub and replace with:
   ```python
   from auth_helper import require_role, validate_and_identify
   ```
   Apply `@require_role("user", "bookings_admin", "admin")` (etc.) to the `/execute` route.
3. Inside each `/execute` handler, read `flask.g.current_user` to get `user_id` and `role`,
   then pass `user_id` to DB calls when role is `user`.
4. Update `tools.dispatch_tool()` in `tools.py` to accept and forward a `token` argument;
   update `app.py` `/api/chat` to extract the bearer token from the inbound request and
   pass it through the tool-dispatch call chain.

### Relevant Context
- `agents/booking_read_service.py` lines 17–21 — stub to replace.
- `agents/booking_mutation_service.py`, `agents/payment_read_service.py`,
  `agents/payment_write_service.py` — same stub pattern in each.
- `tools.py` `dispatch_tool()` — currently sends `AUTH_TOKEN` env var; needs to forward
  the live user token instead.
- `app.py` `chat()` route — the inbound `Authorization` header must be extracted and
  threaded down to `dispatch_tool`.

---

## Sub-Task 5 — Role-Aware REST Endpoints in `app.py`

**Status:** `[ ] pending`

### Intent
Protect the `/api/bookings` and `/api/payment-cards` endpoints in the orchestrator with
the same auth helper, and apply role-based filtering so admins see everything and regular
users see only their own data.

### Expected Outcomes
- `GET /api/bookings` requires a valid JWT; returns all bookings for `admin`/`bookings_admin`,
  own bookings only for `user`; returns `403` for `payments_admin`.
- `GET /api/payment-cards` requires a valid JWT; returns all cards for `admin`/`payments_admin`,
  own cards only for `user`; returns `403` for `bookings_admin`.
- Both endpoints return `401` when no token or invalid token is presented.
- The UI sidebar continues to work — the `index.html` fetch calls must include the bearer
  token from the logged-in session.

### Todo List
1. Import `require_role` from `auth_helper` in `app.py` (adjusting `sys.path` if needed,
   or extracting auth_helper to a top-level location reachable by both `app.py` and
   `agents/`).
2. Decorate `get_bookings()` with `@require_role("admin", "bookings_admin", "user")` and
   filter by `g.current_user["user_id"]` when role is `user`.
3. Decorate `get_payment_cards()` with `@require_role("admin", "payments_admin", "user")`
   and filter similarly.
4. Update `static/index.html` to:
   - On page load, redirect to Keycloak login if no valid token is stored in
     `sessionStorage`.
   - Store the returned `access_token` and include it as `Authorization: Bearer <token>`
     in all fetch calls to `/api/bookings`, `/api/payment-cards`, and `/api/chat`.
   - Handle `401` responses by redirecting back to the Keycloak login page.

### Relevant Context
- `app.py` lines 214–223 — the two endpoints to protect.
- `static/index.html` — the fetch calls for the sidebar and chat stream.
- The `voyager-user` client created in Sub-Task 1 handles the PKCE Authorization Code
  flow for the browser; redirect URI is `http://127.0.0.1:5000/`.

---

## Sub-Task 6 — Documentation & Startup Script Updates

**Status:** `[ ] pending`

### Intent
Update the `README.md` and `start_app.sh` to reflect the new authentication requirements,
environment variables, and setup steps so the application is runnable end-to-end.

### Expected Outcomes
- `README.md` has a new "Authentication & User Management" section covering roles, demo
  credentials, and the Keycloak login flow.
- `start_app.sh` exports `KEYCLOAK_BASE_URL` and `KEYCLOAK_REALM` for the auth helper.
- Setup order is documented: Keycloak → DB schema → Vault → start services.
- Demo login table lists username, password, role, and what data the user can see.

### Todo List
1. Add `KEYCLOAK_BASE_URL` and `KEYCLOAK_REALM` env var exports to `start_app.sh`
   (defaulting to `http://127.0.0.1:8080` and `voyager`).
2. Update `README.md`:
   - Add role matrix table.
   - Add demo user credentials table.
   - Add setup step: "Run `./scripts/setup_keycloak_kcadm.sh` before starting services."
   - Update API section to note that all endpoints now require `Authorization: Bearer <token>`.

### Relevant Context
- `start_app.sh` — existing env var sourcing pattern.
- `README.md` — existing structure to extend.
