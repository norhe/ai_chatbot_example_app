# Microservice Migration Plan

## Overview

Convert the four subprocess-based agents to independent HTTP microservices. Each agent becomes a
long-running Flask application listening on its own local port. The orchestrator in `tools.py`
swaps `subprocess.run` for `requests.post`, moving the auth token from the stdin payload body to
a standard `Authorization: Bearer` header. Agent business logic (validate_token, db calls, result
shape) is unchanged.

**Scope:**
- Convert 4 agent scripts in `agents/` from stdin/stdout scripts to Flask microservices
- Replace `subprocess.run` dispatch in `tools.py` with `requests.post` HTTP dispatch
- Replace `TOOL_AGENT_MAP` script paths with `AGENT_URLS` endpoint map
- Add a `services/` directory with a `start_services.sh` launcher script
- Update `pyproject.toml` to add no new dependencies (Flask is already a dependency)
- Update `README.md` with new startup instructions and service port table

**Non-goals:**
- Containerisation (Docker/Podman) — out of scope, handled separately
- TLS between orchestrator and services — localhost-only, no TLS required
- Per-agent PostgreSQL roles — compatible with this change but handled separately
- Changes to `app.py`, `db.py`, the frontend, or the Ollama tool schemas

**Port assignments:**

| Service | Script | Port |
|---|---|---|
| ReadService | `agents/read_service.py` | 5101 |
| BookingMutationService | `agents/booking_mutation_service.py` | 5102 |
| PaymentReadService | `agents/payment_read_service.py` | 5103 |
| PaymentWriteService | `agents/payment_write_service.py` | 5104 |

---

## Sub-Tasks

---

### Sub-Task 1 — Convert agent scripts to Flask services (`agents/`)

**Status:** `[ ] pending`

**Intent:**
Replace each `agents/*.py` stdin/stdout script with a Flask HTTP service. The business logic
(validate_token, db dispatch, error handling) is identical — only the I/O layer changes. Each
service exposes a single `POST /execute` endpoint that accepts JSON and returns JSON.

**Expected Outcomes:**
- 4 new service files: `read_service.py`, `booking_mutation_service.py`,
  `payment_read_service.py`, `payment_write_service.py` in `agents/`
- Original `*_agent.py` scripts are **removed** — replaced entirely by the service files
- Each service:
  - Reads tool name and args from the POST body JSON
  - Reads the auth token from the `Authorization: Bearer <token>` request header
  - Calls `validate_token(token)` — stub unchanged, same TODO comment
  - Dispatches to `db.*` helpers — identical to the current agent scripts
  - Returns `{"result": ...}` on success, `{"error": "..."}` on failure
  - Returns HTTP 401 if auth fails, HTTP 400 on bad input, HTTP 200 on tool success or tool error
  - Binds to `127.0.0.1` only (never `0.0.0.0`)
  - Port is read from `<SERVICE_NAME>_PORT` env var with a hardcoded default (see port table above)

**Service template each file must follow:**

```python
import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from flask import Flask, request, jsonify

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "<name>", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

def validate_token(token: str) -> bool:
    # TODO: replace with external auth service call
    # e.g. return requests.post(AUTH_SERVICE_URL, json={"token": token}).json()["valid"]
    logger.debug("validate_token: token=%r", token)
    return True

@app.route("/execute", methods=["POST"])
def execute():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not validate_token(token):
        logger.warning("Auth failed for token=%r", token)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "tool" not in data:
        return jsonify({"error": "Invalid request: 'tool' field required"}), 400

    tool = data["tool"]
    args = data.get("args", {})
    logger.debug("tool=%s args=%s", tool, json.dumps(args))

    # ... dispatch by tool name, identical to current agent scripts ...

    logger.error("Unknown tool: %s", tool)
    return jsonify({"error": f"Unknown tool: {tool}"}), 400

if __name__ == "__main__":
    port = int(os.environ.get("<SERVICE_NAME>_PORT", <default_port>))
    app.run(host="127.0.0.1", port=port)
```

**Key differences from the current agent scripts:**
- `main()` → `@app.route("/execute")` Flask handler
- `sys.stdin.read()` → `request.get_json()`
- `payload.get("auth_token")` → `request.headers.get("Authorization").removeprefix("Bearer ")`
- `print(json.dumps(...))` + `sys.exit(1)` → `return jsonify(...), <status_code>`
- `logging.basicConfig(stream=sys.stderr, ...)` → standard Flask logging (stdout)

