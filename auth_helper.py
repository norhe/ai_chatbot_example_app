"""
auth_helper.py — Shared JWT authentication and role-based access control helper.

Validates JWTs issued by Keycloak (RS256, realm_access.roles), performs JIT
user provisioning via db.get_or_create_user, and provides a Flask decorator
that enforces role membership and injects g.current_user.

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

def validate_and_identify(token: str) -> Optional[dict]:
    """Decode and validate a Keycloak JWT.

    Returns a dict:
        {"user_id": int, "sub": str, "username": str, "role": str}
    on success, or None on any failure (expired, bad signature, missing claims, …).

    Never logs the token value itself.
    """
    if not token:
        return None

    try:
        jwks_data = _get_jwks()
        jwks = jwt.PyJWKClient.__new__(jwt.PyJWKClient)
        signing_key = jwt.PyJWKSet.from_dict(jwks_data).get_signing_key_from_jwt(token)

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_ISSUER,
            options={
                "verify_exp": True,
                "verify_iss": True,
                "require": ["sub", "exp", "iss"],
            },
        )
    except jwt.ExpiredSignatureError:
        logger.warning('{"event": "jwt_rejected", "reason": "expired"}')
        return None
    except jwt.InvalidIssuerError:
        logger.warning('{"event": "jwt_rejected", "reason": "invalid_issuer"}')
        return None
    except jwt.PyJWTError as exc:
        logger.warning('{"event": "jwt_rejected", "reason": "%s"}', type(exc).__name__)
        return None
    except Exception as exc:
        logger.warning('{"event": "jwt_rejected", "reason": "%s"}', type(exc).__name__)
        return None

    sub = payload.get("sub")
    username = payload.get("preferred_username") or payload.get("sub")
    realm_roles: list = payload.get("realm_access", {}).get("roles", [])

    # Pick the single highest-priority role the token carries
    role = next((r for r in _ROLE_PRIORITY if r in realm_roles), None)
    if role is None:
        logger.warning('{"event": "jwt_rejected", "reason": "no_recognised_role"}')
        return None

    try:
        user = db.get_or_create_user(sub, username, role)
    except Exception as exc:
        logger.warning('{"event": "jwt_user_provision_failed", "reason": "%s"}', type(exc).__name__)
        return None

    return {
        "user_id": user["id"],
        "sub": sub,
        "username": username,
        "role": role,
    }


def require_role(*allowed_roles: str):
    """Flask decorator — validate the bearer token and enforce role membership.

    On success, sets flask.g.current_user to the dict returned by
    validate_and_identify and calls the wrapped view function.

    Returns:
        401 JSON  — missing or invalid token
        403 JSON  — valid token but role not in allowed_roles
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            user = validate_and_identify(token)
            if user is None:
                return jsonify({"error": "Unauthorized"}), 401
            if user["role"] not in allowed_roles:
                return jsonify({"error": "Forbidden"}), 403
            g.current_user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator
