"""
auth_helper.py — Shared JWT authentication and role-based access control helper.

Validates JWTs issued by Keycloak (RS256, realm_access.roles) and provides a
Flask decorator that enforces role membership and injects g.current_user.

By default (``provision=False``) both ``validate_and_identify`` and
``require_role`` perform a **read-only** lookup of the user row by
``keycloak_sub`` (via ``db.get_user_by_sub``).  A missing user returns ``None``
and ``require_role`` responds with 401.  This is the correct mode for all
agent services.

Only the orchestrator (``app.py``) passes ``provision=True`` so that a new
Keycloak user is automatically inserted on first login via
``db.get_or_create_user``.

Configuration (environment variables)
--------------------------------------
KEYCLOAK_BASE_URL   Base URL of the Keycloak server, e.g. http://127.0.0.1:8080
                    Defaults to http://127.0.0.1:8080.
KEYCLOAK_REALM      Realm name. Defaults to "voyager".

The module is placed at the project root alongside db.py so that both app.py
and the four agent microservices (which already prepend the project root to
sys.path) can import it with a plain `import auth_helper`.
"""

import logging
import os
import time
from functools import wraps
from typing import Optional

import jwt
import requests as http_requests
from flask import g, jsonify, request

import db
import tracer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_KEYCLOAK_BASE_URL = os.environ.get("KEYCLOAK_BASE_URL", "http://127.0.0.1:8080")
_KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "voyager")
_ISSUER = f"{_KEYCLOAK_BASE_URL}/realms/{_KEYCLOAK_REALM}"
_JWKS_URI = f"{_ISSUER}/protocol/openid-connect/certs"

# Role priority — used to pick the single most-privileged role when a token
# carries more than one.
_ROLE_PRIORITY = ["admin", "bookings_admin", "payments_admin", "user"]

# ---------------------------------------------------------------------------
# JWKS cache — (jwks_dict, fetched_at_epoch)
# ---------------------------------------------------------------------------

_jwks_cache: tuple[Optional[dict], float] = (None, 0.0)
_JWKS_TTL = 300  # 5 minutes


