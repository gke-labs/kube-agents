#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Add the directory containing session_manager.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from session_manager import SessionManager


class TestDelegationHeaderSecurity(unittest.TestCase):
    """Verify cryptographic signing and verification of X-Hermes-* headers
    for inter-agent authentication and replay/tamper defense."""

    def setUp(self):
        self.sm = SessionManager()
        self.context = {
            "session_id": "sess-abc-123",
            "user_id": "platform:user-456",
            "sender_id": "user-456",
            "metadata": {"user_email_hash": "9f86d081884c7d65"},
        }
        self.api_key = "primary-secret-key"
        self.secondary_key = "secondary-secret-key"

    def test_signed_delegation_headers(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        self.assertEqual(headers["X-Hermes-Session-Id"], "sess-abc-123")
        self.assertEqual(headers["X-Hermes-User-Id"], "platform:user-456")
        self.assertIn("X-Hermes-Signature", headers)
        self.assertIn("X-Hermes-Timestamp", headers)
        self.assertTrue(headers["X-Hermes-Signature"].startswith("sha256="))

    def test_delegation_headers_carry_no_plaintext_address(self):
        """A delegated turn must not hand a downstream agent an e-mail address."""
        context = dict(self.context)
        context["metadata"] = {
            "user_email": "user@example.com",
            "user_email_hash": "9f86d081884c7d65",
        }
        headers = self.sm.signed_delegation_headers(context, self.api_key)
        self.assertNotIn("X-Hermes-User-Email", headers)
        self.assertEqual(headers["X-Hermes-User-Email-Hash"], "9f86d081884c7d65")
        self.assertNotIn("user@example.com", "".join(headers.values()))

    def test_current_context_ignores_plaintext_email(self):
        """A row written before the migration must not resurrect the address."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "session_kv.db"
            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute(
                    "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY,"
                    " metadata TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                    ("legacy-1", json.dumps({"platform": "google_chat",
                                             "user_email": "user@example.com"})),
                )
            conn.close()

            sm = SessionManager(db_path=db_path)
            for key in ("HERMES_USER_ID", "HERMES_SENDER_ID"):
                os.environ.pop(key, None)
            context = sm.current_context("legacy-1")

        # The raw row is returned verbatim — the server purges it on start, not
        # this reader. What matters is that nothing derived from it leaks.
        self.assertEqual(context["sender_id"], "")
        self.assertEqual(context["user_id"], "")

    def test_current_context_ignores_a_plaintext_user_id(self):
        """On Google Chat the pre-migration `user_id` is the address itself."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "session_kv.db"
            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute(
                    "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY,"
                    " metadata TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                    ("legacy-2", json.dumps({"platform": "google_chat",
                                             "user_id": "user@example.com"})),
                )
            conn.close()

            sm = SessionManager(db_path=db_path)
            for key in ("HERMES_USER_ID", "HERMES_SENDER_ID"):
                os.environ.pop(key, None)
            context = sm.current_context("legacy-2")

        self.assertEqual(context["sender_id"], "")
        self.assertEqual(context["user_id"], "")

    def test_current_context_keeps_an_opaque_user_id(self):
        """A Slack member id carries no `@` and is already a pseudonym."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "session_kv.db"
            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute(
                    "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY,"
                    " metadata TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                    ("slack-1", json.dumps({"platform": "slack",
                                            "user_id": "U012ABCDEF"})),
                )
            conn.close()

            sm = SessionManager(db_path=db_path)
            for key in ("HERMES_USER_ID", "HERMES_SENDER_ID"):
                os.environ.pop(key, None)
            context = sm.current_context("slack-1")

        self.assertEqual(context["sender_id"], "U012ABCDEF")
        self.assertEqual(context["user_id"], "slack:U012ABCDEF")
        self.assertNotIn("user_email", SessionManager.SESSION_METADATA_KEYS)
        self.assertNotIn("user_email", sm.filter_session_metadata(context["metadata"]))
        self.assertNotIn(
            "X-Hermes-User-Email", sm._base_delegation_headers(context)
        )

    def test_verify_delegation_headers_success(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        valid = self.sm.verify_delegation_headers(headers, [self.api_key])
        self.assertTrue(valid)

    def test_verify_delegation_headers_multiple_keys(self):
        headers = self.sm.signed_delegation_headers(self.context, self.secondary_key)
        valid = self.sm.verify_delegation_headers(
            headers, [self.api_key, self.secondary_key]
        )
        self.assertTrue(valid)

    def test_verify_delegation_headers_tampering(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        headers["X-Hermes-User-Id"] = "platform:admin-attacker"
        valid = self.sm.verify_delegation_headers(headers, [self.api_key])
        self.assertFalse(valid)

    def test_verify_delegation_headers_replay_expired(self):
        headers = self.sm.signed_delegation_headers(self.context, self.api_key)
        headers["X-Hermes-Timestamp"] = str(int(time.time()) - 400)
        self.assertFalse(
            self.sm.verify_delegation_headers(headers, [self.api_key])
        )

    def test_canonicalize_headers_forgery_prevention(self):
        """Regression test for canonicalization forgery (Blocking PR comment)."""
        forged_context = {
            "session_id": "sess-abc-123",
            "user_id": "platform:user-456",
            "sender_id": "user-456",
            "metadata": {
                "user_email_hash": "9f86d081\nx-hermes-user-id:platform:admin"
            },
        }
        headers = self.sm.signed_delegation_headers(forged_context, self.api_key)
        # Attempt to forge the header set by replacing user_id with the injected value
        forged_headers = dict(headers)
        forged_headers["X-Hermes-User-Id"] = "platform:admin"
        valid = self.sm.verify_delegation_headers(forged_headers, [self.api_key])
        self.assertFalse(valid)

    def test_signed_delegation_headers_with_body_digest_and_target(self):
        body_digest = "abcdef1234567890"
        target = "platform"
        headers = self.sm.signed_delegation_headers(
            self.context, self.api_key, body_digest=body_digest, target=target
        )
        valid = self.sm.verify_delegation_headers(
            headers, [self.api_key], body_digest=body_digest, target=target
        )
        self.assertTrue(valid)

        # Replaying onto a different target or body digest must fail verification
        self.assertFalse(
            self.sm.verify_delegation_headers(
                headers, [self.api_key], body_digest="different-digest", target=target
            )
        )
        self.assertFalse(
            self.sm.verify_delegation_headers(
                headers, [self.api_key], body_digest=body_digest, target="other-agent"
            )
        )

    def test_signing_key_derivation_and_isolation(self):
        derived = self.sm._resolve_signing_key("my-api-key")
        self.assertNotEqual(derived, "my-api-key")
        self.assertEqual(len(derived), 64)

        try:
            os.environ["HERMES_DELEGATION_SIGNING_KEY"] = "custom-signing-secret"
            self.assertEqual(
                self.sm._resolve_signing_key("my-api-key"), "custom-signing-secret"
            )
        finally:
            os.environ.pop("HERMES_DELEGATION_SIGNING_KEY", None)


if __name__ == "__main__":
    unittest.main()
