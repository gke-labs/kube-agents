#!/usr/bin/env python3
import os
import sys
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
            "metadata": {"user_email": "user@example.com"},
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
            "metadata": {"user_email": "e@x\nx-hermes-user-id:platform:admin"},
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
