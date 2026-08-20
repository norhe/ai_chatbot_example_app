import os
import json
import logging
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "http://localhost:5000"}})

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))


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
    Stream a chat response from Ollama.

    Expected JSON body:
        {
            "model":    "<model name>",      # optional
            "messages": [{"role": ..., "content": ...}, ...]
        }
    """
    data = request.get_json(silent=True)
    if not data or "messages" not in data:
        return jsonify({"error": "Invalid request: 'messages' field is required"}), 400

    messages = data["messages"]
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "Invalid request: 'messages' must be a non-empty list"}), 400

    for msg in messages:
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            return jsonify({"error": "Each message must have 'role' and 'content' fields"}), 400
        if msg["role"] not in ("user", "assistant", "system"):
            return jsonify({"error": f"Invalid role: {msg['role']}"}), 400
        if not isinstance(msg["content"], str) or len(msg["content"]) > 32_000:
            return jsonify({"error": "Message content must be a string ≤ 32 000 characters"}), 400

    model = data.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        return jsonify({"error": "Invalid model name"}), 400

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }

    def generate():
        try:
            with requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json=payload,
                stream=True,
                timeout=REQUEST_TIMEOUT,
            ) as ollama_resp:
                if ollama_resp.status_code != 200:
                    yield f"data: {json.dumps({'error': 'Ollama returned an error'})}\n\n"
                    return
                for line in ollama_resp.iter_lines():
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
        except requests.RequestException as e:
            logger.error("Ollama streaming error", extra={"error": str(e)})
            yield f"data: {json.dumps({'error': 'Connection to Ollama failed'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
