# Vault Agentic AI Plan

## Overview

This document describes the planned migration from the **legacy AppRole + database secrets engine**
integration (implemented in `vault-integration-plan.md`) to the new **Agentic AI** capabilities
introduced in HashiCorp Vault 2.1.

Vault 2.1 adds first-class support for AI agent workloads through three new primitives:

| Primitive | What it does |
|---|---|
| **Workload Identity Federation (WIF)** | Agents prove their identity using a short-lived, cryptographically signed JWT rather than a long-lived `secret_id`. No pre-provisioned credential files needed. |
| **Agent auth method** (`auth/agent/`) | A new Vault auth method purpose-built for AI agent processes. Accepts WIF tokens and issues narrowly-scoped Vault tokens with built-in tool-call policy enforcement. |
| **Tool-call policies** | Vault policies that express *which tools an agent is allowed to invoke*, not just which secrets it can read. The policy engine gains `tool_calls` as a first-class capability type alongside `read`/`write`/`delete`. |

Together these remove the two main friction points of the legacy approach:
- Secret files (`vault_approle_creds/`) that must be generated, distributed, and rotated out-of-band
- Coarse-grained DB credential scoping that cannot express "this agent may call `list_bookings`
  but not `cancel_booking`"

---

## Architecture Comparison

### Legacy (current — AppRole + database secrets engine)

```
┌─────────────────────────────────────────────────────────────────┐
│  Startup                                                        │
│                                                                 │
│  setup_vault.sh ──► vault_approle_creds/<service>.env          │
│       (one-time, operator runs manually)                        │
└────────────────────────────┬────────────────────────────────────┘
                             │ VAULT_ROLE_ID + VAULT_SECRET_ID
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  agents/*_service.py  ──►  vault_helper.vault_approle_login()  │
│                             │                                   │
│                             ▼                                   │
│                        Vault token (1 h TTL)                   │
│                        background renewal thread               │
└────────────────────────────┬────────────────────────────────────┘
                             │ VAULT_TOKEN
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  db.get_connection()  ──►  Vault database/creds/<role>         │
│                             │                                   │
│                             ▼                                   │
│                        ephemeral (user, password)              │
│                        ──► psycopg2.connect(DSN)               │
└─────────────────────────────────────────────────────────────────┘

Auth boundary:  PostgreSQL role (SELECT / INSERT / UPDATE / DELETE)
Tool boundary:  none — any caller that holds a Vault token for a role
                can request credentials for every tool that role covers
Secret storage: vault_approle_creds/*.env  (files on disk, gitignored)
```

### Agentic AI (planned — Vault 2.1)

```
┌─────────────────────────────────────────────────────────────────┐
│  Startup                                                        │
│                                                                 │
│  No credential files — agent self-identifies via WIF JWT       │
└────────────────────────────┬────────────────────────────────────┘
                             │ signed JWT (short-lived, in-process)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  agents/*_service.py  ──►  vault_helper.vault_agent_login()    │
│                             │  POST /v1/auth/agent/login        │
│                             ▼                                   │
│                        Vault token (bound to tool-call policy) │
└────────────────────────────┬────────────────────────────────────┘
                             │ VAULT_TOKEN  (carries tool capabilities)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Per tool call:                                                 │
│                                                                 │
│  vault_helper.request_tool_call(tool_name, args)               │
│       │  POST /v1/agent/tool-call                              │
│       │  Vault enforces tool-call policy before returning creds │
│       ▼                                                         │
│  ephemeral (user, password) — only issued if tool is allowed   │
│       ──► db.get_connection() ──► psycopg2.connect(DSN)        │
└─────────────────────────────────────────────────────────────────┘

Auth boundary:  Vault tool-call policy  (per named tool)
Tool boundary:  Vault enforces which tool names the token may call
Secret storage: none on disk — WIF JWT generated at runtime
```

---

## Side-by-Side Differences

| Dimension | Legacy (current) | Agentic AI (planned) |
|---|---|---|
| **Agent identity** | AppRole `role_id` + `secret_id` stored in `.env` files | Workload Identity JWT — generated at runtime, never stored |
| **Credential distribution** | `setup_vault.sh` writes files; operator must re-run on rotation | Self-service — agent fetches its own short-lived JWT on startup |
| **Auth method** | `auth/approle/` | `auth/agent/` (new in Vault 2.1) |
| **Policy type** | `capabilities = ["read"]` on a DB creds path | `tool_calls = ["allowed"]` on a named tool path |
| **Tool-level enforcement** | None — any service with a token may call any DB role it's bound to | Vault rejects requests for tools not listed in the agent's policy |
| **DB credential issuance** | `db.py` calls `database/creds/<role>` directly | Vault 2.1 issues DB creds only after tool-call policy check passes |
| **Token renewal** | Background thread calling `renew_self()` | Vault 2.1 tokens are auto-renewed by the agent auth method |
| **Files on disk** | `vault_approle_creds/<service>.env` (must be gitignored) | None |
| **Vault config script** | `setup_vault.sh` | New `setup_vault_agentic.sh` (planned) |
| **Python helper** | `agents/vault_helper.py` — `vault_approle_login`, `start_token_renewal` | `agents/vault_helper.py` gains `vault_agent_login`, `request_tool_call` |
| **`db.py` changes** | `_get_vault_db_credentials` called directly from `get_connection()` | Credential fetch wrapped behind `request_tool_call`; `db.py` receives pre-validated creds |

