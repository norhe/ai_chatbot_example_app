# Plan: Move JIT User Provisioning from Agent Services to the Orchestrator

## Overview

Today every process that validates a JWT via `auth_helper.validate_and_identify` also performs
JIT provisioning (`db.get_or_create_user`). That is the orchestrator (`app.py`) and all four agent
services. As a result, every Vault DB role carries `INSERT` on `users`.

**Goal:** only the orchestrator creates user rows. Agent services keep validating the JWT
themselves, then do a read-only lookup of the user by `keycloak_sub`, and return 401 if the user
does not exist.

**Decisions already made (user-confirmed):**

1. Agents do **not** trust forwarded identity headers. They validate the JWT and run a read-only
   lookup by `keycloak_sub`. A missing user returns 401.
2. The orchestrator gets its own Vault DB role `voyager_orchestrator`, with its own policy,
   AppRole and `_init_vault()`.
3. `get_or_create_user` changes to `INSERT ... ON CONFLICT (keycloak_sub) DO NOTHING RETURNING *`,
   followed by a re-SELECT when no row is returned. The initial SELECT stays.
4. `db.init_db()` and `db.seed_db()` are removed from `app.py`. Schema init and seeding happen only
   through `python db.py` (`setup_all.sh` / README).
5. Syncing `users.role` or `users.username` for existing users is **out of scope**.

**Non-goals:** changing Keycloak realm or client setup, changing the Ollama tool schemas, adding
role/username sync, and registering the orchestrator in the Layer 2 agent registry (see Open
Items).

## Target flow

- Orchestrator: validate JWT, then **provision** (get-or-create), then proceed.
- Agent: validate JWT, then **lookup only** (`get_user_by_sub`). If the user is missing, return
  401.
- `require_role` and `validate_and_identify` default to lookup-only (fail-safe). Only the
  orchestrator opts in to provisioning.

## Findings that shape the plan

- `db.get_or_create_user` currently uses `ON CONFLICT ... DO UPDATE`. Postgres needs UPDATE
  privilege for `DO UPDATE`, and the Vault roles only grant `SELECT, INSERT` on `users`. Verify
  this against a Vault-issued role. It may already fail today, and the failure is swallowed into a
  401 by `auth_helper`.
- `users.id` is `SERIAL`. An INSERT by a non-owner role likely also needs `USAGE` on
  `users_id_seq`, and `setup_vault.sh` does not grant it today. Verify against a Vault-issued
  role.
- `app.py` calls `validate_and_identify` inside `/api/chat` `generate()`. It also uses
  `require_role` on `/api/bookings` and `/api/payment-cards`. All three orchestrator paths need
  provisioning enabled.
- `start_app.sh` currently runs the orchestrator on `DATABASE_URL` with no Vault role.
- `app.py` runs `db.init_db()` and `db.seed_db()` at import time. These need DDL and INSERT on
  bookings and payment_cards, which are incompatible with a minimal `voyager_orchestrator` role.
- `vault_helper.py` lives in `agents/`. The agents import it as `from vault_helper import ...`
  because the script directory is on `sys.path`. `agents/__init__.py` exists, so the orchestrator
  can import `agents.vault_helper`.

---

## Sub-Task 1 — Make user lookup/creation DB functions safe for least privilege

**Status:** `[ ] pending`

**Intent:** Provide DB functions that work with a role that has only `SELECT, INSERT` on `users`,
and give the agents a read-only lookup.

**Expected Outcomes**
- `get_or_create_user` never issues an `UPDATE`. A concurrent first-login race yields the same
  row for both callers.
- A new read-only `get_user_by_sub` returns the user dict, or `None` when the user is missing.

**Todo List**
1. Keep the initial `SELECT ... WHERE keycloak_sub = %s` in `get_or_create_user`.
2. Change the insert to `ON CONFLICT (keycloak_sub) DO NOTHING RETURNING *`.
3. If the insert returns no row (lost a race), re-SELECT by `keycloak_sub`. If that still returns
   nothing, raise so the caller fails closed.
4. Update the docstring: used only by the orchestrator, does not sync username or role.
5. Add `get_user_by_sub(keycloak_sub)` as a single parameterized SELECT that returns
   `_row_to_dict(row)` or `None`.

**Relevant Context**
- [`db.get_or_create_user`](../db.py) (around line 330) and `_row_to_dict`.
- `seed_db` already uses `ON CONFLICT (keycloak_sub) DO NOTHING`, so the pattern is established.
- Do not go insert-first. `DO NOTHING` still consumes a `SERIAL` value on conflict.

---

## Sub-Task 2 — Split provisioning from validation in `auth_helper`

