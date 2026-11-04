> **Phase 1 — Monolith** — part of the Voyager Travel training series (see [root README](../README.md)).
>
> All agent/tool code runs inside `app.py`'s process. `tools.dispatch_tool()` calls `db.py` directly, so it is the simplest possible design — but there is **no boundary between the LLM-facing tool code and the database credential**. Every tool shares one connection string and one process, so access cannot be restricted per agent.
>
> Next: [Phase 2](../phase2-microservices/README.md) splits the tools into separate agent services.

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
  -v "$(pwd)/postgres-data:/var/lib/postgresql/data" \
  registry.redhat.io/rhel9/postgresql-16:latest
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
  -v "$(pwd)/postgres-data:/var/lib/postgresql/data:Z" \
  registry.redhat.io/rhel9/postgresql-16:latest
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

| Variable          | Default                    | Description                         |
|-------------------|----------------------------|-------------------------------------|
| `DATABASE_URL`    | *(required)*               | PostgreSQL connection string        |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434`  | Ollama API base URL                 |
| `OLLAMA_MODEL`    | `llama3.2:latest`          | Default model (overridable in UI)   |
| `REQUEST_TIMEOUT` | `120`                      | Seconds before streaming times out  |

## Project layout

```
app.py             ← Flask backend, tool-call loop, SSE streaming
db.py              ← PostgreSQL schema, seed data, CRUD helpers
tools.py           ← Ollama tool schemas and dispatcher
static/
  index.html       ← Single-page chat UI with bookings sidebar (vanilla JS)
pyproject.toml     ← Project metadata and dependencies (uv)
uv.lock            ← Pinned dependency lockfile
.python-version    ← Python version pin (3.12)
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
