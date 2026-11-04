"""
db.py — PostgreSQL data access layer for the Voyager Travel booking agent.

All database access is centralised here. Nothing else in the app should
import psycopg2 directly.

Environment variables
---------------------
  DATABASE_URL   Full PostgreSQL DSN, e.g. postgresql://user:pass@host:5432/dbname

Every service shares this one credential — see README for why that is the problem
Phase 3 solves.
"""

import logging
import os
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection():
    """Return a new psycopg2 connection using DATABASE_URL.

    Raises RuntimeError if DATABASE_URL is not set.
    """
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
    """Create both tables if they do not already exist. Safe to call on every startup."""
    with get_connection() as conn:
        with conn.cursor() as cur:
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
        conn.commit()


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def seed_db():
    """Insert fake data only when the tables are empty (idempotent)."""
    with get_connection() as conn:
        with conn.cursor() as cur:

            # Seed bookings
            cur.execute("SELECT COUNT(*) FROM bookings")
            if cur.fetchone()["count"] == 0:
                bookings = [
                    ("Alice Johnson",   "Paris",       "New York",    "2025-06-10", "2025-06-24", "confirmed"),
                    ("Bob Martinez",    "Tokyo",       "Los Angeles", "2025-07-15", "2025-07-29", "confirmed"),
                    ("Carol Williams",  "London",      "Chicago",     "2025-08-01", None,          "pending"),
                    ("David Lee",       "Sydney",      "San Francisco","2025-09-05", "2025-09-19", "confirmed"),
                    ("Eva Rossi",       "Rome",        "Boston",      "2025-05-20", "2025-05-30", "cancelled"),
                    ("Frank Chen",      "Dubai",       "Toronto",     "2025-10-12", "2025-10-22", "pending"),
                    ("Grace Kim",       "Barcelona",   "Miami",       "2025-11-03", "2025-11-17", "confirmed"),
                    ("Henry Okafor",    "Cape Town",   "London",      "2025-12-15", None,          "pending"),
                ]
                for (pname, dest, orig, tdate, rdate, status) in bookings:
                    cur.execute(
                        """
                        INSERT INTO bookings
                            (passenger_name, destination, origin, travel_date, return_date, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (pname, dest, orig, tdate, rdate, status),
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
                    ("Alice Johnson",  "4111 1111 1111 1111", "Visa",       "09/2027", "123 Main St, New York, NY 10001"),
                    ("Bob Martinez",   "5500 0000 0000 0004", "Mastercard", "03/2026", "456 Oak Ave, Los Angeles, CA 90001"),
                    ("Carol Williams", "3714 496353 98431",   "Amex",       "11/2028", "789 Pine Rd, Chicago, IL 60601"),
                    ("David Lee",      "6011 1111 1111 1117", "Discover",   "06/2027", "321 Elm St, San Francisco, CA 94101"),
                ]
                for (cname, cnum, brand, expiry, addr) in cards:
                    cur.execute(
                        """
                        INSERT INTO payment_cards
                            (cardholder_name, card_number, card_brand, expiry, billing_address)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (cname, cnum, brand, expiry, addr),
                    )

        conn.commit()


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

def create_booking(passenger_name, destination, origin, travel_date, return_date=None):
    """Insert a new booking and return the full booking dict including generated reference."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bookings
                    (passenger_name, destination, origin, travel_date, return_date, status)
                VALUES (%s, %s, %s, %s, %s, 'confirmed')
                RETURNING id
                """,
                (passenger_name, destination, origin, travel_date, return_date),
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


def get_booking(reference):
    """Fetch a single booking by reference code. Returns dict or None."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM bookings WHERE reference = %s",
                (reference,),
            )
            return _row_to_dict(cur.fetchone())


def list_bookings(passenger_name=None):
    """Return all bookings, optionally filtered by passenger name (case-insensitive)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if passenger_name:
                cur.execute(
                    "SELECT * FROM bookings WHERE passenger_name ILIKE %s ORDER BY travel_date",
                    (f"%{passenger_name}%",),
                )
            else:
                cur.execute("SELECT * FROM bookings ORDER BY travel_date")
            return [_row_to_dict(row) for row in cur.fetchall()]


def cancel_booking(reference):
    """Set status = 'cancelled' for the given reference. Returns updated dict or None."""
    with get_connection() as conn:
        with conn.cursor() as cur:
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

def add_payment_card(cardholder_name, card_number, card_brand, expiry, billing_address):
    """Insert a new payment card and return the full card dict."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_cards
                    (cardholder_name, card_number, card_brand, expiry, billing_address)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (cardholder_name, card_number, card_brand, expiry, billing_address),
            )
            card = _row_to_dict(cur.fetchone())
        conn.commit()
    return card


_CARD_UPDATABLE_FIELDS = {"cardholder_name", "card_number", "card_brand", "expiry", "billing_address"}


def update_payment_card(card_id, **fields):
    """Update any subset of the five editable card fields. Returns updated dict or None."""
    allowed = {k: v for k, v in fields.items() if k in _CARD_UPDATABLE_FIELDS}
    if not allowed:
        return get_payment_card(card_id)

    set_clause = ", ".join(f"{col} = %s" for col in allowed)
    values = list(allowed.values()) + [card_id]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE payment_cards SET {set_clause} WHERE id = %s RETURNING *",  # noqa: S608 — column names are allowlisted above
                values,
            )
            card = _row_to_dict(cur.fetchone())
        conn.commit()
    return card


def remove_payment_card(card_id):
    """Delete a payment card. Returns True if a row was deleted, False if not found."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM payment_cards WHERE id = %s",
                (card_id,),
            )
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def list_payment_cards():
    """Return all stored payment cards."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_cards ORDER BY id")
            return [_row_to_dict(row) for row in cur.fetchall()]


def get_payment_card(card_id, name=None):
    """Fetch a single payment card by id, optionally filtered by cardholder name. Returns dict or None."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if name is not None:
                cur.execute(
                    "SELECT * FROM payment_cards WHERE id = %s AND cardholder_name = %s",
                    (card_id, name),
                )
            else:
                cur.execute("SELECT * FROM payment_cards WHERE id = %s", (card_id,))
            return _row_to_dict(cur.fetchone())


# ---------------------------------------------------------------------------
# Entry point — run directly to create tables and seed data
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Initialising database schema…")
    init_db()
    print("Tables ready.")
    print("Seeding data…")
    seed_db()
    print("Done.")
