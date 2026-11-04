import os
import json
import logging
import requests
from datetime import date
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

import db
import tools

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "http://localhost:5000"}})

db.init_db()
db.seed_db()

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))

SYSTEM_PROMPT = """\
You are the Voyager Travel AI booking assistant, a helpful agent for the Voyager Travel company.
Today's date is {date}.

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

    def generate():
        try:
            today = date.today().isoformat()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.format(date=today)}
            ] + list(client_messages)

            # ------------------------------------------------------------------
            # Phase 1 — non-streaming request to check for tool calls
            # ------------------------------------------------------------------
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
            if phase1_resp.status_code != 200:
                yield f"data: {json.dumps({'error': 'Ollama returned an error'})}\n\n"
                return

            assistant_message = phase1_resp.json()["message"]
            tool_calls = assistant_message.get("tool_calls") or []

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
                    result = tools.dispatch_tool(name, args)
                    logger.info("Tool call dispatched", extra={"tool": name})
                    messages.append({
                        "role": "tool",
                        "content": json.dumps(result),
                    })

                # ------------------------------------------------------------------
                # Phase 2 — stream the final natural-language reply
                # ------------------------------------------------------------------
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
                        yield f"data: {json.dumps({'error': 'Ollama returned an error'})}\n\n"
                        return
                    for line in stream_resp.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            done = chunk.get("done", False)
                            yield f"data: {json.dumps({'content': content, 'done': done})}\n\n"
                            if done:
                                break
                        except json.JSONDecodeError:
                            continue
            else:
                # No tool calls — yield the full content from phase 1 then done
                content = assistant_message.get("content", "")
                yield f"data: {json.dumps({'content': content, 'done': False})}\n\n"
                yield f"data: {json.dumps({'content': '', 'done': True})}\n\n"

        except requests.RequestException as e:
            logger.error("Ollama request error", extra={"error": str(e)})
            yield f"data: {json.dumps({'error': 'Connection to Ollama failed'})}\n\n"
        except Exception as e:
            logger.error("Unexpected error in generate()", extra={"error": str(e)})
            yield f"data: {json.dumps({'error': 'Internal server error'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/bookings", methods=["GET"])
def get_bookings():
    bookings = db.list_bookings()
    return jsonify({"bookings": bookings})


@app.route("/api/payment-cards", methods=["GET"])
def get_payment_cards():
    cards = db.list_payment_cards()
    return jsonify({"cards": cards})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
