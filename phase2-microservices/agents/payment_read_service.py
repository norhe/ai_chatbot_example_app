import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from flask import Flask, request, jsonify

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "payment_read_service", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def validate_token(token: str) -> bool:
    # TODO: replace with external auth service call
    # e.g. return requests.post(AUTH_SERVICE_URL, json={"token": token}).json()["valid"]
    logger.debug("validate_token: token=%r", token)
    return True


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "payment_read_service"})


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
    logger.debug("tool=%s", tool)

    if tool == "list_payment_cards":
        logger.debug("Calling db.list_payment_cards")
        try:
            cards = db.list_payment_cards()
        except Exception as e:
            logger.exception("db.list_payment_cards raised an exception")
            return jsonify({"error": str(e)})
        logger.debug("list_payment_cards returned %d rows", len(cards))
        return jsonify({"result": cards})

    logger.error("Unknown tool: %s", tool)
    return jsonify({"error": f"Unknown tool: {tool}"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PAYMENT_READ_SERVICE_PORT", 5103))
    app.run(host="127.0.0.1", port=port)
