"""Unit tests for auth_helper provisioning/lookup behaviour.

Covers:
  - provision=False with an existing user → identity returned
  - provision=False with an unknown sub → None returned (→ 401 from require_role)
  - provision=True with an unknown sub → db.get_or_create_user called
  - db.get_or_create_user race-recovery path (re-SELECT after lost INSERT)

Run from the project root:
    uv run python -m unittest discover -s tests -t .
"""
import time
import unittest
from unittest import mock

import auth_helper
import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(sub="user-sub-123", username="alice", roles=None):
    """Return a minimal decoded JWT payload dict."""
    roles = roles or ["user"]
    return {
        "sub": sub,
        "preferred_username": username,
        "realm_access": {"roles": roles},
        "exp": int(time.time()) + 3600,
        "iss": auth_helper._ISSUER,
    }


def _patch_jwt_decode(payload):
    """Return a mock that patches jwt.decode to return *payload* and skips JWKS fetch."""
    return mock.patch.multiple(
        "auth_helper",
        _get_jwks=mock.DEFAULT,
    )


class ValidateAndIdentifyLookupTests(unittest.TestCase):
    """provision=False (default) — read-only lookup path."""

    def _run(self, sub="user-sub-123", username="alice", db_user=None, roles=None):
        """
        Invoke validate_and_identify with JWT mocked out and db.get_user_by_sub
        returning *db_user*.
        """
        payload = _make_payload(sub=sub, username=username, roles=roles)

        jwks_mock = mock.MagicMock()
        jwk_set_mock = mock.MagicMock()
        signing_key_mock = mock.MagicMock()
        jwk_set_mock.__getitem__ = mock.Mock(return_value=signing_key_mock)

        with mock.patch("auth_helper._get_jwks", return_value={}), \
             mock.patch("jwt.PyJWKSet.from_dict", return_value=jwk_set_mock), \
             mock.patch("jwt.get_unverified_header", return_value={"kid": "k1"}), \
             mock.patch("jwt.decode", return_value=payload), \
             mock.patch("db.get_user_by_sub", return_value=db_user) as mock_lookup, \
             mock.patch("db.get_or_create_user") as mock_provision:
            result = auth_helper.validate_and_identify("fake-token", provision=False)

        return result, mock_lookup, mock_provision

    def test_existing_user_returns_identity(self):
        """provision=False with an existing user returns the identity dict."""
        existing = {"id": 42, "keycloak_sub": "user-sub-123", "username": "alice", "role": "user", "created_at": "2024-01-01T00:00:00"}
        result, mock_lookup, mock_provision = self._run(db_user=existing)

        self.assertIsNotNone(result)
        self.assertEqual(result["user_id"], 42)
        self.assertEqual(result["sub"], "user-sub-123")
        self.assertEqual(result["username"], "alice")
        self.assertEqual(result["role"], "user")

        mock_lookup.assert_called_once_with("user-sub-123")
        mock_provision.assert_not_called()

    def test_unknown_sub_returns_none(self):
        """provision=False with an unknown sub returns None (→ 401 from require_role)."""
        result, mock_lookup, mock_provision = self._run(db_user=None)

        self.assertIsNone(result)
        mock_lookup.assert_called_once_with("user-sub-123")
        mock_provision.assert_not_called()

    def test_get_or_create_user_not_called(self):
        """The provisioning path is never taken when provision=False."""
        _existing = {"id": 1, "keycloak_sub": "s", "username": "u", "role": "user", "created_at": ""}
        _, _, mock_provision = self._run(db_user=_existing)
        mock_provision.assert_not_called()