**Status:** `[ ] pending`

**Intent:** Make provisioning an explicit orchestrator-only opt-in, and make the agent path
lookup-only.

**Expected Outcomes**
- `validate_and_identify` and `require_role` take a `provision` flag that defaults to `False`.
- With `provision=True`, behavior matches today: `get_or_create_user` runs.
- With `provision=False`, `get_user_by_sub` is used. A missing user returns `None`, so 401 from
  `require_role`.
- Trace spans still make the lookup-versus-provision distinction visible. The existing
  "JIT User Provisioning" span is only emitted on the orchestrator path.

**Todo List**
1. Add the `provision` parameter to `validate_and_identify` and thread it through `require_role`.
2. Replace the unconditional `db.get_or_create_user` call with a branch on `provision`.
3. Add an error log and trace span for "user not provisioned" on the lookup path. Log only the
   event name, reason, `sub`, `username` and `role`. Never log the token or any part of it, since
   trace spans are streamed to the browser and merged across services.
4. Update the module docstring, which currently says it performs JIT provisioning.
5. Remove the unused `validate_and_identify` import from `agents/booking_read_service.py` and other agent services.

**Relevant Context**
- [`auth_helper.py`](../auth_helper.py): `validate_and_identify` (around lines 77–240) and
  `require_role` (around line 243).
- The agent services call `@require_role(...)` with no other change needed, because the default is
  lookup-only.
- The `src` and `collector` span pattern used by the existing `collector.record(...)` calls.

---

## Sub-Task 3 — Orchestrator opts in to provisioning and gets its own Vault DB access

**Status:** `[ ] pending`

**Intent:** Make `app.py` the only provisioning caller, running on a dedicated least-privilege
Vault role instead of `DATABASE_URL`.

**Expected Outcomes**
- `/api/chat`, `/api/bookings` and `/api/payment-cards` all provision on valid tokens.
- `app.py` no longer calls `db.init_db()` or `db.seed_db()`.
- On startup, `app.py` authenticates to Vault via AppRole and sets `VAULT_DB_ROLE` to
  `voyager_orchestrator`. It falls back to `DATABASE_URL` when the Vault env vars are absent, the
  same as the agents.

**Todo List**
1. Remove `db.init_db()` and `db.seed_db()` from `app.py`.
2. Pass `provision=True` in the `auth_helper.validate_and_identify` call in `/api/chat` and in the
   two `@require_role(...)` decorators.
3. Add an `_init_vault()` to `app.py` mirroring the agents' version. Use
   `voyager_orchestrator` for `VAULT_DB_ROLE`. Call it from the `__main__` block before
   `app.run`.
4. Import `vault_approle_login` and `start_token_renewal` from `agents.vault_helper`.
5. Confirm a clean first start against a database that was initialised via `python db.py`.

**Relevant Context**
- [`app.py`](../app.py): lines 28–29 (init/seed), line 123 (chat validation), lines 293–315 (REST
  endpoints and `__main__`).
- [`agents/booking_read_service.py`](../agents/booking_read_service.py) `_init_vault` (lines
  75–95) is the pattern to copy.
- [`agents/vault_helper.py`](../agents/vault_helper.py) and [`db.get_connection`](../db.py)
  (line 83).

---

## Sub-Task 4 — Vault setup: new orchestrator role, remove INSERT from agent roles

**Status:** `[ ] pending`

**Intent:** Enforce the new boundary at the database level.

**Expected Outcomes**
- New Vault DB role `voyager_orchestrator`, granting these privileges only:
  - `SELECT, INSERT` on `users`
  - `USAGE` on `users_id_seq` (verify it is needed)
  - `SELECT` on `bookings`
  - `SELECT` on `payment_cards`
- The four agent roles grant only `SELECT` on `users`, plus their existing bookings or
  payment_cards grants.
- A `voyager-orchestrator-policy` allowing `read` on `database/creds/voyager_orchestrator`.
- An AppRole `voyager-orchestrator` whose credentials are written to
  `scripts/vault_approle_creds/orchestrator.env`.

**Todo List**
1. Add `voyager_orchestrator` to `allowed_roles` in the `database/config/voyager-postgres` write.
2. Add `_write_db_role "voyager_orchestrator"` with the grants above. Remove `INSERT ON users`
   from the four existing role statements.
3. Add the orchestrator policy via `_write_policy`.
4. Add `_write_approle_creds "voyager-orchestrator" "voyager-orchestrator-policy" "orchestrator"`.
5. Update "Four database roles written" / "Four policies written" messages to five.
6. Note in the script header that re-running is required, and that already-issued short-lived
   leases keep their old grants until they expire. The default TTL is 1h.

