# Sub-Agent Architecture Plan

## Overview

Refactor the Voyager Travel booking agent so that every tool execution is dispatched to a
dedicated, scoped subprocess agent rather than executing inline in the Flask process. Each agent
owns exactly the tools appropriate to its privilege level, validates an auth token before executing,
and communicates with the orchestrator via stdin/stdout JSON. This creates clean, auditable
permission boundaries that can be wired to a real external auth system in a future iteration.

**Approach:** Option A — `subprocess.run` per tool call (one Python script per agent, spawned on
demand, communicates via stdin/stdout JSON).

**Four agents and their tool scope:**

| Agent | Script | Tools |
|---|---|---|
| ReadAgent | `agents/read_agent.py` | `get_booking`, `list_bookings` |
| BookingMutationAgent | `agents/booking_mutation_agent.py` | `create_booking`, `cancel_booking` |
| PaymentReadAgent | `agents/payment_read_agent.py` | `list_payment_cards` |
| PaymentWriteAgent | `agents/payment_write_agent.py` | `add_payment_card`, `update_payment_card`, `remove_payment_card` |

**Auth stub:** Each agent exposes `validate_token(token: str) -> bool` which returns `True` for
now. A clearly marked `TODO` comment shows exactly where the external auth call replaces it.

**Wire protocol (stdin → stdout):**
```
# orchestrator → agent (stdin):
{"tool": "get_booking", "args": {"reference": "VT-00001"}, "auth_token": "..."}

# agent → orchestrator (stdout):
{"result": {...}}          # success
{"error": "..."}           # tool failure or auth failure
```

**Non-goals:**
- Real auth — stub only
- Process pooling or reuse — a fresh subprocess per call is intentional for isolation
- Changes to the DB schema, frontend, or API endpoints

---

## Sub-Tasks

---

### Sub-Task 1 — Agent scripts (`agents/`)

**Status:** `[ ] pending`

**Intent:**
Create the `agents/` directory and four agent scripts. Each script is a self-contained Python
program: reads a JSON payload from stdin, validates the token, dispatches to the appropriate
`db.py` helper, and writes a JSON result to stdout. No agent imports `tools.py` or `app.py`.

**Expected Outcomes:**
- `agents/__init__.py` exists (empty, makes the directory a package for import clarity)
- Four agent scripts exist, each handling only their scoped tools
- Each script has a `validate_token(token) -> bool` stub with a prominent TODO comment
- Auth failure writes `{"error": "Unauthorized"}` to stdout and exits with code 1
- Unknown tool name writes `{"error": "Unknown tool: <name>"}` to stdout and exits with code 1
- All date/datetime values in results are converted to ISO strings before JSON serialisation
- Each script can be run directly for manual testing:
  ```bash
  echo '{"tool":"get_booking","args":{"reference":"VT-00001"},"auth_token":"test"}' \
    | uv run python agents/read_agent.py
  ```

**Agent → tool mapping:**

`agents/read_agent.py`:
- `get_booking(reference)` → `db.get_booking`
- `list_bookings(passenger_name=None)` → `db.list_bookings`

`agents/booking_mutation_agent.py`:
- `create_booking(passenger_name, destination, origin, travel_date, return_date=None)` → `db.create_booking`
- `cancel_booking(reference)` → `db.cancel_booking`

`agents/payment_read_agent.py`:
- `list_payment_cards()` → `db.list_payment_cards`

`agents/payment_write_agent.py`:
- `add_payment_card(cardholder_name, card_number, card_brand, expiry, billing_address)` → `db.add_payment_card`
- `update_payment_card(card_id, ...)` → `db.update_payment_card`
- `remove_payment_card(card_id)` → `db.remove_payment_card`

**Shared pattern each script must follow:**
```python
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # find db.py
import db

def validate_token(token: str) -> bool:
    # TODO: replace with external auth service call
    # e.g. return requests.post(AUTH_SERVICE_URL, json={"token": token}).json()["valid"]
    return True

def main():
    payload = json.loads(sys.stdin.read())
    if not validate_token(payload.get("auth_token", "")):
        print(json.dumps({"error": "Unauthorized"}))
        sys.exit(1)
    tool = payload["tool"]
    args = payload.get("args", {})
    # dispatch ...
    print(json.dumps(result))

if __name__ == "__main__":
    main()
```

**Todo list:**
- [ ] Create `agents/` directory and `agents/__init__.py`
- [ ] Create `agents/read_agent.py` with `validate_token` stub and dispatch for `get_booking`, `list_bookings`
- [ ] Create `agents/booking_mutation_agent.py` with `validate_token` stub and dispatch for `create_booking`, `cancel_booking`
- [ ] Create `agents/payment_read_agent.py` with `validate_token` stub and dispatch for `list_payment_cards`
- [ ] Create `agents/payment_write_agent.py` with `validate_token` stub and dispatch for `add_payment_card`, `update_payment_card`, `remove_payment_card`
- [ ] Manually verify each agent script is importable and handles unknown tool names gracefully

**Relevant context:**
- `db.py` is at the project root; agents use `sys.path.insert` to find it
- `db.get_booking` returns `None` on not-found — agent must convert this to `{"error": "Booking not found"}`
- `db.cancel_booking` returns `None` on not-found — same pattern
- `db.update_payment_card` returns `None` on not-found — `{"error": "Card not found"}`
- `db.remove_payment_card` returns `bool` — convert to `{"result": "Card removed"}` / `{"error": "Card not found"}`
- All exceptions from `db.*` calls must be caught; return `{"error": str(e)}`

