"""
tools.py — Ollama-compatible tool schemas and dispatcher for the Voyager Travel booking agent.

Exports:
    TOOL_DEFINITIONS  — list of 7 Ollama function-calling schema dicts
    dispatch_tool     — routes tool calls to db.py helpers
"""

import db

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": (
                "Create a new travel booking. Returns the confirmed booking including "
                "its unique reference code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "passenger_name": {
                        "type": "string",
                        "description": "Full name of the passenger.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Destination city or airport.",
                    },
                    "origin": {
                        "type": "string",
                        "description": "Origin city or airport.",
                    },
                    "travel_date": {
                        "type": "string",
                        "description": "Departure date in YYYY-MM-DD format.",
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Return date in YYYY-MM-DD format. Omit for one-way trips.",
                    },
                },
                "required": ["passenger_name", "destination", "origin", "travel_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_booking",
            "description": "Retrieve a single booking by its reference code (e.g. VT-00001).",
            "parameters": {
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "description": "The booking reference code, e.g. VT-00001.",
                    },
                },
                "required": ["reference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_bookings",
            "description": (
                "List all bookings, optionally filtered by passenger name. "
                "Returns a list of booking objects."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "passenger_name": {
                        "type": "string",
                        "description": (
                            "Filter bookings by passenger name (case-insensitive, partial match). "
                            "Omit to return all bookings."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Cancel an existing booking by its reference code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reference": {
                        "type": "string",
                        "description": "The booking reference code to cancel, e.g. VT-00001.",
                    },
                },
                "required": ["reference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_payment_card",
            "description": "Store a new payment card on the account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cardholder_name": {
                        "type": "string",
                        "description": "Full name of the cardholder.",
                    },
                    "card_number": {
                        "type": "string",
                        "description": "Card number (digits and spaces).",
                    },
                    "card_brand": {
                        "type": "string",
                        "description": "Card brand: Visa, Mastercard, Amex, or Discover.",
                    },
                    "expiry": {
                        "type": "string",
                        "description": "Card expiry date in MM/YYYY format.",
                    },
                    "billing_address": {
                        "type": "string",
                        "description": "Full billing address associated with the card.",
                    },
                },
                "required": [
                    "cardholder_name",
                    "card_number",
                    "card_brand",
                    "expiry",
                    "billing_address",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_payment_card",
            "description": (
                "Update one or more fields on an existing payment card. "
                "Only supply the fields that need to change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "The numeric ID of the card to update.",
                    },
                    "cardholder_name": {"type": "string", "description": "Updated cardholder name."},
                    "card_number": {"type": "string", "description": "Updated card number."},
                    "card_brand": {"type": "string", "description": "Updated card brand."},
                    "expiry": {"type": "string", "description": "Updated expiry in MM/YYYY format."},
                    "billing_address": {"type": "string", "description": "Updated billing address."},
                },
                "required": ["card_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_payment_card",
            "description": "Remove a payment card from the account by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "The numeric ID of the card to remove.",
                    },
                },
                "required": ["card_id"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_tool(name: str, args: dict) -> dict:
    """Route a tool call to the appropriate db.py helper.

    Always returns a plain dict:
      {"result": ...}  on success
      {"error": "..."}  on failure

    Exceptions are caught and returned as error dicts so the LLM can
    respond gracefully to failures.
    """
    try:
        if name == "create_booking":
            result = db.create_booking(
                passenger_name=args["passenger_name"],
                destination=args["destination"],
                origin=args["origin"],
                travel_date=args["travel_date"],
                return_date=args.get("return_date"),
            )
            return {"result": result}

        if name == "get_booking":
            result = db.get_booking(args["reference"])
            if result is None:
                return {"error": "Booking not found"}
            return {"result": result}

        if name == "list_bookings":
            result = db.list_bookings(passenger_name=args.get("passenger_name"))
            return {"result": result}

        if name == "cancel_booking":
            result = db.cancel_booking(args["reference"])
            if result is None:
                return {"error": "Booking not found"}
            return {"result": result}

        if name == "add_payment_card":
            result = db.add_payment_card(
                cardholder_name=args["cardholder_name"],
                card_number=args["card_number"],
                card_brand=args["card_brand"],
                expiry=args["expiry"],
                billing_address=args["billing_address"],
            )
            return {"result": result}

        if name == "update_payment_card":
            card_id = args["card_id"]
            other_fields = {k: v for k, v in args.items() if k != "card_id"}
            result = db.update_payment_card(card_id, **other_fields)
            if result is None:
                return {"error": "Card not found"}
            return {"result": result}

        if name == "remove_payment_card":
            removed = db.remove_payment_card(args["card_id"])
            if removed:
                return {"result": "Card removed successfully"}
            return {"error": "Card not found"}

        return {"error": f"Unknown tool: {name}"}

    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
