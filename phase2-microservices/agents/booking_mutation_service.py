import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from flask import Flask, request, jsonify

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "booking_mutation_service", "message": "%(message)s"}',
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
    return jsonify({"status": "ok", "service": "booking_mutation_service"})


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

    if tool == "create_booking":
        try:
            logger.debug("Calling db.create_booking with args=%s", json.dumps(args))
            booking = db.create_booking(
                passenger_name=args["passenger_name"],
                destination=args["destination"],
                origin=args["origin"],
                travel_date=args["travel_date"],
                return_date=args.get("return_date"),
            )
        except Exception as e:
            logger.exception("db.create_booking raised an exception")
            return jsonify({"error": str(e)})
        logger.debug("create_booking result: %s", json.dumps(booking))
        return jsonify({"result": booking})

    if tool == "cancel_booking":
        reference = args.get("reference")
        logger.debug("Calling db.cancel_booking reference=%r", reference)
        try:
            booking = db.cancel_booking(reference)
        except Exception as e:
            logger.exception("db.cancel_booking raised an exception")
            return jsonify({"error": str(e)})
        logger.debug("cancel_booking returned: %s", json.dumps(booking))
        if booking is None:
            logger.warning("cancel_booking: reference=%r not found", reference)
            return jsonify({"error": "Booking not found"})
        return jsonify({"result": booking})

    logger.error("Unknown tool: %s", tool)
    return jsonify({"error": f"Unknown tool: {tool}"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("BOOKING_SERVICE_PORT", 5102))
    app.run(host="127.0.0.1", port=port)
