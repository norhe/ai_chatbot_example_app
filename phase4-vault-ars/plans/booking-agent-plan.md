# Booking Agent Plan

## Overview

Extend the Voyager Travel AI chatbot into a functional travel booking agent. The LLM (via Ollama)
acts as the conversational interface and intent parser; a PostgreSQL database holds the ground truth
for bookings. The LLM uses Ollama's native tool-calling API to trigger structured CRUD operations
which the Flask backend executes and feeds back into the conversation loop.

**Scope:**
- PostgreSQL database with a `bookings` table and a `payment_cards` table, seeded with fake data on first run
- 7 LLM-callable tools: `create_booking`, `get_booking`, `list_bookings`, `cancel_booking`, `add_payment_card`, `update_payment_card`, `remove_payment_card`
- Extended `/api/chat` endpoint that handles the tool-call → execute → re-submit loop
- New `GET /api/bookings` endpoint for the UI sidebar
- Collapsible bookings sidebar in the frontend that auto-refreshes after each AI response

**Non-goals:**
- Authentication / multi-user isolation
- Migration tooling (Alembic etc.) — `CREATE TABLE IF NOT EXISTS` on startup is sufficient
- Multiple booking types — a single generic `bookings` table covers all travel

**Configuration:**
- `DATABASE_URL` environment variable — e.g. `postgresql://user:pass@localhost:5432/voyager`
- All other config (Ollama URL, model, timeout) unchanged

---

## Sub-Tasks

---

### Sub-Task 1 — Database layer (`db.py`)

**Status:** `[ ] pending`

**Intent:**
Centralise all PostgreSQL access in a single `db.py` module. The rest of the app imports helpers
from here; nothing else talks to the DB directly. Using `psycopg2` (pure-Python postgres driver,
no ORM needed for this scope).

**Expected Outcomes:**
- `db.py` exists with `init_db()`, `seed_db()`, booking CRUD helpers, and payment card CRUD helpers
- Running `python db.py` directly creates both tables and seeds them (useful for local testing)
- Both table schemas use `CREATE TABLE IF NOT EXISTS` — safe to call on every startup
- 6–8 realistic fake bookings and 3–4 fake payment cards are inserted only when tables are empty (idempotent seed)

**Bookings table schema:**

| Column         | Type                    | Notes                                      |
|----------------|-------------------------|--------------------------------------------|
| id             | SERIAL PRIMARY KEY      |                                            |
| reference      | VARCHAR(8) UNIQUE       | Short human-readable code e.g. `VT-00042`  |
| passenger_name | VARCHAR(120) NOT NULL   |                                            |
| destination    | VARCHAR(120) NOT NULL   |                                            |
| origin         | VARCHAR(120) NOT NULL   |                                            |
| travel_date    | DATE NOT NULL           |                                            |
| return_date    | DATE                    | NULL for one-way                           |
| status         | VARCHAR(20) NOT NULL    | `confirmed`, `cancelled`, `pending`        |
| created_at     | TIMESTAMPTZ NOT NULL    | Default `NOW()`                            |

**Payment cards table schema (`payment_cards`):**

| Column           | Type                  | Notes                                                        |
|------------------|-----------------------|--------------------------------------------------------------|
| id               | SERIAL PRIMARY KEY    |                                                              |
| cardholder_name  | VARCHAR(120) NOT NULL |                                                              |
| card_number      | VARCHAR(19) NOT NULL  | Full fake number stored for demo only — NEVER do this in production |
| card_brand       | VARCHAR(20) NOT NULL  | `Visa`, `Mastercard`, `Amex`, `Discover`                     |
| expiry           | VARCHAR(7) NOT NULL   | Format `MM/YYYY`                                             |
| billing_address  | TEXT NOT NULL         |                                                              |
| created_at       | TIMESTAMPTZ NOT NULL  | Default `NOW()`                                              |

> ⚠️ **Demo only:** Raw card numbers are stored here purely for example app realism.
> In any real system, card data must never be stored — use a PCI-compliant vault or tokenisation
> service (e.g. Stripe, IBM Hyper Protect Crypto Services).

**Booking CRUD helpers to implement:**

- `create_booking(passenger_name, destination, origin, travel_date, return_date) -> dict`
  — inserts a row, returns the full booking dict including generated `reference`
- `get_booking(reference) -> dict | None`
  — fetch single booking by reference code
- `list_bookings(passenger_name=None) -> list[dict]`
  — return all bookings, optionally filtered by passenger name (case-insensitive ILIKE)
- `cancel_booking(reference) -> dict | None`
  — set `status = 'cancelled'`, return updated booking or None if not found

**Payment card CRUD helpers to implement:**

- `add_payment_card(cardholder_name, card_number, card_brand, expiry, billing_address) -> dict`
  — inserts a card row, returns the full card dict
- `update_payment_card(card_id, **fields) -> dict | None`
  — updates any subset of `cardholder_name`, `card_number`, `card_brand`, `expiry`, `billing_address`; returns updated card or None if not found
- `remove_payment_card(card_id) -> bool`
  — deletes the card row, returns True if a row was deleted, False if not found
