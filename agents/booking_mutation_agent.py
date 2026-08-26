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
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "agent": "booking_mutation_agent", "message": "%(message)s"}',
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
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        logger.debug("create_booking result: %s", json.dumps(booking))
        print(json.dumps({"result": booking}))
        return

    if tool == "cancel_booking":
        reference = args.get("reference")
        logger.debug("Calling db.cancel_booking reference=%r", reference)
        try:
            booking = db.cancel_booking(reference)
        except Exception as e:
            logger.exception("db.cancel_booking raised an exception")
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        logger.debug("cancel_booking returned: %s", json.dumps(booking))
        if booking is None:
            logger.warning("cancel_booking: reference=%r not found", reference)
            print(json.dumps({"error": "Booking not found"}))
            sys.exit(1)
        print(json.dumps({"result": booking}))
        return

    logger.error("Unknown tool: %s", tool)
    print(json.dumps({"error": f"Unknown tool: {tool}"}))
    sys.exit(1)


if __name__ == "__main__":
    main()
