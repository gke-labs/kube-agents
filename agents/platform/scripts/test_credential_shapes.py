"""The shared token-shape list refuses and redacts every shape its two callers rely on.

Run: python3 -m unittest test_credential_shapes   (from agents/platform/scripts)
"""

from __future__ import annotations

import unittest

import credential_shapes as cs

TOKENS = {
    "a GitHub token": "ghp_" + "a" * 36,
    "a fine-grained GitHub token": "github_pat_11ABCDEFG0123456789abcdefGHIJ",
    "a Google API key": "AIza" + "A" * 35,
    "a Google OAuth token": "ya29.a0AfB_" + "x" * 40,
    "an AWS access key id": "AKIA" + "A" * 16,
    "a provider API key": "sk-" + "a" * 24,
    "a Slack token": "xoxb-1234567890-abcdefghijkl",
    "an authorization header value": "Bearer abc123def456ghi789jkl0",
    "a signed JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "a password in a URL": "https://user:s3cretpass@example.com/repo.git",
    "private key material": "-----BEGIN RSA PRIVATE KEY-----",
}
PREFIXED_PROVIDER_KEY = "sk-ant-api03-" + "Ab1" * 12
BENIGN = (
    "auth failed with 401 and the relay retried",
    "I followed the basic troubleshooting steps and the agent hung",
    "Bearer authentication against the relay returns 401",
    "basic configuration of the installation steps",
    "Basic auth is on for the dashboard",
    "the bearer of bad news",
    "sk-short",
    "ghp_tooShort",
    "https://example.com/path:8080/x",
    "AKIAI is not a key",
)


class CredentialShapesTest(unittest.TestCase):
    def test_every_shape_is_found_and_named(self) -> None:
        for name, token in TOKENS.items():
            with self.subTest(name=name):
                self.assertEqual(name, cs.find_credential_shape(f"log line: {token} then 401"))

    def test_the_prefixed_provider_key_is_found(self) -> None:
        self.assertEqual("a provider API key", cs.find_credential_shape(f"auth failed for {PREFIXED_PROVIDER_KEY}"))

    def test_benign_text_is_not_a_shape(self) -> None:
        for text in BENIGN:
            with self.subTest(text=text):
                self.assertIsNone(cs.find_credential_shape(text))

    def test_the_redactor_still_masks_a_letters_only_header_value(self) -> None:
        # Over-masking a word in a log line is cheap; refusing a report for it
        # is not, so the two forms differ on purpose.
        self.assertIn(cs.REDACTED, cs.redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz"))
        self.assertIsNone(cs.find_credential_shape("Bearer abcdefghijklmnopqrstuvwxyz"))

    def test_redact_blanks_every_shape_and_keeps_the_rest(self) -> None:
        line = "relay said " + " and ".join(TOKENS.values()) + " at 12:00"
        out = cs.redact(line)
        for token in TOKENS.values():
            self.assertNotIn(token, out)
        self.assertTrue(out.startswith("relay said ") and out.endswith(" at 12:00"))
        self.assertIn(cs.REDACTED, out)


if __name__ == "__main__":
    unittest.main()
