import logging, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from flask import Flask, request, jsonify
from vault_helper import vault_approle_login, start_token_renewal

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
    os.environ["VAULT_DB_ROLE"] = "voyager_payment_read"
    start_token_renewal(client, logger)
    logger.info('{"event": "vault_init_complete", "db_role": "voyager_payment_read"}')


if __name__ == "__main__":
    port = int(os.environ.get("PAYMENT_READ_SERVICE_PORT", 5103))
    _init_vault()
    app.run(host="127.0.0.1", port=port)
