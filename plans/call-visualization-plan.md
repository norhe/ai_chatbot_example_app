# Component Call Visualization & Architecture Training Plan

## Top-Level Overview

This plan defines the architecture and implementation for an interactive, real-time **Component Call Visualization Panel** positioned on the left side of the Voyager Travel application.

The primary purpose is to serve as an interactive educational and training tool for engineers learning:
1. **Agent-Based Architecture**: How the orchestrator interacts with the LLM (Ollama), parses tool calling decisions, and dispatches requests to dedicated agent microservices.
2. **Keycloak OIDC Integration**: How PKCE browser authentication, Bearer token propagation across services, and RS256 JWKS validation with role-based access control (RBAC) operate.
3. **HashiCorp Vault Integration**: How services authenticate via AppRole/WIF, manage token lifecycle renewals, and request short-lived ephemeral PostgreSQL database credentials on demand via the Vault database secrets engine.
4. **End-to-End Tracing & Observability**: How distributed calls flow between Browser, Orchestrator, Microservices, OIDC Provider, Vault, and PostgreSQL.

The UI will feature a dual-tab left panel containing:
- **Topology Map Tab**: An interactive SVG component topology diagram with animated node highlights and pulse flows across connection paths in real time.
- **Sequence & Trace Timeline Tab**: A chronological step-by-step trace tree with educational explanations, payload inspectors, duration metrics, and security concept callouts (OIDC token checks, Vault leases, DB queries).

---

## Sub-Tasks

### Sub-Task 1: Call Tracing and Instrumentation Infrastructure
- **Intent**: Establish a unified, lightweight tracing mechanism across backend services, database operations, auth validation, and Vault requests without adding heavy external dependencies.
- **Expected Outcomes**:
  - Structured trace events captured during user authentication, chat queries, tool executions, and direct API calls.
  - Standardized trace event model containing `trace_id`, `span_id`, `source`, `target`, `operation`, `type` (e.g. `oidc_auth`, `vault_creds`, `db_query`, `service_http`, `llm_eval`), `timestamp`, `duration_ms`, `status`, and educational metadata.
  - Trace events emitted in real time over the existing SSE stream (`/api/chat`) and included in response headers for REST endpoints.
- **Todo List**:
  1. Create a centralized tracing module (`tracer.py`) to manage trace contexts, span creation, and timing collection.
  2. Instrument `app.py` to trace chat requests, Phase 1 Ollama evaluation, tool dispatch, and Phase 2 streaming response.
  3. Instrument `tools.py` to capture service dispatch HTTP calls, headers, latencies, and responses.
  4. Instrument `auth_helper.py` to capture JWKS cache hits/misses, RS256 signature verification, and RBAC user provisioning.
  5. Instrument `db.py` and `agents/vault_helper.py` to capture Vault AppRole login, token renewal, Vault dynamic DB credential generation, and PostgreSQL query execution.
  6. Instrument agent microservices (`agents/booking_read_service.py`, `agents/booking_mutation_service.py`, `agents/payment_read_service.py`, `agents/payment_write_service.py`) to record execution spans and return them to the orchestrator.
- **Relevant Context**:
  - [`app.py`](app.py)
  - [`tools.py`](tools.py)
  - [`auth_helper.py`](auth_helper.py)
  - [`db.py`](db.py)
  - [`agents/vault_helper.py`](agents/vault_helper.py)
  - [`agents/booking_read_service.py`](agents/booking_read_service.py)
- **Status**: `[X] complete`

---

### Sub-Task 2: Frontend Layout and Responsive Left Visualization Panel
- **Intent**: Add a dedicated, collapsible visual panel on the left side of the screen that seamlessly integrates into the Voyager Travel UI without disrupting existing chat and sidebar functionality.
- **Expected Outcomes**:
  - 3-column desktop layout (`[Left Viz Panel] [Main Chat Panel] [Right Data Sidebar]`).
  - Toggle button in the header to show/hide the visualization panel.
  - Tab navigation switching between **Topology Map** and **Sequence & Trace Timeline**.
  - Control bar with Clear, Pause/Resume Live Trace, Replay Last Flow, and Component Filter toggles.
  - Responsive collapse on smaller viewports with full accessibility.
- **Todo List**:
  1. Update layout structure in `static/index.html` to introduce `.viz-panel` on the left side of the main workspace.
  2. Add styles and brand theme variables for the visualization panel, tabs, control buttons, and responsive breakpoints.
  3. Add header toggle button and keyboard shortcut / button actions to collapse and expand the left panel.
  4. Implement trace state management in JavaScript to buffer and organize traces by session and interaction ID.
- **Relevant Context**:
  - [`static/index.html`](static/index.html)
- **Status**: `[X] complete`

---

