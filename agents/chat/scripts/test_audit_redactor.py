#!/usr/bin/env python3
"""Unit tests for AuditRedactor."""

import os
import sys
import unittest
from pathlib import Path

# Ensure defaults package is importable matching container layout
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "defaults"))

from plugins.common.redactor import AuditRedactor


class TestAuditRedactor(unittest.TestCase):
    def test_redact_text_email(self):
        text = "Contact me at alice@example.com for info."
        redacted = AuditRedactor.redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", redacted)
        self.assertNotIn("alice@example.com", redacted)

    def test_redact_text_gcp_api_key(self):
        text = "Key is AIzaSyD123456789012345678901234567890123 here."
        redacted = AuditRedactor.redact_text(text)
        self.assertIn("[REDACTED_SECRET]", redacted)
        self.assertNotIn("AIzaSyD", redacted)

    def test_redact_text_bearer_token(self):
        text = "Authorization: Bearer my_secret_token_value_123456789"
        redacted = AuditRedactor.redact_text(text)
        self.assertIn("Bearer [REDACTED_SECRET]", redacted)
        self.assertNotIn("my_secret_token_value_123456789", redacted)

    def test_redact_text_bearer_token_prose_ignored(self):
        text = "The Bearer of good news arrived today."
        redacted = AuditRedactor.redact_text(text)
        self.assertEqual(redacted, text)

    def test_redact_text_secret_key_val(self):
        text = '{"api_key": "super-secret"}'
        redacted = AuditRedactor.redact_text(text)
        self.assertIn("[REDACTED_SECRET]", redacted)
        self.assertNotIn("super-secret", redacted)

    def test_redact_text_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIICXAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        redacted = AuditRedactor.redact_text(text)
        self.assertEqual(redacted, "[REDACTED_PRIVATE_KEY]")

    def test_redact_text_openai_token(self):
        text = "Key: sk-123456789012345678901234567890123456789012345678"
        redacted = AuditRedactor.redact_text(text)
        self.assertIn("[REDACTED_SECRET]", redacted)
        self.assertNotIn("sk-1234567890", redacted)

    def test_redact_dict(self):
        data = {
            "api_key": "12345",
            "user_email": "test@example.com",
            "public_info": "hello world",
            "nested": {"password": "pwd"},
        }
        redacted = AuditRedactor.redact(data)
        self.assertEqual(redacted["api_key"], "[REDACTED_SECRET]")
        self.assertEqual(redacted["user_email"], "[REDACTED_EMAIL]")
        self.assertEqual(redacted["public_info"], "hello world")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED_SECRET]")

    def test_redact_false_positives(self):
        data = {
            "author": "mplakhtiy",
            "tokenizer": "gpt-4",
            "credentialsFile": "/etc/creds.json",
        }
        redacted = AuditRedactor.redact(data)
        self.assertEqual(redacted["author"], "mplakhtiy")
        self.assertEqual(redacted["tokenizer"], "gpt-4")
        self.assertEqual(redacted["credentialsFile"], "/etc/creds.json")

    def test_redact_bytes(self):
        data = b"secret@example.com"
        redacted = AuditRedactor.redact(data)
        self.assertIsInstance(redacted, bytes)
        self.assertIn(b"[REDACTED_EMAIL]", redacted)

    def test_hmac_hash(self):
        hashed = AuditRedactor.hmac_hash("test@example.com", salt=b"my-salt")
        self.assertEqual(len(hashed), 64)
        hashed2 = AuditRedactor.hmac_hash("test@example.com", salt=b"my-salt")
        self.assertEqual(hashed, hashed2)

    def test_hmac_hash_fail_closed_without_salt(self):
        old_val = os.environ.pop("SESSION_KV_SALT", None)
        try:
            with self.assertRaises(ValueError):
                AuditRedactor.hmac_hash("test@example.com", salt=None)
            os.environ["SESSION_KV_SALT"] = "test-salt"
            res = AuditRedactor.hmac_hash("test@example.com", salt=None)
            self.assertEqual(len(res), 64)
        finally:
            if old_val is None:
                os.environ.pop("SESSION_KV_SALT", None)
            else:
                os.environ["SESSION_KV_SALT"] = old_val


if __name__ == "__main__":
    unittest.main()

