"""
tracer.py — Centralized tracing and instrumentation for Voyager Travel architecture visualization.

Provides trace context management, span recording, timing collection, and educational
metadata annotations for OIDC validation, Vault AppRole/dynamic DB credentials,
PostgreSQL queries, microservice dispatches, and LLM evaluations.
"""

import contextvars
import datetime
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

# Context variable for current thread/async request trace collector
_current_collector: contextvars.ContextVar[Optional["TraceCollector"]] = contextvars.ContextVar(
    "current_trace_collector", default=None
)

# Educational security concepts and explanations library
EDUCATIONAL_ANNOTATIONS = {
    "oidc_auth": {
        "security_concept": "Keycloak OIDC & RS256 JWKS Signature Verification",
        "explanation": (
            "The service validates the Bearer token's RS256 cryptographic signature against "
            "Keycloak's public keys fetched via the JWKS endpoint (/protocol/openid-connect/certs), "
            "verifies token expiration/issuer, and extracts realm roles for Role-Based Access Control (RBAC)."
        ),
    },
    "vault_creds": {
        "security_concept": "HashiCorp Vault Dynamic Database Secrets Engine",
        "explanation": (
            "Instead of static credentials, the microservice uses its Vault AppRole token to request "
            "a short-lived, ephemeral PostgreSQL credential with least-privilege permissions scoped "
            "to its specific database role. The credential lease automatically expires if not renewed."
        ),
    },
    "db_query": {
        "security_concept": "Least-Privilege PostgreSQL Database Access",
        "explanation": (
            "SQL query executed against PostgreSQL using ephemeral database credentials. "
            "Row-level ownership and role constraints restrict data access according to user identity."
        ),
    },
    "service_http": {
        "security_concept": "Microservice Isolation & Bearer Token Propagation",
        "explanation": (
            "The orchestrator dispatches tool requests to dedicated microservices over HTTP, "
            "forwarding the user's OIDC Bearer token to preserve authentication context and security boundaries."
        ),
    },
    "llm_eval": {
        "security_concept": "LLM Function Calling & Orchestration",
        "explanation": (
            "The orchestrator sends the user prompt and registered tool schemas to the LLM. "
            "The model decides whether to invoke tools (Phase 1) or stream the final natural language answer (Phase 2)."
        ),
    },
    "browser_auth": {
        "security_concept": "OAuth 2.0 PKCE (Proof Key for Code Exchange)",
        "explanation": (
            "The browser generates a cryptographically random code verifier and SHA-256 challenge, "
            "redirects to Keycloak for user login, and securely exchanges the authorization code for tokens."
        ),
    },
}


class TraceEvent:
    def __init__(
        self,
        trace_id: str,
        span_id: str,
        source: str,
        target: str,
        operation: str,
        span_type: str,
        timestamp: Optional[str] = None,
        duration_ms: float = 0.0,
        status: str = "success",
        parent_span_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.source = source
        self.target = target
        self.operation = operation
        self.type = span_type
        self.timestamp = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.duration_ms = round(duration_ms, 2)
        self.status = status
        self.details = details or {}

        # Enrich with educational annotations if not already provided
        annot = EDUCATIONAL_ANNOTATIONS.get(span_type, {})
        if "security_concept" not in self.details and "security_concept" in annot:
            self.details["security_concept"] = annot["security_concept"]
        if "explanation" not in self.details and "explanation" in annot:
            self.details["explanation"] = annot["explanation"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "source": self.source,
            "target": self.target,
            "operation": self.operation,
            "type": self.type,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "details": self.details,
        }


class TraceCollector:
    def __init__(self, trace_id: Optional[str] = None):
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.events: List[TraceEvent] = []

    def add_event(self, event: TraceEvent) -> None:
        self.events.append(event)

    def record(
        self,
        source: str,
        target: str,
        operation: str,
        span_type: str,
        duration_ms: float = 0.0,
        status: str = "success",
        parent_span_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        span_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> TraceEvent:
        event = TraceEvent(
            trace_id=self.trace_id,
            span_id=span_id or uuid.uuid4().hex[:12],
            parent_span_id=parent_span_id,
            source=source,
            target=target,
            operation=operation,
            span_type=span_type,
            timestamp=timestamp,
            duration_ms=duration_ms,
            status=status,
            details=details,
        )
        self.add_event(event)
        return event

    def to_list(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]


def get_current_collector() -> Optional[TraceCollector]:
    return _current_collector.get()


def set_current_collector(collector: Optional[TraceCollector]):
    return _current_collector.set(collector)


@contextmanager
def trace_scope(trace_id: Optional[str] = None):
    """Context manager to establish an active TraceCollector for the scope."""
    collector = TraceCollector(trace_id=trace_id)
    token = _current_collector.set(collector)
    try:
        yield collector
    finally:
        _current_collector.reset(token)


@contextmanager
def span(
    operation: str,
    source: str,
    target: str,
    span_type: str,
    details: Optional[Dict[str, Any]] = None,
    parent_span_id: Optional[str] = None,
    collector: Optional[TraceCollector] = None,
):
    """Context manager to measure duration and record a span."""
    coll = collector or get_current_collector()
    span_id = uuid.uuid4().hex[:12]
    start_time = time.monotonic()
    iso_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
    span_details = dict(details) if details else {}
    status = "success"
    try:
        yield span_id
    except Exception as exc:
        status = "error"
        span_details["error"] = str(exc)
        span_details["error_type"] = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - start_time) * 1000.0
        if coll:
            coll.record(
                source=source,
                target=target,
                operation=operation,
                span_type=span_type,
                duration_ms=duration_ms,
                status=status,
                parent_span_id=parent_span_id,
                details=span_details,
                span_id=span_id,
                timestamp=iso_time,
            )
