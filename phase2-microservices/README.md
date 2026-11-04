> **Phase 2 — Agent Microservices** — part of the Voyager Travel training series (see [root README](../README.md)).
>
> Each tool group is its own HTTP service (ports 5101–5104) with a `/execute` endpoint and a `validate_token()` hook. This creates the **per-agent boundary** where security can be applied — but nothing is enforced yet: `validate_token()` is a stub and all services share one `DATABASE_URL`.
>
> Next: [Phase 3](../phase3-vault-approle/README.md) adds HashiCorp Vault with AppRole.

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

PostgreSQL 14+ is required. The app connects via a `DATABASE_URL` environment variable and creates
its schema automatically on first start — no manual SQL needed.

#### Run with Docker

Data is stored in a local `./postgres-data` directory so it survives container restarts.

```bash
mkdir -p ./postgres-data

docker run -d \
  --name voyager-postgres \
  -e POSTGRES_USER=voyager \
  -e POSTGRES_PASSWORD=voyager \
  -e POSTGRES_DB=voyager \
  -p 127.0.0.1:5432:5432 \
  -v "$(pwd)/postgres-data:/var/lib/postgresql" \
  postgres:latest
```

#### Run with Podman

```bash
mkdir -p ./postgres-data

podman run -d \
  --name voyager-postgres \
  -e POSTGRES_USER=voyager \
  -e POSTGRES_PASSWORD=voyager \
  -e POSTGRES_DB=voyager \
  -p 127.0.0.1:5432:5432 \
  -v "$(pwd)/postgres-data:/var/lib/postgresql:Z" \
  postgres:latest
```

> The `:Z` volume flag is required on SELinux-enforcing systems (Fedora, RHEL, etc.) to relabel the
> host directory so the container can write to it.

#### Stop / restart

```bash
# Docker
docker stop voyager-postgres
docker start voyager-postgres

# Podman
podman stop voyager-postgres
podman start voyager-postgres
```

#### Connect to the database (after the application has run at least once to seed the db)

```bash
# Podman
podman exec -it voyager-postgres psql -U voyager -d voyager

```

## Quick start

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

## Security limitations (what Phase 3 fixes)

The agents are now separate processes, but every one of them still connects to PostgreSQL with
the **same `DATABASE_URL` credential**, and `validate_token()` is a stub that returns `True`.
A compromised `read_service` can therefore still write to or delete from any table. Phase 3 gives
each service its own short-lived, least-privilege database credential via Vault.

---

## Project layout

```
app.py                                  ← Flask orchestrator, tool-call loop, SSE streaming
db.py                                   ← PostgreSQL schema, seed data, CRUD helpers 
tools.py                                ← Ollama tool schemas + HTTP dispatcher
agents/
  read_service.py                       ← get_booking, list_bookings         :5101 [read-only]
  booking_mutation_service.py           ← create_booking, cancel_booking     :5102 [write: bookings]
  payment_read_service.py               ← list_payment_cards                 :5103 [read: payments]
  payment_write_service.py              ← add/update/remove_payment_card     :5104 [write: payments]
services/
  start_services.sh                     ← Start all 4 agent services
  stop_services.sh                      ← Stop all 4 agent services
  logs/                                 ← Per-service stdout/stderr logs (gitignored)
  pids/                                 ← Per-service PID files (gitignored)
static/
  index.html                            ← Single-page chat UI with bookings sidebar (vanilla JS)
pyproject.toml                          ← Project metadata and dependencies (uv)
.python-version                         ← Python version pin (3.12)
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