def _get_jwks() -> dict:
    """Return the Keycloak JWKS JSON, refreshing the cache every 5 minutes."""
    global _jwks_cache
    data, fetched_at = _jwks_cache
    if data is None or (time.monotonic() - fetched_at) > _JWKS_TTL:
        try:
            resp = http_requests.get(_JWKS_URI, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            _jwks_cache = (data, time.monotonic())
            logger.debug('{"event": "jwks_refreshed", "uri": "%s"}', _JWKS_URI)
        except Exception as exc:
            logger.warning('{"event": "jwks_fetch_failed", "reason": "%s"}', type(exc).__name__)
            if data is None:
                raise
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_and_identify(
    token: str,
    service_name: Optional[str] = None,
    provision: bool = False,
) -> Optional[dict]:
    """Decode and validate a Keycloak JWT.

    Returns a dict:
        {"user_id": int, "sub": str, "username": str, "role": str}
    on success, or None on any failure (expired, bad signature, missing claims,
    unknown user, …).

    Args:
        token:        Raw bearer token (without the "Bearer " prefix).
        service_name: Optional service name for trace spans; defaults to the
                      SERVICE_NAME environment variable or "orchestrator".
        provision:    When True, call db.get_or_create_user so a new Keycloak
                      user is inserted on first login (orchestrator only).
                      When False (default), call db.get_user_by_sub instead;
                      a missing user returns None and the caller should 401.

    Never logs the token value itself.
    """
    src = service_name or os.environ.get("SERVICE_NAME", "orchestrator")
    collector = tracer.get_current_collector()

    if not token:
        if collector:
            collector.record(
                source=src,
                target="keycloak",
                operation="OIDC Token Validation",
                span_type="oidc_auth",
                status="error",
                details={"error": "Missing bearer token", "issuer": _ISSUER},
            )
        return None

    start_time = time.monotonic()
    iso_time = datetime_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        jwks_data = _get_jwks()
        jwk_set = jwt.PyJWKSet.from_dict(jwks_data)
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        try:
            signing_key = jwk_set[kid]
        except KeyError:
            raise jwt.InvalidKeyError(f"No matching key found for kid={kid!r}")

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_ISSUER,
            options={
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": False,
                "require": ["sub", "exp", "iss"],
            },
        )
    except jwt.ExpiredSignatureError:
        logger.warning('{"event": "jwt_rejected", "reason": "expired"}')
        if collector:
            collector.record(
                source=src,
                target="keycloak",
                operation="OIDC Token RS256 Verification",
                span_type="oidc_auth",
                duration_ms=(time.monotonic() - start_time) * 1000.0,
                status="error",
                details={"error": "Token expired", "issuer": _ISSUER},
            )
        return None
    except jwt.InvalidIssuerError:
        logger.warning('{"event": "jwt_rejected", "reason": "invalid_issuer"}')
        if collector:
            collector.record(
                source=src,
                target="keycloak",
                operation="OIDC Token RS256 Verification",
                span_type="oidc_auth",
                duration_ms=(time.monotonic() - start_time) * 1000.0,
                status="error",
                details={"error": "Invalid issuer", "issuer": _ISSUER},
            )
        return None
    except jwt.PyJWTError as exc:
        logger.warning('{"event": "jwt_rejected", "reason": "%s"}', type(exc).__name__)
        if collector:
            collector.record(
                source=src,
                target="keycloak",
                operation="OIDC Token RS256 Verification",
                span_type="oidc_auth",
                duration_ms=(time.monotonic() - start_time) * 1000.0,
                status="error",
                details={"error": str(exc), "issuer": _ISSUER},
            )
        return None
    except Exception as exc:
        logger.warning('{"event": "jwt_rejected", "reason": "%s"}', type(exc).__name__)
        if collector:
            collector.record(
                source=src,
                target="keycloak",
                operation="OIDC Token RS256 Verification",
                span_type="oidc_auth",
                duration_ms=(time.monotonic() - start_time) * 1000.0,
                status="error",
                details={"error": str(exc), "issuer": _ISSUER},
            )
        return None

    sub = payload.get("sub")
    username = payload.get("preferred_username") or payload.get("sub")
    realm_roles: list = payload.get("realm_access", {}).get("roles", [])

    # Pick the single highest-priority role the token carries
    role = next((r for r in _ROLE_PRIORITY if r in realm_roles), None)
    if role is None:
        logger.warning('{"event": "jwt_rejected", "reason": "no_recognised_role"}')
        if collector:
            collector.record(
                source=src,
                target="keycloak",
                operation="OIDC Role Resolution",
                span_type="oidc_auth",
                duration_ms=(time.monotonic() - start_time) * 1000.0,
                status="error",
                details={"error": "No recognised role in token", "realm_roles": realm_roles},
            )
        return None

    if provision:
        try:
            user = db.get_or_create_user(sub, username, role)
        except Exception as exc:
            logger.warning('{"event": "jwt_user_provision_failed", "reason": "%s"}', type(exc).__name__)
            if collector:
                collector.record(
                    source=src,
                    target="keycloak",
                    operation="JIT User Provisioning",
                    span_type="oidc_auth",
                    duration_ms=(time.monotonic() - start_time) * 1000.0,
                    status="error",
                    details={"error": str(exc), "sub": sub, "username": username, "role": role},
                )
            return None
    else:
        try:
            user = db.get_user_by_sub(sub)
        except Exception as exc:
            logger.warning('{"event": "jwt_user_lookup_failed", "reason": "%s"}', type(exc).__name__)
            if collector:
                collector.record(
                    source=src,
                    target="keycloak",
                    operation="User Lookup",
                    span_type="oidc_auth",
                    duration_ms=(time.monotonic() - start_time) * 1000.0,
                    status="error",
                    details={"error": str(exc), "sub": sub, "username": username, "role": role},
                )
            return None

        if user is None:
            logger.error(
                '{"event": "user_not_provisioned", "reason": "sub not found in users table",'
                ' "sub": "%s", "username": "%s", "role": "%s"}',
                sub, username, role,
            )
            if collector:
                collector.record(
                    source=src,
                    target="keycloak",
                    operation="User Lookup",
                    span_type="oidc_auth",
                    duration_ms=(time.monotonic() - start_time) * 1000.0,
                    status="error",
                    details={
                        "error": "User not provisioned",
                        "sub": sub,
                        "username": username,
                        "role": role,
                    },
                )
            return None

    duration_ms = (time.monotonic() - start_time) * 1000.0
    if collector:
        collector.record(
            source=src,
            target="keycloak",
            operation="OIDC Token RS256 Verification",
            span_type="oidc_auth",
            duration_ms=duration_ms,
            status="success",
            details={
                "issuer": _ISSUER,
                "jwks_uri": _JWKS_URI,
                "sub": sub,
                "username": username,
                "role": role,
                "algorithm": "RS256",
                "user_id": user["id"],
            },
        )

    return {
        "user_id": user["id"],
        "sub": sub,
        "username": username,
        "role": role,
    }


def require_role(*allowed_roles: str, provision: bool = False):
    """Flask decorator — validate the bearer token and enforce role membership.

    On success, sets flask.g.current_user to the dict returned by
    validate_and_identify and calls the wrapped view function.

    Args:
        *allowed_roles: Role names that are permitted to call the view.
        provision:      Passed through to validate_and_identify.  Set to True
                        only on orchestrator routes so new Keycloak users are
                        provisioned on first login.  Defaults to False
                        (read-only lookup; missing user → 401).

    Returns:
        401 JSON  — missing or invalid token, or user not in users table
        403 JSON  — valid token but role not in allowed_roles
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Check for trace context from header if present
            trace_id = request.headers.get("X-Trace-Id")
            coll = tracer.get_current_collector()
            created_scope = False
            token_scope = None
            if coll is None and trace_id:
                coll = tracer.TraceCollector(trace_id=trace_id)
                token_scope = tracer.set_current_collector(coll)
                created_scope = True
            g.trace_collector = coll

            try:
                auth_header = request.headers.get("Authorization", "")
                token = auth_header.removeprefix("Bearer ").strip()
                user = validate_and_identify(token, provision=provision)
                if user is None:
                    return jsonify({"error": "Unauthorized"}), 401
                if user["role"] not in allowed_roles:
                    return jsonify({"error": "Forbidden"}), 403
                g.current_user = user
                return fn(*args, **kwargs)
            finally:
                if created_scope and token_scope is not None:
                    tracer.set_current_collector(None)
        return wrapper
    return decorator
