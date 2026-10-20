"""
tools.py — Ollama-compatible tool schemas and dispatcher for the Voyager Travel booking agent.

Exports:
    TOOL_DEFINITIONS  — list of 7 Ollama function-calling schema dicts
    AGENT_URLS        — maps each tool name to its agent microservice base URL
    dispatch_tool     — POSTs to the appropriate agent microservice and returns its JSON result
"""

import json
import logging
import os
import requests
import tracer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent service URL routing table
# ---------------------------------------------------------------------------

_AGENT_BASE = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1")

AGENT_URLS = {
    "get_booking":          f"{_AGENT_BASE}:5101",
    "list_bookings":        f"{_AGENT_BASE}:5101",
    "create_booking":       f"{_AGENT_BASE}:5102",
    "cancel_booking":       f"{_AGENT_BASE}:5102",
    "list_payment_cards":   f"{_AGENT_BASE}:5103",
    "add_payment_card":     f"{_AGENT_BASE}:5104",
    "update_payment_card":  f"{_AGENT_BASE}:5104",
    "remove_payment_card":  f"{_AGENT_BASE}:5104",
}

TOOL_SERVICES = {
    "get_booking":          "booking_read_service",
    "list_bookings":        "booking_read_service",
    "create_booking":       "booking_mutation_service",
    "cancel_booking":       "booking_mutation_service",
    "list_payment_cards":   "payment_read_service",
    "add_payment_card":     "payment_write_service",
    "update_payment_card":  "payment_write_service",
    "remove_payment_card":  "payment_write_service",
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

def dispatch_tool(name: str, args: dict, token: str = "", trace_id: str = "") -> dict:
    """POST to the appropriate agent microservice and return its JSON result.

    Args:
        name:     Tool name to dispatch.
        args:     Tool arguments dict.
        token:    Bearer token forwarded from the inbound user request.
        trace_id: Active trace ID.

    Always returns a plain dict:
      {"result": ..., "traces": [...]}  on success
      {"error": "...", "traces": [...]}  on failure or unknown tool
    """
    base_url = AGENT_URLS.get(name)
    target_service = TOOL_SERVICES.get(name, "agent_service")
    collector = tracer.get_current_collector()
    effective_trace_id = trace_id or (collector.trace_id if collector else "")

    if not base_url:
        logger.error("dispatch_tool: no service URL for tool=%s", name)
        if collector:
            collector.record(
                source="orchestrator",
                target=target_service,
                operation=f"Tool Dispatch: {name}",
                span_type="service_http",
                status="error",
                details={"tool": name, "args": args, "error": f"Unknown tool: {name}"},
            )
        return {"error": f"Unknown tool: {name}"}

    if not token:
        token = os.environ.get("AUTH_TOKEN", "")
    logger.debug("dispatch_tool: POST %s/execute tool=%s", base_url, name)

    headers = {"Authorization": f"Bearer {token}"}
    if effective_trace_id:
        headers["X-Trace-Id"] = effective_trace_id

    with tracer.span(
        operation=f"Tool Dispatch: {name}",
        source="orchestrator",
        target=target_service,
        span_type="service_http",
        details={"tool": name, "args": args, "service_url": base_url},
    ):
        try:
            resp = requests.post(
                f"{base_url}/execute",
                json={"tool": name, "args": args},
                headers=headers,
                timeout=30,
            )
            logger.debug("dispatch_tool: service=%s status=%d", base_url, resp.status_code)
            if resp.status_code == 401:
                logger.error("dispatch_tool: service=%s returned 401 Unauthorized", base_url)
                return {"error": "Unauthorized"}
            if resp.status_code != 200:
                logger.error("dispatch_tool: service=%s HTTP %d body=%s",
                             base_url, resp.status_code, resp.text[:500])
                return {"error": f"Agent service error: HTTP {resp.status_code}"}
            result_json = resp.json()
            # If agent returned remote traces, integrate them into active collector
            remote_traces = result_json.get("traces", [])
            if collector and remote_traces:
                for t in remote_traces:
                    collector.record(
                        source=t.get("source", target_service),
                        target=t.get("target", "unknown"),
                        operation=t.get("operation", "Agent Operation"),
                        span_type=t.get("type", "service_http"),
                        duration_ms=t.get("duration_ms", 0.0),
                        status=t.get("status", "success"),
                        parent_span_id=t.get("parent_span_id"),
                        details=t.get("details", {}),
                        span_id=t.get("span_id"),
                        timestamp=t.get("timestamp"),
                    )
            return result_json
        except requests.Timeout:
            logger.error("dispatch_tool: service=%s timed out", base_url)
            return {"error": "Agent timed out"}
        except requests.RequestException as e:
            logger.error("dispatch_tool: service=%s connection error: %s", base_url, e)
            return {"error": f"Agent unreachable: {e}"}
        except Exception as e:  # noqa: BLE001
            logger.exception("dispatch_tool: unexpected error for service=%s", base_url)
            return {"error": str(e)}