- `list_payment_cards() -> list[dict]`
  — returns all stored cards (used internally and by the sidebar)

**Todo list:**
- [ ] Add `psycopg2-binary==2.9.10` to `requirements.txt`
- [ ] Create `db.py` with `get_connection()` using `DATABASE_URL` env var
- [ ] Implement `init_db()` — runs `CREATE TABLE IF NOT EXISTS` for both tables
- [ ] Implement `seed_db()` — inserts fake bookings and fake payment cards only when tables are empty
- [ ] Implement the four booking CRUD helper functions
- [ ] Implement the four payment card CRUD helper functions
- [ ] Add `if __name__ == "__main__"` block to allow running `db.py` directly for setup

**Relevant context:**
- `DATABASE_URL` format: `postgresql://user:pass@host:5432/dbname`
- `psycopg2-binary` bundles the C extension — no system `libpq` install needed
- Reference format: `VT-` + zero-padded 5-digit SERIAL id, e.g. `VT-00001`
  (generate after insert using `RETURNING id`)
- All dict keys returned from helpers should be JSON-serialisable (dates → ISO strings)
- Seed card numbers should follow standard fake test formats (e.g. Visa `4111 1111 1111 1111`)
  so they are obviously synthetic

---

### Sub-Task 2 — Tool definitions (`tools.py`)

**Status:** `[ ] pending`

**Intent:**
Define the four Ollama-compatible tool schemas and a single `dispatch_tool(name, args)` dispatcher.
Keeping this in its own file keeps `app.py` clean and makes it easy to add tools later.

**Expected Outcomes:**
- `tools.py` exports `TOOL_DEFINITIONS` (list of Ollama tool-schema dicts) and `dispatch_tool(name, args) -> dict`
- Each tool schema follows the Ollama `tools` API format (OpenAI-compatible function-calling schema)
- The dispatcher calls the appropriate `db.py` helper and returns a plain dict result

**Tools to define:**

| Tool name            | Parameters                                                                          | Returns               |
|----------------------|-------------------------------------------------------------------------------------|-----------------------|
| `create_booking`     | `passenger_name`, `destination`, `origin`, `travel_date` (YYYY-MM-DD), `return_date` (optional) | booking dict |
| `get_booking`        | `reference`                                                                         | booking dict or error |
| `list_bookings`      | `passenger_name` (optional)                                                         | list of booking dicts |
| `cancel_booking`     | `reference`                                                                         | booking dict or error |
| `add_payment_card`   | `cardholder_name`, `card_number`, `card_brand`, `expiry` (MM/YYYY), `billing_address` | card dict           |
| `update_payment_card`| `card_id`, plus any of: `cardholder_name`, `card_number`, `card_brand`, `expiry`, `billing_address` | card dict or error |
| `remove_payment_card`| `card_id`                                                                           | success/error message |

**Todo list:**
- [ ] Create `tools.py`
- [ ] Define `TOOL_DEFINITIONS` list with all seven tool schemas in Ollama function-calling format
- [ ] Implement `dispatch_tool(name, args)` — routes to `db.py` helpers, wraps result in `{"result": ...}` or `{"error": "..."}` on failure

**Relevant context:**
- Ollama tool schema shape (per Ollama docs):
  ```json
  {
    "type": "function",
    "function": {
      "name": "...",
      "description": "...",
      "parameters": {
        "type": "object",
        "properties": { ... },
        "required": [...]
      }
    }
  }
  ```
- The dispatcher must catch exceptions and return `{"error": "..."}` rather than raising, because
  the error string is sent back to the LLM as the tool result so it can respond gracefully.

---

### Sub-Task 3 — Extended backend (`app.py`)

**Status:** `[ ] pending`

**Intent:**
Extend `app.py` to:
1. Initialise the DB on startup
2. Handle Ollama tool-call responses in the chat loop (detect → dispatch → re-submit)
3. Add `GET /api/bookings` for the sidebar

The streaming SSE contract with the frontend stays the same — the tool-call round-trips happen
server-side and are invisible to the client.

**Expected Outcomes:**
- `init_db()` and `seed_db()` are called once on app startup
- `POST /api/chat` passes `TOOL_DEFINITIONS` to Ollama and loops when a tool call is returned,
  until Ollama returns a final text response
- A hard system prompt prepended to every request establishes the agent persona and date context
- `GET /api/bookings` returns `{"bookings": [...]}` — all rows from the DB
- All existing validation and SSE streaming behaviour is preserved

**Tool-call loop (server-side, inside the `generate()` streaming function):**

```
1. Build messages = [system_prompt] + client_messages
2. POST to Ollama with tools=TOOL_DEFINITIONS, stream=False  ← non-streaming for tool turns
3. If response contains tool_calls:
     a. For each tool call: dispatch_tool(name, args) → result
     b. Append assistant tool-call message + tool result messages to messages
     c. POST to Ollama again (stream=True this time for the final reply)
4. Stream the final reply tokens to the client via SSE as before
```

Note: the intermediate tool-call Ollama request uses `stream=False` to get the full structured
response cleanly. Only the final natural-language reply is streamed to the browser.

