# Voyager Travel — AI Booking Assistant

A streaming AI travel booking agent powered by [Ollama](https://ollama.com/) and Flask, with a
PostgreSQL backend and a conversational tool-calling interface for managing bookings and payment cards.

## Prerequisites

### Python & uv
Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. uv manages the virtual
environment and dependencies — no manual `pip` or `venv` commands needed.

Install uv if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Ollama
[Ollama](https://ollama.com/) must be running locally with at least one model pulled:

```bash
ollama pull llama3.2:latest
```

### PostgreSQL

PostgreSQL is required. The app connects via a `DATABASE_URL` environment variable and creates
its schema automatically on first start — no manual SQL needed.

> **Handled automatically by [`scripts/setup_postgres.sh`](scripts/setup_postgres.sh)** when
> using the full Agentic IAM setup (see [With Vault + Keycloak](#with-vault--keycloak-agentic-iam)
> below). The manual commands are shown here for reference or standalone use.

The script uses `registry.redhat.io/rhel9/postgresql-16:latest` and binds the data volume to
`./postgres-data` so it survives container restarts.

#### Run with Podman (manual / standalone)

```bash
mkdir -p ./postgres-data

podman run -d \
  --name voyager-postgres \
  -e POSTGRESQL_USER=voyager \
  -e POSTGRESQL_PASSWORD=voyager \
  -e POSTGRESQL_DATABASE=voyager \
  -p 127.0.0.1:5432:5432 \
  -v "$(pwd)/postgres-data:/var/lib/pgsql/data:Z" \
  postgres:latest
```

> The `:Z` volume flag is required on SELinux-enforcing systems (Fedora, RHEL, etc.) to relabel the
> host directory so the container can write to it.

#### Stop / restart

```bash
podman stop voyager-postgres
podman start voyager-postgres
```

#### Connect to the database (after the application has run at least once to seed the db)

```bash
podman exec -it voyager-postgres psql -U voyager -d voyager
```

### Keycloak

Keycloak is required for the Agentic IAM path. It runs in Podman and acts as the OIDC/JWT
identity provider for all four agent microservices.

> **Handled automatically by [`scripts/setup_keycloak_kcadm.sh`](scripts/setup_keycloak_kcadm.sh)**
> and [`scripts/keycloak_ai_vault_integration.sh`](scripts/keycloak_ai_vault_integration.sh) when
> using `scripts/setup_all.sh`. The manual command is shown here for reference.

```bash
# Start Keycloak in dev mode (manual / standalone)
podman run -d \
  --name voyager-keycloak \
  -p 127.0.0.1:8080:8080 \
  -e KEYCLOAK_ADMIN=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak \
  start-dev
```

The setup scripts configure a `voyager` realm with four confidential service-account clients
(one per microservice) and a `vault-oidc` client with an audience mapper used by Vault.

## Quick start

### Without Vault (direct DATABASE\_URL)

```bash
# 1. Install dependencies (creates .venv automatically)
uv sync

# 2. Set the database connection string
export DATABASE_URL="postgresql://voyager:voyager@127.0.0.1:5432/voyager"

# 3. Initialise the database and seed demo data (first run only)
uv run python db.py

# 4. Start the agent microservices
./services/start_services.sh

# 5. Run the orchestrator
uv run python app.py
```

To stop the agent services when you're done:

```bash
./services/stop_services.sh
```

### With Vault (AppRole + dynamic DB credentials)

```bash
# 1. Install dependencies
uv sync

# 2. Set environment — DATABASE_URL is used by scripts/setup_vault.sh to configure the
#    Vault database connection and by db.py for schema init
export DATABASE_URL="postgresql://voyager:voyager@127.0.0.1:5432/voyager"
export VAULT_ADDR="http://127.0.0.1:8200"
export VAULT_TOKEN="<your-vault-dev-root-token>"

# 3. Start Vault in dev mode (separate terminal)
vault server -dev

# 4. Initialise the database (first run only) — uses DATABASE_URL directly
uv run python db.py

# 5. Configure Vault — enables database engine, creates roles, policies,
#    and writes scripts/vault_approle_creds/<service>.env for each microservice
./scripts/setup_vault.sh

# 6. Start all five processes (orchestrator + 4 services) with Vault creds
./start_app.sh
```

Then open **http://127.0.0.1:5000** in your browser.

### With Vault + Keycloak (Agentic IAM)

This path adds a Keycloak OIDC layer and wires Vault's OAuth Resource Server profile and
agent registry on top of the AppRole base. `scripts/setup_all.sh` runs all six setup steps
in the correct order.

**Prerequisites:** `podman`, `vault`, `jq`, and `curl` on `PATH`; a running Vault dev server.

```bash
# 1. Install dependencies
uv sync

# 2. Set environment
export DATABASE_URL="postgresql://voyager:voyager@127.0.0.1:5432/voyager"
export VAULT_ADDR="http://127.0.0.1:8200"
export VAULT_TOKEN="<your-vault-dev-root-token>"
export KEYCLOAK_ADMIN_PASSWORD="admin"   # default; change if desired

# 3. Start Vault in dev mode (separate terminal)
vault server -dev

# 4. Initialise the database (first run only)
uv run python db.py

# 5. Run the full infrastructure setup (idempotent — safe to re-run)
./scripts/setup_all.sh

# 6. Start all five processes with Vault AppRole credentials
./start_app.sh
```

`setup_all.sh` runs these six scripts in order — each is also safe to run standalone:

| Step | Script | What it does |
|------|--------|--------------|
| 1 | `scripts/setup_postgres.sh` | Start PostgreSQL container, wait for readiness |
| 2 | `scripts/setup_keycloak_kcadm.sh` | Start Keycloak, create `voyager` realm and four service-account clients |
| 3 | `scripts/keycloak_ai_vault_integration.sh` | Add `vault-oidc` client and audience mapper to the `voyager` realm |
| 4 | `scripts/setup_vault.sh` | Enable DB secrets engine, create roles, policies, AppRole credentials |
| 5 | `scripts/vault_agentic_keycloak_setup.sh` | Write Vault OAuth Resource Server profile and ceiling policy |
| 6 | `scripts/vault_register_agents.sh` | Register each service agent (Identity entity, alias, agent registry) |

Then open **http://127.0.0.1:5000** in your browser.

## Configuration

All options are controlled via environment variables:

| Variable              | Default                    | Description                                                        |
|-----------------------|----------------------------|--------------------------------------------------------------------|
| `DATABASE_URL`        | *(required)*               | PostgreSQL connection string                                       |
| `OLLAMA_BASE_URL`     | `http://127.0.0.1:11434`  | Ollama API base URL                                                |
| `OLLAMA_MODEL`        | `llama3.2:latest`          | Default model (overridable in UI)                                  |
| `REQUEST_TIMEOUT`     | `120`                      | Seconds before streaming times out                                 |
| `AUTH_TOKEN`          | `dev-token`                | Bearer token sent to each agent service for authorization          |
| `AGENT_BASE_URL`      | `http://127.0.0.1`         | Base URL for all agent services (override for container deployments) |
| `DEBUG`               | *(unset)*                  | Set to `1` to enable verbose debug logging                         |
| `READ_SERVICE_PORT`   | `5101`                     | Port for the ReadService                                           |
| `BOOKING_SERVICE_PORT`| `5102`                     | Port for the BookingMutationService                                |
| `PAYMENT_READ_SERVICE_PORT` | `5103`               | Port for the PaymentReadService                                    |
| `PAYMENT_WRITE_SERVICE_PORT`| `5104`               | Port for the PaymentWriteService                                   |

**Vault env vars** (required only when running with Vault):

| Variable           | Default                    | Description                                              |
|--------------------|----------------------------|----------------------------------------------------------|
| `VAULT_ADDR`       | `http://127.0.0.1:8200`    | Vault server address                                     |
| `VAULT_TOKEN`      | *(set by vault_helper.py)* | Vault token — set automatically after AppRole login      |
| `VAULT_DB_ROLE`    | *(set by vault_helper.py)* | Vault database role — set automatically per service      |
| `DB_HOST`          | *(parsed by start_app.sh)* | PostgreSQL host — parsed from `DATABASE_URL` at launch   |
| `DB_PORT`          | *(parsed by start_app.sh)* | PostgreSQL port — parsed from `DATABASE_URL` at launch   |
| `DB_NAME`          | *(parsed by start_app.sh)* | PostgreSQL database name — parsed from `DATABASE_URL`    |

**Keycloak env vars** (required only when running the Agentic IAM setup):

| Variable                   | Default                   | Description                                              |
|----------------------------|---------------------------|----------------------------------------------------------|
| `KEYCLOAK_BASE_URL`        | `http://127.0.0.1:8080`   | Keycloak base URL (used by setup scripts and services)   |
| `KEYCLOAK_ADMIN`           | `admin`                   | Keycloak admin username                                  |
| `KEYCLOAK_ADMIN_PASSWORD`  | `admin`                   | Keycloak admin password — read from env, never hardcoded |

## Debugging

### Enabling debug logging

Set `DEBUG=1` before starting the server and services to enable verbose structured logging
across all layers — the Flask orchestrator, the HTTP dispatcher, and every agent service.

```bash
# Start services with debug logging
DEBUG=1 ./services/start_services.sh

# Start orchestrator with debug logging (separate terminal)
DEBUG=1 uv run python app.py
```

Accepted values: `1`, `true`, `yes` (case-insensitive). Any other value (or unset) means
normal `WARNING`-level logging for services, `INFO`-level for the orchestrator.

### What gets logged at each layer

| Layer | File | What `DEBUG=1` adds |
|---|---|---|
| Orchestrator | `app.py` | Model, message count, Phase 1 Ollama status, tool name + args, tool result keys, Phase 2 message count |
| Dispatcher | `tools.py` | Service URL being called, HTTP status received |
| Each service | `agents/*_service.py` | Request token, tool name + args, db function called with args, db return value or exception traceback |

Each log line is JSON with `time`, `level`, and `logger` or `service` field, making them
easy to filter. Service logs are written to `services/logs/<name>.log`:

```bash
# Watch all service logs live
tail -f services/logs/*.log

# Show only errors across all service logs
grep '"level": "ERROR"' services/logs/*.log

# Show only orchestrator debug lines
DEBUG=1 uv run python app.py 2>&1 | grep '"level": "DEBUG"'
```

### Exercising a service directly

Each agent service exposes `POST /execute` with a Bearer token header. You can call one
directly with `curl` without running the full orchestrator:

```bash
# Simulate a cancel_booking call
curl -s -X POST http://127.0.0.1:5102/execute \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-token" \
  -d '{"tool":"cancel_booking","args":{"reference":"VT-00001"}}'

# Simulate a list_bookings call
curl -s -X POST http://127.0.0.1:5101/execute \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-token" \
  -d '{"tool":"list_bookings","args":{}}'

# Check a service health endpoint
curl -s http://127.0.0.1:5101/health
```

### Tracing a failed tool call end-to-end

With `DEBUG=1` you get the full trace from every layer. A typical `cancel_booking` failure
looks like:

```
# app.py — orchestrator
{"level": "DEBUG", "logger": "app", "message": "Dispatching tool: name=cancel_booking args={\"reference\": \"VT-00999\"}"}
{"level": "DEBUG", "logger": "tools", "message": "dispatch_tool: POST http://127.0.0.1:5102/execute tool=cancel_booking"}

# services/logs/booking_mutation_service.log — service log
{"level": "DEBUG", "service": "booking_mutation_service", "message": "tool=cancel_booking args={\"reference\": \"VT-00999\"}"}
{"level": "DEBUG", "service": "booking_mutation_service", "message": "Calling db.cancel_booking reference='VT-00999'"}
{"level": "WARNING","service": "booking_mutation_service", "message": "cancel_booking: reference='VT-00999' not found"}

# tools.py — dispatcher reports final result
{"level": "INFO",  "logger": "app", "message": "Tool dispatched: name=cancel_booking result_keys=['error']"}
```

## Agent architecture

Each tool call is dispatched to a dedicated, scoped **HTTP microservice** rather than executing
inline. This creates an explicit permission boundary per service that can be wired to an external
auth service without touching the orchestrator or the Ollama tool schemas.

```
POST /api/chat
  └─ app.py (orchestrator)
       └─ tools.dispatch_tool(name, args)
            └─ requests.post(<service>/execute, Authorization: Bearer <token>)
                 ├─ agents/read_service.py             :5101 — get_booking, list_bookings
                 ├─ agents/booking_mutation_service.py :5102 — create_booking, cancel_booking
                 ├─ agents/payment_read_service.py     :5103 — list_payment_cards
                 └─ agents/payment_write_service.py    :5104 — add/update/remove_payment_card
```

Each service:
1. Receives the tool name and args from the **POST `/execute` request body**
2. Reads the auth token from the **`Authorization: Bearer` request header**
3. Calls `validate_token(token)` — currently a stub that returns `True`
4. Dispatches to the appropriate `db.*` helper
5. Returns a JSON response (`{"result": ...}` or `{"error": "..."}`)

Each service also exposes `GET /health` → `{"status": "ok", "service": "<name>"}` used by
the launcher script to verify startup.

**To integrate a real auth service**, replace the body of `validate_token()` in the relevant
service file(s). The `AUTH_TOKEN` env var is the injection point — set it to the token your
auth service expects:

```python
# In any agents/*_service.py — current stub:
def validate_token(token: str) -> bool:
    # TODO: replace with external auth service call
    return True

# Future integration:
def validate_token(token: str) -> bool:
    resp = requests.post(AUTH_SERVICE_URL, json={"token": token}, timeout=5)
    return resp.json().get("valid", False)
```

## Vault secret management

The project demonstrates two layers of database credential management, both implemented and
runnable today.

### Layer 1: AppRole + dynamic DB credentials

Each microservice authenticates to Vault at startup using a pre-generated `role_id` /
`secret_id` pair (written to `scripts/vault_approle_creds/<service>.env` by
[`scripts/setup_vault.sh`](scripts/setup_vault.sh)).
On every `db.get_connection()` call the service requests a fresh, short-lived PostgreSQL
credential pair from Vault's database secrets engine. The Vault token is kept alive by a
background renewal thread in [`agents/vault_helper.py`](agents/vault_helper.py).

**Relevant files:** `scripts/setup_vault.sh`, `agents/vault_helper.py`, `db.py`, `start_app.sh`

Permission boundaries are enforced at the **PostgreSQL role level**:

| Service | Vault role | PostgreSQL grants |
|---|---|---|
| `read_service` | `voyager_read` | SELECT on bookings |
| `booking_mutation_service` | `voyager_booking_write` | SELECT, INSERT, UPDATE on bookings |
| `payment_read_service` | `voyager_payment_read` | SELECT on payment_cards |
| `payment_write_service` | `voyager_payment_write` | SELECT, INSERT, UPDATE, DELETE on payment_cards |

### Layer 2: Keycloak OIDC + Vault Agentic IAM

Built on top of Layer 1, this layer adds a Keycloak identity layer and wires Vault's OAuth
Resource Server and agent registry so that each service proves its identity via a
short-lived JWT rather than a pre-distributed `secret_id`.

**Relevant scripts:** `scripts/setup_keycloak_kcadm.sh`, `scripts/keycloak_ai_vault_integration.sh`,
`scripts/vault_agentic_keycloak_setup.sh`, `scripts/vault_register_agents.sh`

Key components configured by the scripts:

- **Keycloak `voyager` realm** — four confidential service-account clients (one per
  microservice) plus a `vault-oidc` client with an audience mapper pointed at Vault
- **Vault OAuth Resource Server profile** (`keycloak`) — tells Vault to validate tokens
  against Keycloak's JWKS endpoint with `jwt_type=access_token`
- **Ceiling policy** (`voyager-agent-ceiling`) — upper bound on all permissions any
  registered agent may ever exercise; scoped to the four `database/creds/*` paths
- **Agent registry entries** — one per service, each linking a Vault Identity entity
  (keyed on the Keycloak `sub` claim) to the ceiling policy

This shifts the permission boundary from the PostgreSQL role level toward the
**individual agent identity level**: Vault resolves the agent's Keycloak identity before
issuing any database credential.

See **[`vault-agentic-ai-plan.md`](vault-agentic-ai-plan.md)** for the full architecture
comparison and sub-task breakdown.

---

## Project layout

```
app.py                                  ← Flask orchestrator, tool-call loop, SSE streaming
db.py                                   ← PostgreSQL schema, seed data, CRUD helpers (Vault-aware)
tools.py                                ← Ollama tool schemas + HTTP dispatcher
agents/
  vault_helper.py                       ← AppRole login + background token renewal (Vault helper)
  read_service.py                       ← get_booking, list_bookings         :5101 [read-only]
  booking_mutation_service.py           ← create_booking, cancel_booking     :5102 [write: bookings]
  payment_read_service.py               ← list_payment_cards                 :5103 [read: payments]
  payment_write_service.py              ← add/update/remove_payment_card     :5104 [write: payments]
services/
  start_services.sh                     ← Start all 4 agent services (no-Vault path)
  stop_services.sh                      ← Stop all 4 agent services
  logs/                                 ← Per-service stdout/stderr logs (gitignored)
  pids/                                 ← Per-service PID files (gitignored)
scripts/
  setup_all.sh                          ← One-shot orchestrator: runs all 6 setup steps in order
  setup_postgres.sh                     ← Start PostgreSQL container, wait for readiness
  setup_keycloak_kcadm.sh              ← Start Keycloak, create voyager realm + service-account clients
  keycloak_ai_vault_integration.sh     ← Add vault-oidc client + audience mapper to voyager realm
  setup_vault.sh                        ← DB secrets engine, roles, policies, AppRole credentials
  vault_agentic_keycloak_setup.sh      ← Vault OAuth Resource Server profile + ceiling policy
  vault_register_agents.sh             ← Per-agent Identity entity, alias, and registry entry
  vault_approle_creds/                  ← Per-service AppRole .env files (gitignored, written by setup_vault.sh)
static/
  index.html                            ← Single-page chat UI with bookings sidebar (vanilla JS)
start_app.sh                            ← Launch all 5 processes with Vault AppRole credentials
pyproject.toml                          ← Project metadata and dependencies (uv)
uv.lock                                 ← Pinned dependency lockfile
.python-version                         ← Python version pin (3.12)
vault-integration-plan.md              ← Plan: legacy AppRole + DB secrets engine (complete)
vault-agentic-ai-plan.md               ← Plan: Vault 2.1 Agentic AI migration
keycloak-vault-oidc-plan.md            ← Plan: Keycloak OIDC + Vault Agentic IAM (complete)
```

## API

| Method | Path                  | Description                           |
|--------|-----------------------|---------------------------------------|
| GET    | `/`                   | Serves the chat UI                    |
| GET    | `/api/models`         | Lists locally available Ollama models |
| POST   | `/api/chat`           | Streams a chat response (SSE)         |
| GET    | `/api/bookings`       | Returns all bookings as JSON          |
| GET    | `/api/payment-cards`  | Returns all payment cards as JSON     |

### POST `/api/chat` body

```json
{
  "model": "llama3.2:latest",
  "messages": [
    { "role": "user", "content": "Hello!" }
  ]
}
```

Response is a stream of `text/event-stream` lines:

```
data: {"content": "Hi", "done": false}
data: {"content": " there!", "done": false}
data: {"content": "", "done": true}
```
