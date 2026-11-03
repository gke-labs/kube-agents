"""The shared token-shape list refuses and redacts every shape its two callers rely on.

Run: python3 -m unittest test_credential_shapes   (from agents/platform/scripts)
"""

from __future__ import annotations

import unittest
from unittest import mock

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

    def test_shape_hints_cover_every_registered_shape_and_variant(self) -> None:
        self.assertEqual(len(cs.CREDENTIAL_SHAPES), len(cs._SHAPE_HINTS))
        self.assertEqual(len(cs.REFUSAL_SHAPES), len(cs._SHAPE_HINTS))
        variants_by_index: tuple[tuple[str, ...], ...] = (
            (
                "-----BEGIN PRIVATE KEY-----",
                "-----BEGIN RSA PRIVATE KEY-----",
                "-----BEGIN EC PRIVATE KEY-----",
                "-----BEGIN OPENSSH PRIVATE KEY-----",
            ),
            tuple(f"{prefix}{'a' * 36}" for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"))
            + ("truncatedghp_" + "a" * 36,),
            (
                "github_pat_11ABCDEFG0123456789abcdefGHIJ",
                "gluedgithub_pat_11ABCDEFG0123456789abcdefGHIJ",
            ),
            ("AIza" + "A" * 35,),
            ("ya29." + "a0AfB_" + "x" * 40,),
            ("AKIA" + "A" * 16, "ASIA" + "2" * 16),
            ("sk-" + "a" * 24,),
            tuple(
                f"sk-{kind}-api03-" + "Ab1" * 12
                for kind in ("ant", "proj", "svcacct", "admin")
            ),
            tuple(
                f"xox{code}-1234567890-abcdefghijkl"
                for code in ("a", "b", "p", "r", "s")
            ),
            (
                "Bearer abc123def456ghi789jkl0",
                "bearer abc123def456ghi789jkl0",
                "BEARER abc123def456ghi789jkl0",
                "BeArEr abc123def456ghi789jkl0",
                "Bearer\tabc123def456ghi789jkl0",
                "Basic dXNlcjpwYXNzd29yZDEyMzQ=",
                "basic dXNlcjpwYXNzd29yZDEyMzQ=",
                "BASIC dXNlcjpwYXNzd29yZDEyMzQ=",
                "bAsIc dXNlcjpwYXNzd29yZDEyMzQ=",
                "Basic\ndXNlcjpwYXNzd29yZDEyMzQ=",
            ),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",),
            (
                "https://user:s3cretpass@example.com/repo.git",
                "https://:s3cretpass@example.com/repo.git",
                "alice@example.com https://user:s3cretpass@example.com/repo.git",
                "postgres+ssl://admin:p4ssw0rd@db.internal:5432/app",
            ),
        )
        self.assertEqual(len(cs.CREDENTIAL_SHAPES), len(variants_by_index))
        for idx, ((name, cred_pattern), (refusal_name, refusal_pattern), hints, samples) in enumerate(
            zip(cs.CREDENTIAL_SHAPES, cs.REFUSAL_SHAPES, cs._SHAPE_HINTS, variants_by_index, strict=True)
        ):
            self.assertEqual(name, refusal_name)
            for sample in samples:
                with self.subTest(index=idx, name=name, sample=sample):
                    self.assertTrue(cs._may_contain_shape(sample, hints))
                    self.assertIsNotNone(cred_pattern.search(sample))
                    self.assertIsNotNone(refusal_pattern.search(sample))
                    self.assertEqual(name, cs.find_credential_shape(f"prefix {sample} suffix"))
                    redacted = cs.redact(f"prefix {sample} suffix")
                    self.assertIn(cs.REDACTED, redacted)
                    self.assertEqual(cred_pattern.sub(cs.REDACTED, f"prefix {sample} suffix"), redacted)

    def test_mixed_case_bearer_and_basic_headers_respect_redact_and_refuse_rules(self) -> None:
        for header in ("BeArEr", "bAsIc", "BEARER", "BASIC"):
            with self.subTest(header=header):
                letters_only = f"Authorization: {header} abcdefghijklmnopqrstuvwxyz"
                self.assertEqual(f"Authorization: {cs.REDACTED}", cs.redact(letters_only))
                self.assertIsNone(cs.find_credential_shape(letters_only))
                with_digits = f"Authorization: {header} abcdef1234567890ghij"
                self.assertEqual(f"Authorization: {cs.REDACTED}", cs.redact(with_digits))
                self.assertEqual(
                    "an authorization header value",
                    cs.find_credential_shape(with_digits),
                )

    def test_url_password_hint_requires_both_scheme_and_at_sign(self) -> None:
        url_hints = cs._SHAPE_HINTS[-1]
        self.assertIsInstance(url_hints, cs._AllOf)
        self.assertEqual(("://", "@"), url_hints)
        self.assertFalse(cs._may_contain_shape("https://kubernetes.default.svc/api/v1/pods", url_hints))
        self.assertFalse(cs._may_contain_shape("serviceAccount: bot@project.iam.gserviceaccount.com", url_hints))
        self.assertTrue(cs._may_contain_shape("https://user@example.com/repo.git", url_hints))
        self.assertIsNone(cs.find_credential_shape("https://user@example.com/repo.git"))
        self.assertEqual("https://user@example.com/repo.git", cs.redact("https://user@example.com/repo.git"))

    def test_empty_and_clean_multi_mib_payloads_short_circuit_without_regex_calls(self) -> None:
        self.assertIsNone(cs.find_credential_shape(""))
        self.assertEqual("", cs.redact(""))

        pod_item = (
            '{"metadata":{"name":"checkout-gateway-0","namespace":"prod",'
            '"selfLink":"https://kubernetes.default.svc/api/v1/namespaces/prod/pods/checkout-gateway-0"},'
            '"status":{"phase":"Running","podIP":"10.24.1.19"}}\n'
        )
        large_clean_json = '{"apiVersion":"v1","kind":"PodList","items":[' + (pod_item * 12000) + "]}"
        self.assertGreater(len(large_clean_json), 2 * 1024 * 1024)

        class _ForbiddenPattern:
            def search(self, _text: str) -> None:
                raise AssertionError("pattern.search should not be called when hint is absent")

            def sub(self, _repl: str, _text: str) -> str:
                raise AssertionError("pattern.sub should not be called when hint is absent")

        forbidden = _ForbiddenPattern()
        fake_shapes = tuple((name, forbidden) for name, _ in cs.CREDENTIAL_SHAPES)
        with (
            mock.patch.object(cs, "CREDENTIAL_SHAPES", fake_shapes),
            mock.patch.object(cs, "REFUSAL_SHAPES", fake_shapes),
        ):
            self.assertIsNone(cs.find_credential_shape(large_clean_json))
            self.assertIs(large_clean_json, cs.redact(large_clean_json))

    def test_benign_k8s_sk_words_skip_prefixed_provider_key_and_slack_regexes(self) -> None:
        text = "node condition: disk-pressure=false, label: task-runner=active, mask-bits=0xff"
        self.assertTrue(cs._may_contain_shape(text, cs._SHAPE_HINTS[6]))
        self.assertFalse(cs._may_contain_shape(text, cs._SHAPE_HINTS[7]))
        self.assertFalse(cs._may_contain_shape(text, cs._SHAPE_HINTS[8]))
        self.assertIsNone(cs.find_credential_shape(text))
        self.assertEqual(text, cs.redact(text))


if __name__ == "__main__":
    unittest.main()