---

## Files That Change (planned)

| File | Legacy state | Change needed for Vault 2.1 |
|---|---|---|
| `setup_vault.sh` | Configures AppRole + DB roles | Unchanged — legacy config remains valid alongside new config |
| `setup_vault_agentic.sh` | Does not exist | New: configures `auth/agent/`, WIF, tool-call policies |
| `agents/vault_helper.py` | `vault_approle_login`, `start_token_renewal` | Add `vault_agent_login(jwt)`, `request_tool_call(token, tool, args)` |
| `agents/*_service.py` | Calls `_init_vault()` → `vault_approle_login` | `_init_vault()` conditionally calls `vault_agent_login` when `VAULT_AGENT_MODE=1` |
| `db.py` | `_get_vault_db_credentials` called from `get_connection()` | `get_connection()` accepts pre-fetched `(user, password)` passed from service layer |
| `start_app.sh` | Sources `.env` files for each service | Add `VAULT_AGENT_MODE` export; no `.env` files needed in agent mode |
| `.gitignore` | `vault_approle_creds/` | No new entries needed (no files generated) |

---

## Planned Sub-Tasks

These are not yet scheduled. They are listed here so the scope is clear before implementation
begins.

### Sub-Task A — `setup_vault_agentic.sh`

Configure the new Vault 2.1 primitives alongside the existing engine (non-destructive):

1. Enable `auth/agent/` mount
2. Configure a WIF trust bundle (for local dev: a self-signed JWKS)
3. Write four agent roles (`voyager-read-agent`, etc.) bound to tool-call policies
4. Write four tool-call policies — one per service, listing only the tool names that service
   is permitted to call:

   ```hcl
   # Example: read_service tool-call policy
   path "agent/tool-call/get_booking" {
     capabilities = ["create"]
     tool_calls   = ["allowed"]
   }
   path "agent/tool-call/list_bookings" {
     capabilities = ["create"]
     tool_calls   = ["allowed"]
   }
   ```

5. Verify with `vault token capabilities agent/tool-call/cancel_booking` for the read-service
   token — must return `denied`

### Sub-Task B — `agents/vault_helper.py` additions

Add two new functions without touching existing ones (fully backwards compatible):

- `vault_agent_login(jwt: str) -> hvac.Client`
  - `POST /v1/auth/agent/login` with the WIF JWT
  - Returns client with token set (same interface as `vault_approle_login`)
- `request_tool_call(client: hvac.Client, tool_name: str, args: dict) -> dict`
  - `POST /v1/agent/tool-call` with `{"tool": tool_name, "args": args}`
  - Vault validates the tool-call policy and returns ephemeral DB credentials in the response
  - Returns `{"username": ..., "password": ...}` on success; raises on policy denial

### Sub-Task C — Service file updates (`agents/*_service.py`)

Each service's `_init_vault()` gains a branch:

```python
if os.environ.get("VAULT_AGENT_MODE") == "1":
    jwt = _generate_wif_jwt()          # new helper: builds and signs the JWT
    client = vault_agent_login(jwt)
else:
    client = vault_approle_login(role_id, secret_id)  # existing path unchanged
```

Each tool handler wraps its `db.*` call:

```python
# Before (legacy):
booking = db.get_booking(reference)

# After (Agentic AI):
creds = request_tool_call(client, "get_booking", {"reference": reference})
booking = db.get_booking(reference, db_creds=creds)
```

### Sub-Task D — `db.py` updates

`get_connection()` gains an optional `db_creds` parameter:

```python
def get_connection(db_creds: tuple[str, str] | None = None):
    if db_creds:
        user, password = db_creds
        # build DSN from DB_HOST/DB_PORT/DB_NAME as now
    elif vault_role := os.environ.get("VAULT_DB_ROLE"):
        user, password = _get_vault_db_credentials(vault_role)   # legacy path
    else:
        url = os.environ.get("DATABASE_URL")  # fallback path
        ...
```

The legacy path through `VAULT_DB_ROLE` remains fully intact — no behaviour changes unless
`VAULT_AGENT_MODE=1` is set.

---

## Status

| Sub-task | Status |
|---|---|
| Legacy AppRole integration (`vault-integration-plan.md`) | ✅ Complete |
| Sub-Task A — `setup_vault_agentic.sh` | ⬜ Pending |
| Sub-Task B — `vault_helper.py` additions | ⬜ Pending |
| Sub-Task C — Service file updates | ⬜ Pending |
| Sub-Task D — `db.py` updates | ⬜ Pending |
