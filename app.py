import os
import json
import logging
import time
import requests
from datetime import date
from flask import Flask, g, request, jsonify, Response, stream_with_context
from flask_cors import CORS

import db
import tools
import tracer
import auth_helper
from auth_helper import require_role
from agents.vault_helper import vault_approle_login, start_token_renewal

_LOG_LEVEL = logging.DEBUG if os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes") else logging.INFO
logging.basicConfig(
    level=_LOG_LEVEL,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)
if _LOG_LEVEL == logging.DEBUG:
    logger.debug("Debug logging enabled")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "http://localhost:5000"}})

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))

SYSTEM_PROMPT = """\
You are the Voyager Travel AI booking assistant, a helpful agent for the Voyager Travel company.
Today's date is {date}.
The authenticated user is: {username}. You do NOT need to ask for login credentials — the user \
is already signed in and their identity is verified.

You have access to the following tools — use them whenever a user asks about bookings or payment cards:

  • create_booking     — Create a new travel booking for a passenger. Always confirm the result
                         by telling the user the booking reference code (e.g. VT-00042).
  • get_booking        — Retrieve details of a single booking by its reference code.
  • list_bookings      — List all bookings, optionally filtered by passenger name.
  • cancel_booking     — Cancel an existing booking by its reference code.
  • add_payment_card   — Store a new payment card on the account.
  • update_payment_card— Update one or more fields on an existing payment card.
  • remove_payment_card— Remove a payment card from the account.

Important rules:
  - After creating a booking, always confirm it to the user with the reference code.
  - Never display full card numbers. When referring to a card, use the cardholder name and
    the last 4 digits only (e.g. "Alice Johnson's Visa ending in 1111").
  - You can only cancel one booking at a time, and only remove one payment card at a time.
    If the user asks to delete or cancel multiple items in a single request, politely explain
    this limitation and ask them to confirm each one individually.
  - Be concise, friendly, and professional.
"""


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/models", methods=["GET"])
def list_models():
    """Return the list of locally available Ollama models."""
    try:
        resp = requests.get(
            f"{OLLAMA_BASE_URL}/api/tags",
            timeout=10,
        )
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return jsonify({"models": models})
    except requests.RequestException as e:
        logger.error("Failed to fetch model list", extra={"error": str(e)})
        return jsonify({"error": "Could not reach Ollama service"}), 502


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Stream a chat response from Ollama, with server-side tool-call resolution.

    Expected JSON body:
        {
            "model":    "<model name>",      # optional
            "messages": [{"role": ..., "content": ...}, ...]
        }
    """
    data = request.get_json(silent=True)
    if not data or "messages" not in data:
        return jsonify({"error": "Invalid request: 'messages' field is required"}), 400

    client_messages = data["messages"]
    if not isinstance(client_messages, list) or not client_messages:
        return jsonify({"error": "Invalid request: 'messages' must be a non-empty list"}), 400

    for msg in client_messages:
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            return jsonify({"error": "Each message must have 'role' and 'content' fields"}), 400
        if msg["role"] not in ("user", "assistant", "system"):
            return jsonify({"error": f"Invalid role: {msg['role']}"}), 400
        if not isinstance(msg["content"], str) or len(msg["content"]) > 32_000:
            return jsonify({"error": "Message content must be a string ≤ 32 000 characters"}), 400

    model = data.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        return jsonify({"error": "Invalid model name"}), 400

    trace_id = request.headers.get("X-Trace-Id") or None
    bearer_token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()

    def generate():
        collector = tracer.TraceCollector(trace_id=trace_id)
        tracer_token = tracer.set_current_collector(collector)
        try:
            today = date.today().isoformat()
            user_info = auth_helper.validate_and_identify(bearer_token, provision=True)
            username = user_info["username"] if user_info else "unknown"
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.format(date=today, username=username)}
            ] + list(client_messages)

            logger.debug("chat/generate: model=%s message_count=%d", model, len(messages))

            # ------------------------------------------------------------------
            # Phase 1 — non-streaming request to check for tool calls
            # ------------------------------------------------------------------
            logger.debug("Phase 1: sending to Ollama (stream=False, tools=%d)", len(tools.TOOL_DEFINITIONS))
            p1_start = time.monotonic()
            phase1_resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools.TOOL_DEFINITIONS,
                    "stream": False,
                },
                timeout=REQUEST_TIMEOUT,
            )
            p1_duration = (time.monotonic() - p1_start) * 1000.0
            logger.debug("Phase 1: Ollama status=%d", phase1_resp.status_code)
            if phase1_resp.status_code != 200:
                logger.error("Phase 1: Ollama error body: %s", phase1_resp.text[:500])
                collector.record(
                    source="orchestrator",
                    target="ollama",
                    operation=f"Phase 1 LLM Evaluation ({model})",
                    span_type="llm_eval",
                    duration_ms=p1_duration,
                    status="error",
                    details={"model": model, "status_code": phase1_resp.status_code, "error": "Ollama error"},
                )
                yield f"data: {json.dumps({'trace': collector.events[-1].to_dict()})}\n\n"
                yield f"data: {json.dumps({'error': 'Ollama returned an error'})}\n\n"
                return

            assistant_message = phase1_resp.json()["message"]
            tool_calls = assistant_message.get("tool_calls") or []
            tool_names = [c["function"]["name"] for c in tool_calls] if tool_calls else []
            logger.debug("Phase 1: tool_calls=%d", len(tool_calls))

            collector.record(
                source="orchestrator",
                target="ollama",
                operation=f"Phase 1 LLM Evaluation ({model})",
                span_type="llm_eval",
                duration_ms=p1_duration,
                status="success",
                details={
                    "model": model,
                    "tool_count": len(tools.TOOL_DEFINITIONS),
                    "tools_invoked": tool_names,
                    "prompt_tokens": len(messages),
                },
            )
            yield f"data: {json.dumps({'trace': collector.events[-1].to_dict()})}\n\n"

            if tool_calls:
                # Append the assistant's tool-call message to the conversation
                messages.append(assistant_message)

                # Dispatch each tool call and append the results
                for call in tool_calls:
                    fn = call["function"]
                    name = fn["name"]
                    args = fn["arguments"]
                    if isinstance(args, str):
                        args = json.loads(args)
                    logger.debug("Dispatching tool: name=%s args=%s", name, json.dumps(args))
                    prev_count = len(collector.events)
                    result = tools.dispatch_tool(name, args, token=bearer_token, trace_id=collector.trace_id)
                    logger.info("Tool dispatched: name=%s result_keys=%s", name, list(result.keys()))
                    logger.debug("Tool result: %s", json.dumps(result))

                    # Yield newly recorded spans over SSE immediately
                    for ev in collector.events[prev_count:]:
                        yield f"data: {json.dumps({'trace': ev.to_dict()})}\n\n"

                    # Ollama expects the tool output inside the role=tool content
                    tool_content = result.get("result", result) if isinstance(result, dict) and "result" in result else result
                    messages.append({
                        "role": "tool",
                        "content": json.dumps(tool_content),
                    })

                # ------------------------------------------------------------------
                # Phase 2 — stream the final natural-language reply
                # ------------------------------------------------------------------
                logger.debug("Phase 2: streaming final reply (message_count=%d)", len(messages))
                p2_start = time.monotonic()
                full_reply = ""
                with requests.post(
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": True,
                    },
                    stream=True,
                    timeout=REQUEST_TIMEOUT,
                ) as stream_resp:
                    if stream_resp.status_code != 200:
                        collector.record(
                            source="orchestrator",
                            target="ollama",
                            operation=f"Phase 2 LLM Streaming ({model})",
                            span_type="llm_eval",
                            duration_ms=(time.monotonic() - p2_start) * 1000.0,
                            status="error",
                            details={"model": model, "status_code": stream_resp.status_code},
                        )
                        yield f"data: {json.dumps({'trace': collector.events[-1].to_dict()})}\n\n"
                        yield f"data: {json.dumps({'error': 'Ollama returned an error'})}\n\n"
                        return

                    for line in stream_resp.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            done = chunk.get("done", False)
                            full_reply += content
                            yield f"data: {json.dumps({'content': content, 'done': done})}\n\n"
                            if done:
                                break
                        except json.JSONDecodeError:
                            continue

                p2_duration = (time.monotonic() - p2_start) * 1000.0
                collector.record(
                    source="orchestrator",
                    target="ollama",
                    operation=f"Phase 2 LLM Streaming Response ({model})",
                    span_type="llm_eval",
                    duration_ms=p2_duration,
                    status="success",
                    details={"model": model, "response_chars": len(full_reply)},
                )
                yield f"data: {json.dumps({'trace': collector.events[-1].to_dict()})}\n\n"
            else:
                # No tool calls — yield the full content from phase 1 then done
                content = assistant_message.get("content", "")
                yield f"data: {json.dumps({'content': content, 'done': False})}\n\n"
                yield f"data: {json.dumps({'content': '', 'done': True})}\n\n"

        except requests.RequestException as e:
            logger.error("Ollama request error: %s", str(e))
            yield f"data: {json.dumps({'error': 'Connection to Ollama failed'})}\n\n"
        except Exception as e:
            logger.exception("Unexpected error in generate()")
            yield f"data: {json.dumps({'error': 'Internal server error'})}\n\n"
        finally:
            tracer.set_current_collector(None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/bookings", methods=["GET"])
@require_role("admin", "bookings_admin", "user", provision=True)
def get_bookings():
    current_user = g.current_user
    user_id = current_user["user_id"] if current_user["role"] == "user" else None
    bookings = db.list_bookings(user_id=user_id)
    coll = getattr(g, "trace_collector", None) or tracer.get_current_collector()
    traces = coll.to_list() if coll else []
    return jsonify({"bookings": bookings, "traces": traces})


@app.route("/api/payment-cards", methods=["GET"])
@require_role("admin", "payments_admin", "user", provision=True)
def get_payment_cards():
    current_user = g.current_user
    user_id = current_user["user_id"] if current_user["role"] == "user" else None
    cards = db.list_payment_cards(user_id=user_id)
    coll = getattr(g, "trace_collector", None) or tracer.get_current_collector()
    traces = coll.to_list() if coll else []
    return jsonify({"cards": cards, "traces": traces})


def _init_vault() -> None:
    """Authenticate to Vault using AppRole creds from the environment.

    Skips setup and falls back to DATABASE_URL if any required env var is
    absent (allows running the orchestrator without Vault for local development).
    """
    role_id    = os.environ.get("VAULT_ROLE_ID")
    secret_id  = os.environ.get("VAULT_SECRET_ID")
    vault_addr = os.environ.get("VAULT_ADDR")

    if not (role_id and secret_id and vault_addr):
        logger.warning(
            '{"event": "vault_init_skipped", "reason": "VAULT_ROLE_ID/VAULT_SECRET_ID/VAULT_ADDR not set — falling back to DATABASE_URL"}'
        )
        return

    client = vault_approle_login(role_id, secret_id)
    os.environ["VAULT_TOKEN"]   = client.token
    os.environ["VAULT_DB_ROLE"] = "voyager_orchestrator"
    start_token_renewal(client, logger)
    logger.info('{"event": "vault_init_complete", "db_role": "voyager_orchestrator"}')


if __name__ == "__main__":
    _init_vault()
    app.run(host="127.0.0.1", port=5000, debug=False)