**Relevant Context**
- [`scripts/setup_vault.sh`](../scripts/setup_vault.sh): `allowed_roles` (line 60), `_write_db_role`
  calls (lines 89–96), `_write_policy` (lines 116–119), `_write_approle_creds` (lines 188–191).
- [`scripts/setup_all.sh`](../scripts/setup_all.sh) step 4 runs `setup_vault.sh`, so no change is
  expected there.

---

## Sub-Task 5 — Launch the orchestrator with its Vault credentials

**Status:** `[ ] pending`

**Intent:** Make `start_app.sh` start `app.py` the same way it starts the services.

**Expected Outcomes**
- The orchestrator sources `scripts/vault_approle_creds/orchestrator.env`, with `VAULT_ADDR`,
  `DB_HOST`, `DB_PORT`, `DB_NAME` exported.
- If the creds file is missing, it warns and falls back to `DATABASE_URL`, the same as the agents.
- The stale comment "uses DATABASE_URL directly; no Vault role needed" is gone.

**Todo List**
1. Reuse `_launch_service` for the orchestrator. The orchestrator has no port env var, since the
   port is hardcoded to 5000. Either make the `port_var` argument optional, or add a small
   dedicated launch block. Pick whichever is the smaller diff.
2. Update the comment at line 90 and the header prerequisites.

**Relevant Context**
- [`start_app.sh`](../start_app.sh): `_launch_service` (lines 52–72), orchestrator launch (lines
  90–92).

---

## Sub-Task 6 — Tests and verification

**Status:** `[ ] pending`

**Intent:** Prove the boundary holds, not just that the code reads correctly.

**Expected Outcomes**
- Unit tests cover `auth_helper` behavior for both modes, with `db` and JWT validation mocked.
- Manual verification confirms the privilege assumptions.

**Todo List**
1. Add unit tests, following the `unittest` and `mock` style of `tests/test_tools_tracing.py`:
   - `provision=False` with an existing user returns the identity.
   - `provision=False` with an unknown `sub` returns `None`, so 401 from `require_role`.
   - `provision=True` with an unknown `sub` calls `get_or_create_user`.
   - `get_or_create_user` returns the existing row on a lost insert race (re-SELECT path).
2. Manual: run `setup_all.sh`, then `start_app.sh`.
3. Manual, privilege check: use a Vault-issued `voyager_orchestrator` credential to confirm the
   first-login insert succeeds. Confirm that an agent role attempting `INSERT INTO users` is
   denied.
4. Manual, end-to-end: a brand-new Keycloak user chats through the orchestrator (provisioned, tool
   call succeeds). The same token sent directly to an agent `/execute` before ever hitting the
   orchestrator returns 401.
5. Confirm all existing tests still pass.

**Relevant Context**
- [`tests/test_tools_tracing.py`](../tests/test_tools_tracing.py).
- `scripts/setup_keycloak_kcadm.sh` for creating a test user.

---

## Sub-Task 7 — Documentation

**Status:** `[ ] pending`

**Intent:** Keep the docs accurate about who provisions users and who holds which DB privileges.

**Expected Outcomes**
- The README states that the orchestrator provisions users and the agents are lookup-only.
- The Vault permission table lists the `voyager_orchestrator` role and the reduced `users`
  grants.

**Todo List**
1. Update the permission-boundary table (README around lines 453–460). It already looks stale,
   for example `voyager_read`, so correct it while editing.
2. Update the request-flow description that mentions `auth_helper.require_role` (README around
   line 377).
3. Update the project file list (README around lines 494–518) if new files are added.
4. Document that `app.py` no longer initialises or seeds the schema, and that `python db.py` must
   run first.

**Relevant Context**
- [`README.md`](../README.md): the lines above.
- Prior plans `plans/user-auth-plan.md` and `plans/vault-integration-plan.md` describe the old
  behavior. They are history and are not edited.

---

## Open Items

- **Layer 2 (agent registry):** `scripts/vault_agentic_keycloak_setup.sh` has a ceiling policy
  listing the four `database/creds/*` paths, and `scripts/vault_register_agents.sh` registers the
  four agents. This plan treats the orchestrator as an AppRole client only and does **not** add
  it to the Layer 2 registry or ceiling policy. Confirm that is acceptable.
- **Privilege assumptions to verify in Sub-Task 6:** that `DO UPDATE` needs UPDATE privilege
  even without a conflict, and that `users_id_seq` needs an explicit `USAGE` grant. If either is
  wrong, the plan still holds but the grant list shrinks.
