import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from flask import Flask, request, jsonify

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "payment_write_service", "message": "%(message)s"}',
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
    return jsonify({"status": "ok", "service": "payment_write_service"})


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

    if tool == "add_payment_card":
        logger.debug("Calling db.add_payment_card")
        try:
            card = db.add_payment_card(
                cardholder_name=args["cardholder_name"],
                card_number=args["card_number"],
                card_brand=args["card_brand"],
                expiry=args["expiry"],
                billing_address=args["billing_address"],
            )
        except Exception as e:
            logger.exception("db.add_payment_card raised an exception")
            return jsonify({"error": str(e)})
        logger.debug("add_payment_card result: %s", json.dumps(card))
        return jsonify({"result": card})

    if tool == "update_payment_card":
        card_id = args["card_id"]
        updatable = {"cardholder_name", "card_number", "card_brand", "expiry", "billing_address"}
        fields = {k: v for k, v in args.items() if k in updatable}
        logger.debug("Calling db.update_payment_card card_id=%s fields=%s", card_id, list(fields.keys()))
        try:
            card = db.update_payment_card(card_id, **fields)
        except Exception as e:
            logger.exception("db.update_payment_card raised an exception")
            return jsonify({"error": str(e)})
        logger.debug("update_payment_card returned: %s", json.dumps(card))
        if card is None:
            logger.warning("update_payment_card: card_id=%s not found", card_id)
            return jsonify({"error": "Card not found"})
        return jsonify({"result": card})

    if tool == "remove_payment_card":
        card_id = args["card_id"]
        logger.debug("Calling db.remove_payment_card card_id=%s", card_id)
        try:
            removed = db.remove_payment_card(card_id)
        except Exception as e:
            logger.exception("db.remove_payment_card raised an exception")
            return jsonify({"error": str(e)})
        logger.debug("remove_payment_card returned: %s", removed)
        if not removed:
            logger.warning("remove_payment_card: card_id=%s not found", card_id)
            return jsonify({"error": "Card not found"})
        return jsonify({"result": "Card removed"})

    logger.error("Unknown tool: %s", tool)
    return jsonify({"error": f"Unknown tool: {tool}"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PAYMENT_WRITE_SERVICE_PORT", 5104))
    app.run(host="127.0.0.1", port=port)