**Logging behaviour:**
- Debug logging controlled by `DEBUG` env var — same pattern as the current agents
- `"service"` field in log format instead of `"agent"` for clarity
- No need to write to stderr specifically (Flask services don't use stdout as a data channel)

**Todo list:**
- [ ] Create `agents/read_service.py` — handles `get_booking`, `list_bookings`, port 5101
- [ ] Create `agents/booking_mutation_service.py` — handles `create_booking`, `cancel_booking`, port 5102
- [ ] Create `agents/payment_read_service.py` — handles `list_payment_cards`, port 5103
- [ ] Create `agents/payment_write_service.py` — handles `add_payment_card`, `update_payment_card`, `remove_payment_card`, port 5104
- [ ] Delete `agents/read_agent.py`, `agents/booking_mutation_agent.py`, `agents/payment_read_agent.py`, `agents/payment_write_agent.py`
- [ ] Syntax-check all 4 new service files
- [ ] Verify each service starts and `/execute` responds to a request:
  ```bash
  uv run python agents/read_service.py &
  sleep 1
  curl -s -X POST http://127.0.0.1:5101/execute \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer dev-token" \
    -d '{"tool":"list_bookings","args":{}}'
  kill %1
  ```

**Relevant context:**
- `db.py` is at the project root — same `sys.path.insert` pattern required
- All db call logic, None-handling, and exception wrapping is identical to the current agents
- `flask-cors` is not needed on the agent services — they are only called by the orchestrator,
  never directly by the browser

---

### Sub-Task 2 — Update orchestrator (`tools.py`)

**Status:** `[ ] pending`

**Intent:**
Replace the `subprocess.run` dispatcher with an HTTP dispatcher using `requests.post`. The
`TOOL_AGENT_MAP` of script paths becomes `AGENT_URLS` of service base URLs. The `dispatch_tool`
signature is unchanged — `app.py` requires no modifications.

**Expected Outcomes:**
- `TOOL_AGENT_MAP` is renamed to `AGENT_URLS` and maps tool names to service base URLs
- `dispatch_tool()` calls `requests.post(url, json=payload, headers={"Authorization": ...})`
- Auth token moves from the JSON body to the `Authorization: Bearer` header
- HTTP 401 response is mapped to `{"error": "Unauthorized"}`
- Non-200 response is mapped to `{"error": "Agent service error: HTTP <status>"}`
- `requests.Timeout` is mapped to `{"error": "Agent timed out"}`
- `subprocess`, `sys` (for `sys.executable`) imports are removed; `requests` is already imported
- Debug logging updated to log the URL being called and HTTP status received

**`AGENT_URLS` structure:**
```python
_AGENT_BASE = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1")

AGENT_URLS = {
    "get_booking":          f"{_AGENT_BASE}:5101",
    "list_bookings":        f"{_AGENT_BASE}:5101",
    "create_booking":       f"{_AGENT_BASE}:5102",
    "cancel_booking":       f"{_AGENT_BASE}:5102",
    "list_payment_cards":   f"{_AGENT_BASE}:5103",
    "add_payment_card":     f"{_AGENT_BASE}:5104",
    "update_payment_card":  f"{_AGENT_BASE}:5104",
    "remove_payment_card":  f"{_AGENT_BASE}:5104",
}
```

**`dispatch_tool` implementation:**
```python
def dispatch_tool(name: str, args: dict) -> dict:
    base_url = AGENT_URLS.get(name)
    if not base_url:
        logger.error("dispatch_tool: no service URL for tool=%s", name)
        return {"error": f"Unknown tool: {name}"}

    token = os.environ.get("AUTH_TOKEN", "dev-token")
    logger.debug("dispatch_tool: POST %s/execute tool=%s", base_url, name)

    try:
        resp = requests.post(
            f"{base_url}/execute",
            json={"tool": name, "args": args},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        logger.debug("dispatch_tool: service=%s status=%d", base_url, resp.status_code)
        if resp.status_code == 401:
            return {"error": "Unauthorized"}
        if resp.status_code != 200:
            logger.error("dispatch_tool: service=%s HTTP %d body=%s",
                         base_url, resp.status_code, resp.text[:500])
            return {"error": f"Agent service error: HTTP {resp.status_code}"}
        return resp.json()
    except requests.Timeout:
        logger.error("dispatch_tool: service=%s timed out", base_url)
        return {"error": "Agent timed out"}
    except requests.RequestException as e:
        logger.error("dispatch_tool: service=%s connection error: %s", base_url, e)
        return {"error": f"Agent unreachable: {e}"}
    except Exception as e:
        logger.exception("dispatch_tool: unexpected error for service=%s", base_url)
        return {"error": str(e)}
```

**Todo list:**
- [ ] Rename `TOOL_AGENT_MAP` to `AGENT_URLS` with service base URLs
- [ ] Add `_AGENT_BASE` env var read for future containerised deployments
- [ ] Replace `dispatch_tool()` body with `requests.post` implementation
- [ ] Remove `subprocess` and `sys` imports (no longer needed)
- [ ] Update debug log messages to reflect URL-based dispatch
- [ ] Syntax-check: `uv run python -m py_compile tools.py`
- [ ] Verify tool count unchanged: `uv run python -c "from tools import AGENT_URLS; assert len(AGENT_URLS) == 8"`

**Relevant context:**
- `requests` is already in `pyproject.toml` — no new dependency
- `app.py` calls `tools.dispatch_tool(name, args)` — this signature must not change
- `AGENT_BASE_URL` env var allows pointing all services at a different host (e.g. a container
  network) without code changes — future-proofs container and remote deployments

---

### Sub-Task 3 — Service launcher (`services/start_services.sh`)

**Status:** `[ ] pending`

**Intent:**
Provide a single shell script that starts all four agent services in the background and a
`stop_services.sh` that shuts them down cleanly. This replaces having to open four terminal
windows and makes the demo runnable with two commands.

**Expected Outcomes:**
- `services/start_services.sh` starts all 4 services, writes each PID to `services/pids/`
- `services/stop_services.sh` kills all PIDs recorded in `services/pids/`
- Each service's stdout/stderr is redirected to `services/logs/<service_name>.log`
- The start script waits up to 5 seconds for each service to become healthy before continuing
  (health check: `curl -sf http://127.0.0.1:<port>/health`)
- Each service exposes a `GET /health` endpoint that returns `{"status": "ok"}`
- If a service fails to start, the script prints an error and exits non-zero

**Start script structure:**
```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p services/pids services/logs

start_service() {
    local name=$1 script=$2 port=$3
    uv run python "$script" > "services/logs/${name}.log" 2>&1 &
    echo $! > "services/pids/${name}.pid"
    # wait for /health
    for i in $(seq 1 10); do
        curl -sf "http://127.0.0.1:${port}/health" > /dev/null && break
        sleep 0.5
    done || { echo "ERROR: $name failed to start"; exit 1; }
    echo "  started $name on :$port (PID $(cat services/pids/${name}.pid))"
}

start_service read_service           agents/read_service.py           5101
start_service booking_mutation_service agents/booking_mutation_service.py 5102
start_service payment_read_service   agents/payment_read_service.py   5103
start_service payment_write_service  agents/payment_write_service.py  5104
```

**Todo list:**
- [ ] Create `services/` directory
- [ ] Add `GET /health` route returning `{"status": "ok", "service": "<name>"}` to all 4 service files
- [ ] Create `services/start_services.sh` with health-check loop
- [ ] Create `services/stop_services.sh` reading PIDs from `services/pids/`
- [ ] Make both scripts executable (`chmod +x`)
- [ ] Add `services/pids/` and `services/logs/` to `.gitignore`
- [ ] Test: `./services/start_services.sh && curl http://127.0.0.1:5101/health`

**Relevant context:**
- `uv run python` must be used (not bare `python`) to pick up the venv
- The health route must be added to service files before the launcher is tested
- `services/logs/` is the right place to look when `DEBUG=1` is set for individual services

---

### Sub-Task 4 — Update `README.md`

**Status:** `[ ] pending`

**Intent:**
Update the README to reflect the new startup sequence, service port table, `AGENT_BASE_URL`
config entry, and revised debugging instructions.

**Expected Outcomes:**
- Quick start updated: `./services/start_services.sh` added before `uv run python app.py`
- Configuration table gains `AGENT_BASE_URL` row
- Agent architecture section updated to show HTTP transport and Bearer token
- Debugging section updated: direct agent invocation commands replaced with `curl` examples
- Project layout updated: `agents/*_agent.py` replaced with `agents/*_service.py`,
  `services/` directory listed

**Todo list:**
- [ ] Update Quick start to include `./services/start_services.sh` step
- [ ] Add `AGENT_BASE_URL` to Configuration table
- [ ] Update Agent architecture diagram to show HTTP + Bearer token
- [ ] Replace agent stdin/stdout invocation examples with `curl` equivalents
- [ ] Update Project layout block

**Relevant context:**
- The full startup sequence after migration:
  1. Start PostgreSQL
  2. `uv sync`
  3. `export DATABASE_URL=...`
  4. `uv run python db.py` (first run only)
  5. `./services/start_services.sh`
  6. `uv run python app.py`

---

## Architecture comparison

```
Before (subprocess)
───────────────────
tools.dispatch_tool
  └─ subprocess.run(agents/read_agent.py)
       stdin:  {"tool": ..., "args": ..., "auth_token": "..."}
       stdout: {"result": ...}


After (microservice)
────────────────────
tools.dispatch_tool
  └─ requests.post("http://127.0.0.1:5101/execute")
       body:    {"tool": ..., "args": ...}
       header:  Authorization: Bearer <token>
       response: {"result": ...}


Unchanged across both
─────────────────────
app.py          — zero changes
db.py           — zero changes
TOOL_DEFINITIONS — zero changes (Ollama sees nothing different)
validate_token() — same stub, same TODO replacement point
db.* dispatch   — identical logic in each service
Result shape    — {"result": ...} / {"error": "..."}
```