---

### Sub-Task 2 — Orchestrator dispatch (`tools.py`)

**Status:** `[ ] pending`

**Intent:**
Replace the inline `dispatch_tool()` implementation in `tools.py` with a subprocess-based
dispatcher. The tool-to-agent routing table and the subprocess invocation live here. `app.py`
is unchanged — it still calls `tools.dispatch_tool(name, args)` exactly as before.

**Expected Outcomes:**
- `TOOL_DEFINITIONS` list is unchanged (all 7 schemas intact)
- `dispatch_tool(name, args)` now spawns the correct agent subprocess instead of calling `db.*` directly
- An `auth_token` is passed in the stdin payload (sourced from `AUTH_TOKEN` env var, defaulting to `"dev-token"` for local use)
- The function reads stdout from the subprocess and returns the parsed JSON dict
- If the subprocess exits non-zero or stdout is not valid JSON, return `{"error": "Agent execution failed"}`
- A `TOOL_AGENT_MAP` dict maps each tool name to its agent script path — makes routing explicit and easy to extend

**`TOOL_AGENT_MAP` structure:**
```python
TOOL_AGENT_MAP = {
    "get_booking":          "agents/read_agent.py",
    "list_bookings":        "agents/read_agent.py",
    "create_booking":       "agents/booking_mutation_agent.py",
    "cancel_booking":       "agents/booking_mutation_agent.py",
    "list_payment_cards":   "agents/payment_read_agent.py",
    "add_payment_card":     "agents/payment_write_agent.py",
    "update_payment_card":  "agents/payment_write_agent.py",
    "remove_payment_card":  "agents/payment_write_agent.py",
}
```

**`dispatch_tool` implementation pattern:**
```python
def dispatch_tool(name: str, args: dict) -> dict:
    agent_script = TOOL_AGENT_MAP.get(name)
    if not agent_script:
        return {"error": f"Unknown tool: {name}"}

    payload = json.dumps({
        "tool": name,
        "args": args,
        "auth_token": os.environ.get("AUTH_TOKEN", "dev-token"),
    })

    result = subprocess.run(
        [sys.executable, agent_script],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        return {"error": "Agent execution failed"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "Agent returned invalid response"}
```

**Todo list:**
- [ ] Add `import subprocess, sys, os, json` to `tools.py`
- [ ] Define `TOOL_AGENT_MAP` dict
- [ ] Replace the body of `dispatch_tool()` with subprocess invocation logic
- [ ] Remove all direct `import db` and `db.*` calls from `tools.py` (db is now only used inside agents)
- [ ] Verify `TOOL_DEFINITIONS` list is untouched
- [ ] Syntax-check: `uv run python -m py_compile tools.py`

**Relevant context:**
- `app.py` calls `tools.dispatch_tool(name, args)` — this signature must not change
- `sys.executable` ensures the same Python interpreter/venv is used for the subprocess
- Agent scripts use `sys.path.insert` to find `db.py`, so working directory doesn't matter
- `AUTH_TOKEN` env var is the injection point for the future external auth integration

---

### Sub-Task 3 — Update README

**Status:** `[ ] pending`

**Intent:**
Document the new agent architecture, the `AUTH_TOKEN` env var, and the project layout change
in `README.md`. No code changes — documentation only.

**Expected Outcomes:**
- `agents/` directory is listed in the Project layout section with one line per agent file
- `AUTH_TOKEN` is added to the Configuration table with its default and purpose
- A brief "Architecture" section explains the subprocess agent model and where auth hooks in

**Todo list:**
- [ ] Add `AUTH_TOKEN` row to the Configuration table in `README.md`
- [ ] Update Project layout block to show `agents/` and its four scripts
- [ ] Add a short "Agent architecture" section after Configuration explaining the dispatch model and the auth stub

**Relevant context:**
- README is at the project root
- The auth stub note should point developers to `validate_token()` in each agent script as the replacement point

---

## Architecture reference

```
POST /api/chat (browser)
  │
  ▼
app.py — generate()
  │  Phase 1: POST Ollama (stream=False, tools=TOOL_DEFINITIONS)
  │
  ├─ no tool call → stream direct response → SSE → browser
  │
  └─ tool_calls detected
       │
       ▼
     tools.dispatch_tool(name, args)          ← unchanged signature
       │
       ▼
     TOOL_AGENT_MAP lookup
       │
       ▼
     subprocess.run(agent_script, stdin=payload_json)
       │
       ├─ agents/read_agent.py                [validate_token → db.get_booking / db.list_bookings]
       ├─ agents/booking_mutation_agent.py    [validate_token → db.create_booking / db.cancel_booking]
       ├─ agents/payment_read_agent.py        [validate_token → db.list_payment_cards]
       └─ agents/payment_write_agent.py       [validate_token → db.add/update/remove_payment_card]
                │
                ▼
           stdout → JSON result
                │
                ▼
     tools.dispatch_tool returns dict
       │
       ▼
     append tool result to messages → Phase 2: stream final Ollama reply → SSE → browser
```
