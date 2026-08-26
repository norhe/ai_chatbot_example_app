"""
tools.py — Ollama-compatible tool schemas and dispatcher for the Voyager Travel booking agent.

Exports:
    TOOL_DEFINITIONS  — list of 7 Ollama function-calling schema dicts
    TOOL_AGENT_MAP    — maps each tool name to its agent script path
    dispatch_tool     — spawns the appropriate agent subprocess and returns its JSON result
"""

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent routing table
# ---------------------------------------------------------------------------

TOOL_AGENT_MAP = {
    "get_booking":          "agents/read_agent.py",
    "list_bookings":        "agents/read_agent.py",
    "create_booking":       "agents/booking_mutation_agent.py",
    "cancel_booking":       "agents/booking_mutation_agent.py",
    "list_payment_cards":   "agents/payment_read_agent.py",
    "add_payment_card":     "agents/payment_write_agent.py",
    "update_payment_card":  "agents/payment_write_agent.py",
    "remove_payment_card":  "agents/payment_write_agent.py",
}

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
    """Spawn the appropriate agent subprocess and return its JSON result.

    Always returns a plain dict:
      {"result": ...}  on success
      {"error": "..."}  on failure or unknown tool
    """
    agent_script = TOOL_AGENT_MAP.get(name)
    if not agent_script:
        logger.error("dispatch_tool: no agent mapped for tool=%s", name)
        return {"error": f"Unknown tool: {name}"}

    payload = json.dumps({
        "tool": name,
        "args": args,
        "auth_token": os.environ.get("AUTH_TOKEN", "dev-token"),
    })

    logger.debug("dispatch_tool: spawning agent=%s tool=%s", agent_script, name)

    try:
        result = subprocess.run(
            [sys.executable, agent_script],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.debug("dispatch_tool: agent=%s exit_code=%d stdout=%r stderr=%r",
                     agent_script, result.returncode,
                     result.stdout[:500], result.stderr[:500])
        if result.returncode != 0:
            logger.error("dispatch_tool: agent=%s failed exit_code=%d stderr=%s",
                         agent_script, result.returncode, result.stderr.strip())
            return {"error": f"Agent execution failed: {result.stderr.strip() or 'no stderr'}"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error("dispatch_tool: agent=%s returned non-JSON stdout=%r", agent_script, result.stdout[:500])
            return {"error": "Agent returned invalid response"}
    except subprocess.TimeoutExpired:
        logger.error("dispatch_tool: agent=%s timed out", agent_script)
        return {"error": "Agent timed out"}
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch_tool: unexpected error for agent=%s", agent_script)
        return {"error": str(e)}
