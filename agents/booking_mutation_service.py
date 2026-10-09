import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from flask import Flask, request, jsonify, g
from auth_helper import require_role
from vault_helper import vault_approle_login, start_token_renewal

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "booking_mutation_service", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "booking_mutation_service"})


@app.route("/execute", methods=["POST"])
@require_role("user", "bookings_admin", "admin")
def execute():
    current_user = g.current_user
    role = current_user["role"]
    user_id = current_user["user_id"] if role == "user" else None

    data = request.get_json(silent=True)
    if not data or "tool" not in data:
        return jsonify({"error": "Invalid request: 'tool' field required"}), 400

    tool = data["tool"]
    args = data.get("args", {})
    logger.debug("tool=%s args=%s", tool, json.dumps(args))

    if tool == "create_booking":
        try:
            logger.debug("Calling db.create_booking with args=%s user_id=%s", json.dumps(args), user_id)
            booking = db.create_booking(
                passenger_name=args["passenger_name"],
                destination=args["destination"],
                origin=args["origin"],
                travel_date=args["travel_date"],
                return_date=args.get("return_date"),
                user_id=current_user["user_id"],  # always set user_id for new bookings
            )
        except Exception as e:
            logger.exception("db.create_booking raised an exception")
            return jsonify({"error": "Internal server error"})
        logger.debug("create_booking result: %s", json.dumps(booking))
        return jsonify({"result": booking})

    if tool == "cancel_booking":
        reference = args.get("reference")
        logger.debug("Calling db.cancel_booking reference=%r user_id=%s", reference, user_id)
        try:
            booking = db.cancel_booking(reference, user_id=user_id)
        except Exception as e:
            logger.exception("db.cancel_booking raised an exception")
            return jsonify({"error": "Internal server error"})
        logger.debug("cancel_booking returned: %s", json.dumps(booking))
        if booking is None:
            logger.warning("cancel_booking: reference=%r not found or not accessible", reference)
            return jsonify({"error": "Booking not found"})
        return jsonify({"result": booking})

    logger.error("Unknown tool: %s", tool)
    return jsonify({"error": f"Unknown tool: {tool}"}), 400


def _init_vault() -> None:
    """Authenticate to Vault using AppRole creds from the environment.

    Skips setup and falls back to DATABASE_URL if any required env var is
    absent (allows running the service without Vault for local development).
    """
    role_id   = os.environ.get("VAULT_ROLE_ID")
    secret_id = os.environ.get("VAULT_SECRET_ID")
    vault_addr = os.environ.get("VAULT_ADDR")

    if not (role_id and secret_id and vault_addr):
        logger.warning(
            '{"event": "vault_init_skipped", "reason": "VAULT_ROLE_ID/VAULT_SECRET_ID/VAULT_ADDR not set — falling back to DATABASE_URL"}'
        )
        return

    client = vault_approle_login(role_id, secret_id)
    os.environ["VAULT_TOKEN"]   = client.token
    os.environ["VAULT_DB_ROLE"] = "voyager_booking_write"
    start_token_renewal(client, logger)
    logger.info('{"event": "vault_init_complete", "db_role": "voyager_booking_write"}')


if __name__ == "__main__":
    port = int(os.environ.get("BOOKING_SERVICE_PORT", 5102))
    _init_vault()
    app.run(host="127.0.0.1", port=port)