### Sub-Task 3: Interactive Architecture Topology Map (Tab 1)
- **Intent**: Provide an intuitive, animated visual diagram showing all system components and live call flows between them to train engineers on the microservice, Vault, and OIDC interactions.
- **Expected Outcomes**:
  - Clean SVG/Canvas diagram depicting all architecture nodes:
    - User / Browser
    - Keycloak (OIDC Provider)
    - Orchestrator Backend (`app.py`)
    - Ollama LLM
    - Agent Microservices (`Booking Read`, `Booking Mutation`, `Payment Read`, `Payment Write`)
    - HashiCorp Vault (AppRole & Database Secrets Engine)
    - PostgreSQL Database
  - Animated pulsing connection lines and active node glowing effects when calls are in-flight.
  - Clickable nodes and edges showing component role, security boundaries, and active communication stats.
  - Visual distinction between synchronous API calls, authentication checks, secret leases, and DB transactions.
- **Todo List**:
  1. Build the SVG component graph layout with defined node positions, icons, labels, and directional path connectors in `static/index.html`.
  2. Implement visual state transitions (idle, active/pulsing, success, error) for nodes and connector lines.
  3. Wire incoming real-time trace events to trigger visual call path animations with configurable step speed.
  4. Add interactive component info tooltips and inspect modal on node click to explain each component's architectural role.
- **Relevant Context**:
  - [`static/index.html`](static/index.html)
- **Status**: `[X] complete`

---

### Sub-Task 4: Detailed Sequence & Call Trace Timeline (Tab 2)
- **Intent**: Provide a granular, chronological breakdown of each call hop with in-depth educational notes explaining what happened and why (e.g. why Vault generated ephemeral DB creds, how OIDC token roles were evaluated).
- **Expected Outcomes**:
  - Chronological trace list with collapsible step cards.
  - Educational callout badges for key concepts:
    - `[OIDC PKCE]` / `[JWT RS256 Verification]`
    - `[Vault AppRole]` / `[Vault Dynamic DB Secret Lease]`
    - `[Agent Tool Dispatch]` / `[Least Privilege DB Query]`
  - Detailed metadata viewer for each step (URL, method, execution time, parameters, unsanitized responses for learning purposes, lease TTL).
  - Trace export/download option (JSON) for sharing and offline training review.
- **Todo List**:
  1. Implement sequence/timeline rendering engine in `static/index.html` that formats spans into a step tree.
  2. Build educational annotation library explaining security mechanics for each event type (OIDC, Vault, DB, Agent, LLM).
  3. Add expandable payload/metadata inspector for each trace card.
  4. Implement filter controls (e.g., filter by OIDC only, Vault only, DB only, or Tool Dispatch only).
- **Relevant Context**:
  - [`static/index.html`](static/index.html)
  - [`auth_helper.py`](auth_helper.py)
  - [`agents/vault_helper.py`](agents/vault_helper.py)
  - [`db.py`](db.py)
- **Status**: `[X] complete`

---

### Sub-Task 5: Frontend Auth Tracing and Real-Time SSE Stream Integration
- **Intent**: Connect the browser-side OIDC auth lifecycle and the streaming chat SSE pipeline directly into the visualization engine.
- **Expected Outcomes**:
  - Browser captures and displays its own OIDC interactions (auth redirect, PKCE challenge generation, token exchange, token refresh).
  - SSE listener in frontend consumes both message chunks and embedded `trace` event payloads seamlessly without latency impact.
  - Real-time synchronous playback of traces as the user interacts with the chat assistant.
- **Todo List**:
  1. Instrument frontend PKCE auth routines (`_generatePKCE`, `_handleCallback`, `_getToken`, `authFetch`) to push client-side auth traces to the visualization engine.
  2. Update frontend `sendMessage()` to parse mixed content/trace SSE streams.
  3. Instrument sidebar fetch calls (`loadBookings()`, `loadCards()`) to capture REST trace data.
  4. Implement step playback queue to ensure animated flows remain smooth and legible even during fast responses.
- **Relevant Context**:
  - [`static/index.html`](static/index.html)
  - [`app.py`](app.py)
- **Status**: `[ ] pending`

---

### Sub-Task 6: Verification, End-to-End Testing, and Training Documentation
- **Intent**: Validate that all component calls are accurately captured and visualized across all workflows, and document the visualization features for engineering training.
- **Expected Outcomes**:
  - Successful trace capture across:
    - PKCE Login & Token Validation
    - Chat prompt with no tools (Orchestrator -> Ollama)
    - Booking query tool (Orchestrator -> Booking Read Service -> Keycloak JWKS -> Vault DB Creds -> PostgreSQL)
    - Booking mutation tool (Orchestrator -> Booking Mutation Service -> Vault DB Creds -> PostgreSQL)
    - Payment card tools (Payment Read/Write Services)
  - Training documentation in `README.md` explaining how to use the visualization panel for onboarding new engineers.
- **Todo List**:
  1. Run end-to-end chat queries covering all 7 tool types and verify all topology paths and sequence steps illuminate correctly.
  2. Verify error flows (e.g., unauthorized token, expired lease, unknown tool) are properly reflected with red/warning states.
  3. Update `README.md` with a guide on the Call Visualization panel and the architecture patterns it demonstrates.
- **Relevant Context**:
  - [`README.md`](README.md)
  - [`app.py`](app.py)
  - [`services/start_services.sh`](services/start_services.sh)
- **Status**: `[ ] pending`
