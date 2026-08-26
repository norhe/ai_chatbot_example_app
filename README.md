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

# 4. Run the server
uv run python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Configuration

All options are controlled via environment variables:

| Variable          | Default                    | Description                                          |
|-------------------|----------------------------|------------------------------------------------------|
| `DATABASE_URL`    | *(required)*               | PostgreSQL connection string                         |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434`  | Ollama API base URL                                  |
| `OLLAMA_MODEL`    | `llama3.2:latest`          | Default model (overridable in UI)                    |
| `REQUEST_TIMEOUT` | `120`                      | Seconds before streaming times out                   |
| `AUTH_TOKEN`      | `dev-token`                | Token passed to each agent for authorization checks  |
| `DEBUG`           | *(unset)*                  | Set to `1` to enable verbose debug logging           |

## Debugging

### Enabling debug logging

Set `DEBUG=1` before starting the server to enable verbose structured logging across all
layers — the Flask orchestrator, the subprocess dispatcher, and every agent script.

```bash
DEBUG=1 uv run python app.py
```

Accepted values: `1`, `true`, `yes` (case-insensitive). Any other value (or unset) means
normal `INFO`-level logging.

### What gets logged at each layer

| Layer | File | What `DEBUG=1` adds |
|---|---|---|
| Orchestrator | `app.py` | Model, message count, Phase 1 Ollama status, tool name + args, tool result keys, Phase 2 message count |
| Dispatcher | `tools.py` | Agent script being spawned, subprocess exit code, full stdout/stderr (up to 500 chars) |
| Each agent | `agents/*.py` | Raw stdin payload, token validation, db function called with args, db return value or exception traceback |

Each log line is JSON with `time`, `level`, `logger` (orchestrator/dispatcher) or `agent`
(agent scripts) fields, making them easy to filter:

```bash
# Show only agent-level debug lines
DEBUG=1 uv run python app.py 2>&1 | grep '"level": "DEBUG"'

# Show only errors across all layers
DEBUG=1 uv run python app.py 2>&1 | grep '"level": "ERROR"'
```

> **Note:** Agent logs are written to `stderr`. The stdout of each agent subprocess is the
> raw JSON result that the dispatcher parses — mixing log output into stdout would break the
> wire protocol. When running the full server, agent stderr is captured by the dispatcher and
> included in error messages on failure; with `DEBUG=1` it also appears in the server logs.

### Exercising an agent directly

Each agent script reads a JSON payload from stdin and writes a result to stdout. You can invoke
one directly without running the server — useful for isolating a broken tool call:

```bash
# Simulate a cancel_booking call
echo '{"tool":"cancel_booking","args":{"reference":"VT-00001"},"auth_token":"dev-token"}' \
  | DEBUG=1 DATABASE_URL="postgresql://voyager:voyager@127.0.0.1:5432/voyager" \
    uv run python agents/booking_mutation_agent.py

# Simulate a list_bookings call
echo '{"tool":"list_bookings","args":{},"auth_token":"dev-token"}' \
  | DEBUG=1 DATABASE_URL="postgresql://voyager:voyager@127.0.0.1:5432/voyager" \
    uv run python agents/read_agent.py

# Simulate adding a payment card
echo '{"tool":"add_payment_card","args":{"cardholder_name":"Jane Smith","card_number":"4111 1111 1111 1111","card_brand":"Visa","expiry":"12/2027","billing_address":"1 Main St"},"auth_token":"dev-token"}' \
  | DEBUG=1 DATABASE_URL="postgresql://voyager:voyager@127.0.0.1:5432/voyager" \
    uv run python agents/payment_write_agent.py
```

Each agent prints its result to stdout and debug trace to stderr, so you see both streams
interleaved in the terminal.

### Tracing a failed tool call end-to-end

When a tool call fails in the running server, the dispatcher logs the agent's stderr. With
`DEBUG=1` you get the full trace from every layer. A typical `cancel_booking` failure
looks like:

```
# app.py — orchestrator
{"level": "DEBUG", "logger": "app", "message": "Dispatching tool: name=cancel_booking args={\"reference\": \"VT-00999\"}"}
{"level": "DEBUG", "logger": "tools", "message": "dispatch_tool: spawning agent=agents/booking_mutation_agent.py tool=cancel_booking"}

# booking_mutation_agent.py — agent stderr (captured and re-logged by dispatcher)
{"level": "DEBUG", "agent": "booking_mutation_agent", "message": "tool=cancel_booking args={\"reference\": \"VT-00999\"}"}
{"level": "DEBUG", "agent": "booking_mutation_agent", "message": "Calling db.cancel_booking reference='VT-00999'"}
{"level": "WARNING","agent": "booking_mutation_agent", "message": "cancel_booking: reference='VT-00999' not found"}

# tools.py — dispatcher reports final result
{"level": "INFO",  "logger": "app", "message": "Tool dispatched: name=cancel_booking result_keys=['error']"}
```

## Agent architecture

Each tool call is dispatched to a dedicated, scoped **subprocess agent** rather than executing
inline. This creates an explicit permission boundary per agent that can be wired to an external
auth service without touching the orchestrator or the Ollama tool schemas.

```
POST /api/chat
  └─ app.py (orchestrator)
       └─ tools.dispatch_tool(name, args)
            └─ subprocess.run(agent_script, stdin={"tool","args","auth_token"})
                 ├─ agents/read_agent.py            — get_booking, list_bookings
                 ├─ agents/booking_mutation_agent.py — create_booking, cancel_booking
                 ├─ agents/payment_read_agent.py     — list_payment_cards
                 └─ agents/payment_write_agent.py    — add/update/remove_payment_card
```

Each agent:
1. Reads the JSON payload from **stdin**
2. Calls `validate_token(token)` — currently a stub that returns `True`
3. Dispatches to the appropriate `db.*` helper
4. Writes a JSON result to **stdout** (`{"result": ...}` or `{"error": "..."}`)

**To integrate a real auth service**, replace the body of `validate_token()` in the relevant
agent file(s). The `AUTH_TOKEN` env var is the injection point — set it to the token your auth
service expects:

```python
# In any agents/*.py — current stub:
def validate_token(token: str) -> bool:
    # TODO: replace with external auth service call
    return True

# Future integration:
def validate_token(token: str) -> bool:
    resp = requests.post(AUTH_SERVICE_URL, json={"token": token}, timeout=5)
    return resp.json().get("valid", False)
```

## Project layout

```
app.py                             ← Flask backend, tool-call loop, SSE streaming
db.py                              ← PostgreSQL schema, seed data, CRUD helpers
tools.py                           ← Ollama tool schemas + subprocess dispatcher
agents/
  read_agent.py                    ← get_booking, list_bookings         [read-only]
  booking_mutation_agent.py        ← create_booking, cancel_booking     [write: bookings]
  payment_read_agent.py            ← list_payment_cards                 [read: payments]
  payment_write_agent.py           ← add/update/remove_payment_card     [write: payments]
static/
  index.html                       ← Single-page chat UI with bookings sidebar (vanilla JS)
pyproject.toml                     ← Project metadata and dependencies (uv)
uv.lock                            ← Pinned dependency lockfile
.python-version                    ← Python version pin (3.12)
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
