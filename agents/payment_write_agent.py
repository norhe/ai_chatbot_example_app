import sys
import json
import logging
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

# ---------------------------------------------------------------------------
# Debug logging — controlled by the DEBUG env var (1 / true / yes)
# Writes to stderr so it never contaminates the stdout JSON protocol.
# ---------------------------------------------------------------------------
_debug = os.environ.get("DEBUG", "").strip("\"'").lower() in ("1", "true", "yes")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG if _debug else logging.WARNING,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "agent": "payment_write_agent", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)


def validate_token(token: str) -> bool:
    # TODO: replace with external auth service call
    # e.g. return requests.post(AUTH_SERVICE_URL, json={"token": token}).json()["valid"]
    logger.debug("validate_token: token=%r", token)
    return True


def main():
    raw = sys.stdin.read()
    logger.debug("stdin received: %s", raw[:500])

    payload = json.loads(raw)
    token = payload.get("auth_token", "")

    if not validate_token(token):
        logger.warning("Auth failed for token=%r", token)
        print(json.dumps({"error": "Unauthorized"}))
        sys.exit(1)

    tool = payload["tool"]
    args = payload.get("args", {})
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
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        logger.debug("add_payment_card result: %s", json.dumps(card))
        print(json.dumps({"result": card}))
        return

    if tool == "update_payment_card":
        card_id = args["card_id"]
        updatable = {"cardholder_name", "card_number", "card_brand", "expiry", "billing_address"}
        fields = {k: v for k, v in args.items() if k in updatable}
        logger.debug("Calling db.update_payment_card card_id=%s fields=%s", card_id, list(fields.keys()))
        try:
            card = db.update_payment_card(card_id, **fields)
        except Exception as e:
            logger.exception("db.update_payment_card raised an exception")
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        logger.debug("update_payment_card returned: %s", json.dumps(card))
        if card is None:
            logger.warning("update_payment_card: card_id=%s not found", card_id)
            print(json.dumps({"error": "Card not found"}))
            sys.exit(1)
        print(json.dumps({"result": card}))
        return

    if tool == "remove_payment_card":
        card_id = args["card_id"]
        logger.debug("Calling db.remove_payment_card card_id=%s", card_id)
        try:
            removed = db.remove_payment_card(card_id)
        except Exception as e:
            logger.exception("db.remove_payment_card raised an exception")
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        logger.debug("remove_payment_card returned: %s", removed)
        if not removed:
            logger.warning("remove_payment_card: card_id=%s not found", card_id)
            print(json.dumps({"error": "Card not found"}))
            sys.exit(1)
        print(json.dumps({"result": "Card removed"}))
        return

    logger.error("Unknown tool: %s", tool)
    print(json.dumps({"error": f"Unknown tool: {tool}"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
