import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SERVICE_NAME", "booking_read_service")
import db
import tracer
from flask import Flask, request, jsonify, g
from auth_helper import require_role
from vault_helper import vault_approle_login, start_token_renewal

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "booking_read_service", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "read_service"})


@app.route("/execute", methods=["POST"])
@require_role("user", "bookings_admin", "admin")
def execute():
    current_user = g.current_user
    role = current_user["role"]
    user_id = current_user["user_id"] if role == "user" else None
    coll = getattr(g, "trace_collector", None) or tracer.get_current_collector()
    traces = coll.to_list() if coll else []

    data = request.get_json(silent=True)
    if not data or "tool" not in data:
        return jsonify({"error": "Invalid request: 'tool' field required", "traces": traces}), 400

    tool = data["tool"]
    args = data.get("args", {})
    logger.debug("tool=%s args=%s", tool, json.dumps(args))

    if tool == "get_booking":
        reference = args.get("reference")
        logger.debug("Calling db.get_booking reference=%r user_id=%s", reference, user_id)
        try:
            booking = db.get_booking(reference, user_id=user_id)
        except Exception as e:
            logger.exception("db.get_booking raised an exception")
            traces = coll.to_list() if coll else []
            return jsonify({"error": "Internal server error", "traces": traces}), 500
        logger.debug("get_booking returned: %s", json.dumps(booking))
        traces = coll.to_list() if coll else []
        if booking is None:
            logger.warning("get_booking: reference=%r not found or not accessible", reference)
            return jsonify({"error": "Booking not found", "traces": traces})
        return jsonify({"result": booking, "traces": traces})

    if tool == "list_bookings":
        passenger_name = args.get("passenger_name")
        logger.debug("Calling db.list_bookings passenger_name=%r user_id=%s", passenger_name, user_id)
        try:
            bookings = db.list_bookings(passenger_name=passenger_name, user_id=user_id)
        except Exception as e:
            logger.exception("db.list_bookings raised an exception")
            traces = coll.to_list() if coll else []
            return jsonify({"error": "Internal server error", "traces": traces}), 500
        logger.debug("list_bookings returned %d rows", len(bookings))
        traces = coll.to_list() if coll else []
        return jsonify({"result": bookings, "traces": traces})

    logger.error("Unknown tool: %s", tool)
    return jsonify({"error": f"Unknown tool: {tool}", "traces": traces}), 400


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
    os.environ["VAULT_DB_ROLE"] = "voyager_booking_read"
    start_token_renewal(client, logger)
    logger.info('{"event": "vault_init_complete", "db_role": "voyager_booking_read"}')


if __name__ == "__main__":
    port = int(os.environ.get("READ_SERVICE_PORT", 5101))
    _init_vault()
    app.run(host="127.0.0.1", port=port)