class ValidateAndIdentifyProvisionTests(unittest.TestCase):
    """provision=True — JIT provisioning path (orchestrator only)."""

    def _run(self, sub="new-sub-999", username="bob", roles=None):
        payload = _make_payload(sub=sub, username=username, roles=roles)

        jwk_set_mock = mock.MagicMock()
        signing_key_mock = mock.MagicMock()
        jwk_set_mock.__getitem__ = mock.Mock(return_value=signing_key_mock)

        provisioned = {"id": 7, "keycloak_sub": sub, "username": username, "role": "user", "created_at": ""}

        with mock.patch("auth_helper._get_jwks", return_value={}), \
             mock.patch("jwt.PyJWKSet.from_dict", return_value=jwk_set_mock), \
             mock.patch("jwt.get_unverified_header", return_value={"kid": "k1"}), \
             mock.patch("jwt.decode", return_value=payload), \
             mock.patch("db.get_or_create_user", return_value=provisioned) as mock_provision, \
             mock.patch("db.get_user_by_sub") as mock_lookup:
            result = auth_helper.validate_and_identify("fake-token", provision=True)

        return result, mock_provision, mock_lookup

    def test_unknown_sub_calls_get_or_create_user(self):
        """provision=True with an unknown sub calls db.get_or_create_user."""
        result, mock_provision, mock_lookup = self._run(sub="new-sub-999", username="bob")

        self.assertIsNotNone(result)
        self.assertEqual(result["user_id"], 7)
        mock_provision.assert_called_once_with("new-sub-999", "bob", "user")
        mock_lookup.assert_not_called()

    def test_provision_returns_identity(self):
        """provision=True returns a fully populated identity dict."""
        result, _, _ = self._run()
        self.assertIn("user_id", result)
        self.assertIn("sub", result)
        self.assertIn("username", result)
        self.assertIn("role", result)


class GetOrCreateUserRaceTests(unittest.TestCase):
    """db.get_or_create_user re-SELECT path when INSERT loses the race."""

    def _make_cursor(self, initial_select=None, insert_row=None, reselect_row=None):
        """Return a mock cursor that drives the three-step SELECT/INSERT/SELECT."""
        cursor = mock.MagicMock()
        # fetchone() call sequence: initial SELECT, INSERT RETURNING, re-SELECT
        cursor.fetchone.side_effect = [
            initial_select,   # fast-path SELECT
            insert_row,       # INSERT … RETURNING
            reselect_row,     # re-SELECT after lost race
        ]
        return cursor

    def _make_conn(self, cursor):
        conn = mock.MagicMock()
        conn.__enter__ = mock.Mock(return_value=conn)
        conn.__exit__ = mock.Mock(return_value=False)
        conn.cursor.return_value.__enter__ = mock.Mock(return_value=cursor)
        conn.cursor.return_value.__exit__ = mock.Mock(return_value=False)
        return conn

    def test_existing_row_returned_on_fast_path(self):
        """If the initial SELECT finds a row, no INSERT is issued."""
        existing = {"id": 1, "keycloak_sub": "sub", "username": "u", "role": "user", "created_at": None}
        cursor = self._make_cursor(initial_select=existing)
        conn = self._make_conn(cursor)

        with mock.patch("db.get_connection", return_value=conn):
            result = db.get_or_create_user("sub", "u", "user")

        self.assertEqual(result["id"], 1)
        # Only the initial SELECT was executed; no INSERT
        self.assertEqual(cursor.execute.call_count, 1)

    def test_insert_winner_returns_inserted_row(self):
        """INSERT wins the race → returns the RETURNING row without a re-SELECT."""
        inserted = {"id": 2, "keycloak_sub": "sub", "username": "u", "role": "user", "created_at": None}
        cursor = self._make_cursor(initial_select=None, insert_row=inserted)
        conn = self._make_conn(cursor)

        with mock.patch("db.get_connection", return_value=conn):
            result = db.get_or_create_user("sub", "u", "user")

        self.assertEqual(result["id"], 2)

    def test_insert_loser_falls_back_to_reselect(self):
        """INSERT loses the race (DO NOTHING returns nothing) → re-SELECT finds the row."""
        existing = {"id": 3, "keycloak_sub": "sub", "username": "u", "role": "user", "created_at": None}
        cursor = self._make_cursor(initial_select=None, insert_row=None, reselect_row=existing)
        conn = self._make_conn(cursor)

        with mock.patch("db.get_connection", return_value=conn):
            result = db.get_or_create_user("sub", "u", "user")

        self.assertEqual(result["id"], 3)
        # Three executes: fast-path SELECT, INSERT, re-SELECT
        self.assertEqual(cursor.execute.call_count, 3)

    def test_insert_loser_reselect_missing_raises(self):
        """If both INSERT and re-SELECT return nothing, RuntimeError is raised."""
        cursor = self._make_cursor(initial_select=None, insert_row=None, reselect_row=None)
        conn = self._make_conn(cursor)

        with mock.patch("db.get_connection", return_value=conn):
            with self.assertRaises(RuntimeError):
                db.get_or_create_user("sub", "u", "user")


if __name__ == "__main__":
    unittest.main()
