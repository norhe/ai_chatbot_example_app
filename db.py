"""
db.py — PostgreSQL data access layer for the Voyager Travel booking agent.

All database access is centralised here. Nothing else in the app should
import psycopg2 directly.

Environment variables
---------------------
Standard (always required for direct schema-init runs):
  DATABASE_URL   Full PostgreSQL DSN, e.g. postgresql://user:pass@host:5432/dbname

Vault mode (used by microservices — all four must be set together):
  VAULT_ADDR     Vault server address, e.g. http://127.0.0.1:8200
  VAULT_TOKEN    Vault token set by agents/vault_helper.py at startup
  VAULT_DB_ROLE  Vault database role name to request credentials for
                 (e.g. voyager_read, voyager_booking_write ...)
  DB_HOST        PostgreSQL host parsed from DATABASE_URL by start_app.sh
  DB_PORT        PostgreSQL port
  DB_NAME        PostgreSQL database name

When VAULT_DB_ROLE is set, get_connection() fetches ephemeral credentials
from Vault on every call. When it is absent, DATABASE_URL is used directly
(preserving the ability to run `python db.py` for schema init and seeding).
"""

import logging
import os
import psycopg2
import psycopg2.extras
import hvac

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _get_vault_db_credentials(role: str) -> tuple[str, str]:
    """Fetch a fresh ephemeral (username, password) from Vault's database secrets engine.

    Reads VAULT_ADDR and VAULT_TOKEN from the environment.  The token is
    managed (kept alive) by the background renewal thread in vault_helper.py.
    """
    vault_addr = os.environ["VAULT_ADDR"]
    logger.debug(
        '{"event": "vault_db_creds_request", "role": "%s", "vault_addr": "%s"}',
        role, vault_addr,
    )
    try:
        client = hvac.Client(url=vault_addr, token=os.environ["VAULT_TOKEN"])
        response = client.secrets.database.generate_credentials(name=role)
        data = response["data"]
        logger.debug(
            '{"event": "vault_db_creds_issued", "role": "%s", "lease_id": "%s", "lease_duration": %s}',
            role,
            response.get("lease_id", ""),
            response.get("lease_duration", ""),
        )
        return data["username"], data["password"]
    except Exception:
        logger.exception(
            '{"event": "vault_db_creds_failed", "role": "%s"}', role,
        )
        raise


def get_connection():
    """Return a new psycopg2 connection.

    When VAULT_DB_ROLE is set, fetches ephemeral credentials from Vault and
    builds the DSN from DB_HOST / DB_PORT / DB_NAME env vars.
    Falls back to DATABASE_URL when VAULT_DB_ROLE is absent (e.g. direct runs
    of `python db.py` for schema init and seeding).

    Raises RuntimeError if neither path has the required env vars.
    """
    vault_role = os.environ.get("VAULT_DB_ROLE")
    if vault_role:
        user, password = _get_vault_db_credentials(vault_role)
        host = os.environ["DB_HOST"]
        port = os.environ["DB_PORT"]
        name = os.environ["DB_NAME"]
        url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
        logger.debug(
            '{"event": "vault_db_connect", "role": "%s", "host": "%s", "port": "%s", "db": "%s"}',
            vault_role, host, port, name,
        )
    else:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "Expected format: postgresql://user:pass@host:5432/dbname"
            )
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables if they do not already exist, and add FK columns
    to existing tables. Safe to call on every startup (fully idempotent)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Users table — keyed on Keycloak subject (sub) UUID
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id              SERIAL PRIMARY KEY,
                    keycloak_sub    VARCHAR(36) UNIQUE NOT NULL,
                    username        VARCHAR(120) NOT NULL,
                    role            VARCHAR(30) NOT NULL,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id              SERIAL PRIMARY KEY,
                    reference       VARCHAR(8) UNIQUE,
                    passenger_name  VARCHAR(120) NOT NULL,
                    destination     VARCHAR(120) NOT NULL,
                    origin          VARCHAR(120) NOT NULL,
                    travel_date     DATE NOT NULL,
                    return_date     DATE,
                    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # ---------------------------------------------------------------
            # SECURITY WARNING — DEMO ONLY
            # Storing raw card numbers is a severe PCI-DSS violation in any
            # real system. This table exists ONLY to demonstrate CRUD
            # mechanics in a local example app. In production, NEVER store
            # card numbers — use a PCI-compliant tokenisation service such as
            # Stripe, IBM Hyper Protect Crypto Services, or a dedicated vault.
            # ---------------------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_cards (
                    id               SERIAL PRIMARY KEY,
                    cardholder_name  VARCHAR(120) NOT NULL,
                    card_number      VARCHAR(19) NOT NULL,
                    card_brand       VARCHAR(20) NOT NULL,
                    expiry           VARCHAR(7) NOT NULL,
                    billing_address  TEXT NOT NULL,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Add user_id FK columns to existing tables (idempotent)
            cur.execute("""
                ALTER TABLE bookings
                ADD COLUMN IF NOT EXISTS user_id INT REFERENCES users(id)
            """)
            cur.execute("""
                ALTER TABLE payment_cards
                ADD COLUMN IF NOT EXISTS user_id INT REFERENCES users(id)
            """)

        conn.commit()


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

