import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SERVICE_NAME", "payment_write_service")
import db
import tracer
from flask import Flask, request, jsonify, g
from auth_helper import require_role
from vault_helper import vault_approle_login, start_token_renewal

_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "service": "payment_write_service", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "payment_write_service"})


@app.route("/execute", methods=["POST"])
@require_role("user", "payments_admin", "admin")
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

    if tool == "add_payment_card":
        logger.debug("Calling db.add_payment_card user_id=%s", current_user["user_id"])
        try:
            card = db.add_payment_card(
                cardholder_name=args["cardholder_name"],
                card_number=args["card_number"],
                card_brand=args["card_brand"],
                expiry=args["expiry"],
                billing_address=args["billing_address"],
                user_id=current_user["user_id"],  # always set user_id for new cards
            )
        except Exception as e:
            logger.exception("db.add_payment_card raised an exception")
            traces = coll.to_list() if coll else []
            return jsonify({"error": "Internal server error", "traces": traces}), 500
        logger.debug("add_payment_card result: %s", json.dumps(card))
        traces = coll.to_list() if coll else []
        return jsonify({"result": card, "traces": traces})

    if tool == "update_payment_card":
        card_id = args["card_id"]
        updatable = {"cardholder_name", "card_number", "card_brand", "expiry", "billing_address"}
        fields = {k: v for k, v in args.items() if k in updatable}
        logger.debug("Calling db.update_payment_card card_id=%s user_id=%s fields=%s",
                     card_id, user_id, list(fields.keys()))
        try:
            card = db.update_payment_card(card_id, user_id=user_id, **fields)
        except Exception as e:
            logger.exception("db.update_payment_card raised an exception")
            traces = coll.to_list() if coll else []
            return jsonify({"error": "Internal server error", "traces": traces}), 500
        logger.debug("update_payment_card returned: %s", json.dumps(card))
        traces = coll.to_list() if coll else []
        if card is None:
            logger.warning("update_payment_card: card_id=%s not found or not accessible", card_id)
            return jsonify({"error": "Card not found", "traces": traces})
        return jsonify({"result": card, "traces": traces})

    if tool == "remove_payment_card":
        card_id = args["card_id"]
        logger.debug("Calling db.remove_payment_card card_id=%s user_id=%s", card_id, user_id)
        try:
            removed = db.remove_payment_card(card_id, user_id=user_id)
        except Exception as e:
            logger.exception("db.remove_payment_card raised an exception")
            traces = coll.to_list() if coll else []
            return jsonify({"error": "Internal server error", "traces": traces}), 500
        logger.debug("remove_payment_card returned: %s", removed)
        traces = coll.to_list() if coll else []
        if not removed:
            logger.warning("remove_payment_card: card_id=%s not found or not accessible", card_id)
            return jsonify({"error": "Card not found", "traces": traces})
        return jsonify({"result": "Card removed", "traces": traces})

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
    os.environ["VAULT_DB_ROLE"] = "voyager_payment_write"
    start_token_renewal(client, logger)
    logger.info('{"event": "vault_init_complete", "db_role": "voyager_payment_write"}')


if __name__ == "__main__":
    port = int(os.environ.get("PAYMENT_WRITE_SERVICE_PORT", 5104))
    _init_vault()
    app.run(host="127.0.0.1", port=port)
