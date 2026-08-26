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
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "agent": "payment_read_agent", "message": "%(message)s"}',
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
    logger.debug("tool=%s", tool)

    if tool == "list_payment_cards":
        logger.debug("Calling db.list_payment_cards")
        try:
            cards = db.list_payment_cards()
        except Exception as e:
            logger.exception("db.list_payment_cards raised an exception")
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        logger.debug("list_payment_cards returned %d rows", len(cards))
        print(json.dumps({"result": cards}))
        return

    logger.error("Unknown tool: %s", tool)
    print(json.dumps({"error": f"Unknown tool: {tool}"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