# Placeholder sub UUIDs for demo users; replaced by seed-users CLI subcommand
# after running setup_keycloak_kcadm.sh which writes real Keycloak sub values.
_DEMO_USERS = [
    # (username, keycloak_sub placeholder, role)
    ("alice",        "00000000-0000-0000-0000-000000000001", "user"),
    ("bob",          "00000000-0000-0000-0000-000000000002", "user"),
    ("carol",        "00000000-0000-0000-0000-000000000003", "user"),
    ("david",        "00000000-0000-0000-0000-000000000004", "user"),
    ("bookings_mgr", "00000000-0000-0000-0000-000000000005", "bookings_admin"),
    ("payments_mgr", "00000000-0000-0000-0000-000000000006", "payments_admin"),
    ("admin_user",   "00000000-0000-0000-0000-000000000007", "admin"),
]


def seed_db():
    """Insert fake data only when the tables are empty (idempotent)."""
    with get_connection() as conn:
        with conn.cursor() as cur:

            # ------------------------------------------------------------------
            # Seed demo users (placeholder sub UUIDs)
            # ------------------------------------------------------------------
            for (username, sub, role) in _DEMO_USERS:
                cur.execute(
                    """
                    INSERT INTO users (keycloak_sub, username, role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (keycloak_sub) DO NOTHING
                    """,
                    (sub, username, role),
                )

            # Build a username → user_id lookup for linking seed rows
            cur.execute("SELECT id, username FROM users")
            user_id_map = {row["username"]: row["id"] for row in cur.fetchall()}

            # Seed bookings
            cur.execute("SELECT COUNT(*) FROM bookings")
            if cur.fetchone()["count"] == 0:
                bookings = [
                    ("Alice Johnson",   "alice",        "Paris",       "New York",     "2025-06-10", "2025-06-24", "confirmed"),
                    ("Bob Martinez",    "bob",           "Tokyo",       "Los Angeles",  "2025-07-15", "2025-07-29", "confirmed"),
                    ("Carol Williams",  "carol",         "London",      "Chicago",      "2025-08-01", None,          "pending"),
                    ("David Lee",       "david",         "Sydney",      "San Francisco","2025-09-05", "2025-09-19", "confirmed"),
                    ("Eva Rossi",       None,            "Rome",        "Boston",       "2025-05-20", "2025-05-30", "cancelled"),
                    ("Frank Chen",      None,            "Dubai",       "Toronto",      "2025-10-12", "2025-10-22", "pending"),
                    ("Grace Kim",       None,            "Barcelona",   "Miami",        "2025-11-03", "2025-11-17", "confirmed"),
                    ("Henry Okafor",    None,            "Cape Town",   "London",       "2025-12-15", None,          "pending"),
                ]
                for (pname, uname, dest, orig, tdate, rdate, status) in bookings:
                    uid = user_id_map.get(uname) if uname else None
                    cur.execute(
                        """
                        INSERT INTO bookings
                            (passenger_name, destination, origin, travel_date, return_date, status, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (pname, dest, orig, tdate, rdate, status, uid),
                    )
                    row_id = cur.fetchone()["id"]
                    reference = "VT-" + str(row_id).zfill(5)
                    cur.execute(
                        "UPDATE bookings SET reference = %s WHERE id = %s",
                        (reference, row_id),
                    )

            # Seed payment cards
            # ---------------------------------------------------------------
            # DEMO ONLY — synthetic test card numbers below are public
            # well-known test values and carry no real financial data.
            # NEVER store real card numbers in any database.
            # ---------------------------------------------------------------
            cur.execute("SELECT COUNT(*) FROM payment_cards")
            if cur.fetchone()["count"] == 0:
                cards = [
                    ("Alice Johnson",  "alice",  "4111 1111 1111 1111", "Visa",       "09/2027", "123 Main St, New York, NY 10001"),
                    ("Bob Martinez",   "bob",    "5500 0000 0000 0004", "Mastercard", "03/2026", "456 Oak Ave, Los Angeles, CA 90001"),
                    ("Carol Williams", "carol",  "3714 496353 98431",   "Amex",       "11/2028", "789 Pine Rd, Chicago, IL 60601"),
                    ("David Lee",      "david",  "6011 1111 1111 1117", "Discover",   "06/2027", "321 Elm St, San Francisco, CA 94101"),
                ]
                for (cname, uname, cnum, brand, expiry, addr) in cards:
                    uid = user_id_map.get(uname)
                    cur.execute(
                        """
                        INSERT INTO payment_cards
                            (cardholder_name, card_number, card_brand, expiry, billing_address, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (cname, cnum, brand, expiry, addr, uid),
                    )

        conn.commit()


def seed_users_from_env():
    """Patch demo user keycloak_sub values from environment variables.

    Reads <USERNAME_UPPER>_SUB env vars (e.g. ALICE_SUB, BOOKINGS_MGR_SUB)
    and updates the users table using ON CONFLICT (keycloak_sub) DO UPDATE,
    replacing placeholder UUIDs with real Keycloak-issued values.

    Called by `python db.py seed-users` after setup_keycloak_kcadm.sh has
    written scripts/demo_user_subs.env and seed_users.sh has sourced it.
    """
    updates = []
    for (username, placeholder_sub, role) in _DEMO_USERS:
        env_key = username.upper().replace("-", "_") + "_SUB"
        real_sub = os.environ.get(env_key)
        if real_sub:
            updates.append((real_sub, username, role, placeholder_sub))
        else:
            logger.warning("seed_users_from_env: %s not set — skipping %s", env_key, username)

    if not updates:
        logger.warning("seed_users_from_env: no sub env vars found; nothing updated")
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            for (real_sub, username, role, placeholder_sub) in updates:
                # Insert with real sub; if placeholder already exists, update it
                cur.execute(
                    """
                    INSERT INTO users (keycloak_sub, username, role)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (keycloak_sub) DO UPDATE
                        SET username = EXCLUDED.username,
                            role     = EXCLUDED.role
                    """,
                    (real_sub, username, role),
                )
                # Remove the placeholder row if it's different from real_sub
                cur.execute(
                    "DELETE FROM users WHERE keycloak_sub = %s AND keycloak_sub != %s",
                    (placeholder_sub, real_sub),
                )
        conn.commit()
    print(f"Updated {len(updates)} demo user sub UUIDs.")


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def get_or_create_user(keycloak_sub: str, username: str, role: str) -> dict:
    """Return the users row for the given Keycloak sub, creating it if absent.

    Used by auth_helper for JIT provisioning on first login.
    Returns a plain dict with keys: id, keycloak_sub, username, role, created_at.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE keycloak_sub = %s",
                (keycloak_sub,),
            )
            row = cur.fetchone()
            if row:
                return _row_to_dict(row)

            cur.execute(
                """
                INSERT INTO users (keycloak_sub, username, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (keycloak_sub) DO UPDATE
                    SET username = EXCLUDED.username,
                        role     = EXCLUDED.role
                RETURNING *
                """,
                (keycloak_sub, username, role),
            )
            user = _row_to_dict(cur.fetchone())
        conn.commit()
    return user


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    """Convert a RealDictRow to a plain dict with JSON-serialisable values."""
    if row is None:
        return None
    result = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Booking CRUD helpers
# ---------------------------------------------------------------------------

def create_booking(passenger_name, destination, origin, travel_date,
                   return_date=None, user_id=None):
    """Insert a new booking and return the full booking dict including generated reference."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bookings
                    (passenger_name, destination, origin, travel_date, return_date, status, user_id)
                VALUES (%s, %s, %s, %s, %s, 'confirmed', %s)
                RETURNING id
                """,
                (passenger_name, destination, origin, travel_date, return_date, user_id),
            )
            row_id = cur.fetchone()["id"]
            reference = "VT-" + str(row_id).zfill(5)
            cur.execute(
                """
                UPDATE bookings SET reference = %s WHERE id = %s
                RETURNING *
                """,
                (reference, row_id),
            )
            booking = _row_to_dict(cur.fetchone())
        conn.commit()
    return booking


def get_booking(reference, user_id=None):
    """Fetch a single booking by reference code.

    When user_id is provided, only returns the booking if it belongs to that user.
    Returns dict or None.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    "SELECT * FROM bookings WHERE reference = %s AND user_id = %s",
                    (reference, user_id),
                )
            else:
                cur.execute(
                    "SELECT * FROM bookings WHERE reference = %s",
                    (reference,),
                )
            return _row_to_dict(cur.fetchone())


def list_bookings(passenger_name=None, user_id=None):
    """Return bookings, optionally filtered by passenger name and/or owner user_id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            conditions = []
            params = []
            if passenger_name:
                conditions.append("passenger_name ILIKE %s")
                params.append(f"%{passenger_name}%")
            if user_id is not None:
                conditions.append("user_id = %s")
                params.append(user_id)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            cur.execute(
                f"SELECT * FROM bookings {where} ORDER BY travel_date",  # noqa: S608
                params,
            )
            return [_row_to_dict(row) for row in cur.fetchall()]


def cancel_booking(reference, user_id=None):
    """Set status = 'cancelled' for the given reference.

    When user_id is provided, only cancels the booking if it belongs to that user.
    Returns updated dict or None.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    """
                    UPDATE bookings SET status = 'cancelled'
                    WHERE reference = %s AND user_id = %s
                    RETURNING *
                    """,
                    (reference, user_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE bookings SET status = 'cancelled'
                    WHERE reference = %s
                    RETURNING *
                    """,
                    (reference,),
                )
            booking = _row_to_dict(cur.fetchone())
        conn.commit()
    return booking


# ---------------------------------------------------------------------------
# Payment card CRUD helpers
# ---------------------------------------------------------------------------

# ⚠️  DEMO ONLY — see security warning in init_db() above.

def add_payment_card(cardholder_name, card_number, card_brand, expiry,
                     billing_address, user_id=None):
    """Insert a new payment card and return the full card dict."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_cards
                    (cardholder_name, card_number, card_brand, expiry, billing_address, user_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (cardholder_name, card_number, card_brand, expiry, billing_address, user_id),
            )
            card = _row_to_dict(cur.fetchone())
        conn.commit()
    return card


_CARD_UPDATABLE_FIELDS = {"cardholder_name", "card_number", "card_brand", "expiry", "billing_address"}


def update_payment_card(card_id, user_id=None, **fields):
    """Update any subset of the five editable card fields.

    When user_id is provided, only updates the card if it belongs to that user.
    Returns updated dict or None.
    """
    allowed = {k: v for k, v in fields.items() if k in _CARD_UPDATABLE_FIELDS}
    if not allowed:
        return get_payment_card(card_id, user_id=user_id)

    set_clause = ", ".join(f"{col} = %s" for col in allowed)
    values = list(allowed.values()) + [card_id]

    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                values.append(user_id)
                cur.execute(
                    f"UPDATE payment_cards SET {set_clause} WHERE id = %s AND user_id = %s RETURNING *",  # noqa: S608
                    values,
                )
            else:
                cur.execute(
                    f"UPDATE payment_cards SET {set_clause} WHERE id = %s RETURNING *",  # noqa: S608
                    values,
                )
            card = _row_to_dict(cur.fetchone())
        conn.commit()
    return card


def remove_payment_card(card_id, user_id=None):
    """Delete a payment card.

    When user_id is provided, only deletes the card if it belongs to that user.
    Returns True if a row was deleted, False if not found.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    "DELETE FROM payment_cards WHERE id = %s AND user_id = %s",
                    (card_id, user_id),
                )
            else:
                cur.execute(
                    "DELETE FROM payment_cards WHERE id = %s",
                    (card_id,),
                )
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def list_payment_cards(user_id=None):
    """Return payment cards, optionally filtered by owner user_id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    "SELECT * FROM payment_cards WHERE user_id = %s ORDER BY id",
                    (user_id,),
                )
            else:
                cur.execute("SELECT * FROM payment_cards ORDER BY id")
            return [_row_to_dict(row) for row in cur.fetchall()]


def get_payment_card(card_id, name=None, user_id=None):
    """Fetch a single payment card by id.

    Optionally filtered by cardholder name and/or owner user_id.
    Returns dict or None.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            conditions = ["id = %s"]
            params = [card_id]
            if name is not None:
                conditions.append("cardholder_name = %s")
                params.append(name)
            if user_id is not None:
                conditions.append("user_id = %s")
                params.append(user_id)
            where = " AND ".join(conditions)
            cur.execute(
                f"SELECT * FROM payment_cards WHERE {where}",  # noqa: S608
                params,
            )
            return _row_to_dict(cur.fetchone())


# ---------------------------------------------------------------------------
# Entry point — run directly to create tables and seed data
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    subcommand = sys.argv[1] if len(sys.argv) > 1 else None

    if subcommand == "seed-users":
        print("Patching demo user Keycloak sub UUIDs from environment…")
        seed_users_from_env()
        print("Done.")
    else:
        print("Initialising database schema…")
        init_db()
        print("Tables ready.")
        print("Seeding data…")
        seed_db()
        print("Done.")