**System prompt content:**
- Agent name and company (Voyager Travel)
- Today's date (injected at request time so the LLM reasons correctly about travel dates)
- Description of available tools and when to use them
- Instruction to always confirm bookings with the reference code

**Todo list:**
- [ ] Import `db`, `tools` modules in `app.py`
- [ ] Call `db.init_db()` and `db.seed_db()` at app startup (module level, before `app.run`)
- [ ] Define `SYSTEM_PROMPT` string (with `{date}` placeholder filled at request time) — include payment card tool descriptions
- [ ] Refactor `generate()` in `POST /api/chat`:
  - First POST to Ollama non-streaming to check for tool calls
  - If tool calls present: dispatch all, append messages, then stream final reply
  - If no tool calls: stream the direct response
- [ ] Add `GET /api/bookings` route that queries `db.list_bookings()` and returns JSON
- [ ] Add `GET /api/payment-cards` route that queries `db.list_payment_cards()` and returns JSON
- [ ] Update `requirements.txt` if any new imports are needed (none expected beyond Sub-Task 1)

**Relevant context:**
- Existing `generate()` is a generator function yielding SSE `data:` lines — preserve this shape
- The `messages` list must be mutable within `generate()` to support the tool loop
- Ollama tool-call response shape: `response["message"]["tool_calls"]` — list of `{function: {name, arguments}}`
- `arguments` from Ollama may be a JSON string or a dict — normalise with `json.loads` if string

---

### Sub-Task 4 — Frontend sidebar (`static/index.html`)

**Status:** `[ ] pending`

**Intent:**
Add a collapsible bookings sidebar to the right of the chat. It polls `GET /api/bookings` on load
and after every completed AI response. A toggle button shows/hides the panel.

**Expected Outcomes:**
- Sidebar is visible by default on wide screens, hidden on narrow screens
- Sidebar has two tabs: **Bookings** and **Payment Cards**
- Bookings tab: each card shows reference, passenger name, origin → destination, travel date, status badge
- Payment Cards tab: each card shows cardholder name, masked card number (last 4 digits only for display), brand, expiry
- Status badge is colour-coded: green = confirmed, red = cancelled, amber = pending
- Sidebar refreshes automatically after each assistant message completes
- A "Refresh" button allows manual reload
- Toggle button collapses/expands the sidebar

**Layout:**

```
┌─────────────────────────────────────────────────┐
│  Header (full width)                            │
├────────────────────────────┬────────────────────┤
│                            │  [Bookings][Cards] [↔] │
│   Chat window              │  ─────────────     │
│                            │  VT-00001          │
│                            │  Jane Smith        │
│                            │  NYC → Paris       │
│                            │  Jun 10 · confirmed│
│                            │  ─────────────     │
│   Input bar                │  ...               │
└────────────────────────────┴────────────────────┘
```

**Todo list:**
- [ ] Restructure the `<body>` layout: wrap chat + input in a `.main-panel` flex container,
  add `.sidebar` alongside it
- [ ] Add sidebar CSS: fixed width (~280px), scrollable, collapsible via `.sidebar--hidden` class
- [ ] Add two-tab switcher in the sidebar header (Bookings / Payment Cards)
- [ ] Add toggle button to collapse/expand the sidebar
- [ ] Implement `loadBookings()` — fetches `/api/bookings`, renders booking cards in the Bookings tab
- [ ] Implement `loadCards()` — fetches `/api/payment-cards`, renders card entries in the Cards tab
- [ ] Call both `loadBookings()` and `loadCards()` on page load and after each successful AI response
- [ ] Display card numbers masked to last 4 digits in the UI (full number is in the API response but must not be rendered)
- [ ] Add status badge styling (confirmed = green, cancelled = red, pending = amber)
- [ ] Ensure layout is responsive: sidebar collapses automatically below 700px viewport width

**Relevant context:**
- Existing brand CSS variables (`--brand-primary`, `--brand-accent`, etc.) should be reused
- `sendMessage()` already has a `finally` block — `loadBookings()` / `loadCards()` calls go in the `try` block
  after `conversationHistory.push(...)` (i.e. only on success)
- Keep the sidebar pure HTML/CSS/JS — no framework dependencies
- **Security note:** even though the full card number is stored in the DB for demo purposes, the UI
  must only ever render the last 4 digits — mask on the frontend with `card.card_number.slice(-4)`

---

## Architecture Diagram (reference)

```
Browser
  Chat UI       ←──SSE stream─────────────────────────┐
  Sidebar                                              │
    Bookings tab  ←──GET /api/bookings──────────┐      │
    Cards tab     ←──GET /api/payment-cards──┐  │      │
                                             │  │      │
Flask (app.py)                               │  │      │
  GET  /api/payment-cards ── db.list_cards() ┘  │      │
  GET  /api/bookings ──────── db.list_bookings() ┘     │
  POST /api/chat                                       │
    │                                                  │
    ├─[no tool call]──► stream Ollama ─────────────────┘
    │
    └─[tool call detected]
        │
        ├─► tools.dispatch() ─► db.py ─► PostgreSQL
        │       result
        └─► re-submit to Ollama ─► stream final reply ──► SSE
```
