"""Tests for the shared audit redactor.

Two properties matter more than the individual patterns and are asserted
throughout: nothing in here raises (the callers are `pre_gateway_dispatch` and
`start_span`), and a value that is not a credential survives unchanged — an
over-eager redactor makes the audit log useless, which is the failure mode that
gets redaction switched off.
"""

import importlib
import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402
import common.redactor as redactor_module  # noqa: E402


class TestRedactText(unittest.TestCase):

    def assertRedacted(self, text, secret):
        result = AuditRedactor.redact_text(text)
        self.assertNotIn(secret, result, f"{secret!r} survived redaction of {text!r}")
        return result

    def test_empty_input_is_returned_as_is(self):
        self.assertEqual(AuditRedactor.redact_text(""), "")

    def test_private_key_block(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAx3f9\nabcdef\n"
            "-----END RSA PRIVATE KEY-----"
        )
        self.assertEqual(AuditRedactor.redact_text(text), "[REDACTED_PRIVATE_KEY]")

    def test_gcp_api_key(self):
        key = "AIza" + "a" * 35
        self.assertEqual(
            self.assertRedacted(f"key={key} rest", key), "key=[REDACTED_SECRET] rest"
        )

    def test_gcp_oauth_token(self):
        token = "ya29." + "A" * 40
        self.assertRedacted(f"token {token}", token)

    def test_bearer_token_keeps_the_scheme(self):
        result = self.assertRedacted("Authorization: Bearer abcdefghij0123456789", "abcdefghij")
        self.assertIn("Bearer [REDACTED_SECRET]", result)

    def test_basic_auth_keeps_the_scheme(self):
        # base64 of `admin:hunter2`, i.e. the credential shape a `curl -u` in a
        # tool argument leaves behind.
        result = self.assertRedacted(
            "Authorization: Basic YWRtaW46aHVudGVyMg==", "YWRtaW46aHVudGVyMg"
        )
        self.assertIn("Basic [REDACTED_SECRET]", result)

    def test_slack_bot_token(self):
        token = "xoxb-1234567890-" + "D" * 24
        self.assertRedacted(f"slack_bot_token was {token}", token)

    def test_github_fine_grained_pat(self):
        token = "github_pat_" + "E" * 40
        self.assertRedacted(f"cloned with {token}", token)

    def test_jwt_is_redacted(self):
        # The shape of a projected ServiceAccount token, which is the one most
        # likely to land in a tool result inside this pod.
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0iLCJhdWQiOlsiazhzIl19.c2lnbmF0dXJlXw"
        self.assertRedacted(f"token: {jwt}", jwt)

    def test_prefixed_key_names_are_matched(self):
        # `\bapi_key` matches neither of these: `_` is a word character, so the
        # boundary the old pattern wanted is never there.
        for text, secret in (
            ("SESSION_KV_API_KEY=abc123def456", "abc123def456"),
            ("ANTHROPIC_API_KEY: sk-live-value", "sk-live-value"),
        ):
            with self.subTest(text=text):
                result = self.assertRedacted(text, secret)
                # The key survives in full, prefix included, or the record no
                # longer says which credential was present.
                self.assertIn(text.split("=")[0].split(":")[0], result)

    def test_secret_data_block_is_blanked_whatever_the_keys_are_called(self):
        manifest = (
            "apiVersion: v1\n"
            "kind: Secret\n"
            "metadata:\n"
            "  name: platform-agent-secrets\n"
            "data:\n"
            "  SESSION_KV_API_KEY: YWJjMTIz\n"
            "  SESSION_KV_SALT: c2FsdHk=\n"
            "  ANTHROPIC_API_KEY: c2stbGl2ZQ==\n"
            "type: Opaque\n"
        )
        result = AuditRedactor.redact_text(manifest)
        for value in ("YWJjMTIz", "c2FsdHk=", "c2stbGl2ZQ=="):
            self.assertNotIn(value, result)
        # The keys stay: the record has to say what was there.
        self.assertIn("SESSION_KV_SALT: [REDACTED_SECRET]", result)
        # And the block ends where the indentation does.
        self.assertIn("type: Opaque", result)
        self.assertIn("name: platform-agent-secrets", result)

    def test_github_token(self):
        token = "ghp_" + "B" * 36
        self.assertRedacted(f"remote uses {token} today", token)

    def test_openai_token(self):
        token = "sk-" + "C" * 32
        self.assertRedacted(f"OPENAI={token}", token)

    def test_key_value_pair(self):
        result = self.assertRedacted('{"password": "hunter2"}', "hunter2")
        # The key survives: knowing a password was present is the useful part.
        self.assertIn("password", result)

    def test_key_value_pair_with_equals_and_no_quotes(self):
        self.assertRedacted("client_secret=s3cr3t-value", "s3cr3t-value")

    def test_email_address(self):
        self.assertEqual(
            AuditRedactor.redact_text("ping alice@example.com now"),
            "ping [REDACTED_EMAIL] now",
        )

    def test_ordinary_text_is_untouched(self):
        for text in (
            "kubectl get pods -n kube-system",
            "Deployment nginx has 3/3 replicas ready",
            "the tokenizer emitted 42 tokens",
            "authored by the release job",
            "image: ghcr.io/gke-labs/kube-agents/platform-agent:v0.4.1",
            # A service-account address is not personal data, and it is the one
            # thing an operator greps an IAM audit record for.
            "binding kube-agents-platform@my-proj.iam.gserviceaccount.com to roles/container.admin",
            "annotate sa default gcp-sa@my-proj.iam.gserviceaccount.com",
            # Ending a sentence must not cost the exemption.
            "granted to gcp-sa@my-proj.iam.gserviceaccount.com.",
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), text)

    def test_the_service_account_exemption_is_anchored_at_both_edges(self):
        # A domain that merely contains the label sequence is not a service
        # account: neither a prefix nor a suffix may extend it.
        for text, address in (
            ("mail victim@corp.gserviceaccount.com.attacker.io now", "victim@corp"),
            ("mail a@notgserviceaccount.com now", "a@notgserviceaccount.com"),
            ("mail b@foo.gserviceaccount.company.com now", "b@foo"),
        ):
            with self.subTest(text=text):
                self.assertRedacted(text, address)


class TestRedactStructures(unittest.TestCase):

    def test_sensitive_key_redacts_the_whole_value(self):
        self.assertEqual(
            AuditRedactor.redact({"apiKey": "not-even-a-known-shape"}),
            {"apiKey": "[REDACTED_SECRET]"},
        )

    def test_camel_case_and_snake_case_keys_both_match(self):
        for key in ("clientSecret", "client_secret", "CLIENT_SECRET", "client-secret"):
            with self.subTest(key=key):
                self.assertEqual(AuditRedactor.redact({key: "v"}), {key: "[REDACTED_SECRET]"})

    def test_keys_that_merely_contain_a_sensitive_word_are_not_matched(self):
        # Whole-word matching: `tokenizer` is not `token`, `author` is not `auth`.
        self.assertEqual(
            AuditRedactor.redact({"tokenizer": "tiktoken", "author": "release-bot"}),
            {"tokenizer": "tiktoken", "author": "release-bot"},
        )

    def test_email_keys_are_redacted_by_key_not_by_shape(self):
        # The value is not address-shaped, so only the key can catch it.
        self.assertEqual(
            AuditRedactor.redact({"userEmail": "alice"}), {"userEmail": "[REDACTED_EMAIL]"}
        )

    def test_nested_containers_are_walked(self):
        payload = {
            "tool": "kubectl",
            "args": ["--token", "Bearer abcdefghij0123456789"],
            "env": {"nested": {"password": "hunter2"}},
            "meta": ("contact alice@example.com",),
        }
        result = AuditRedactor.redact(payload)
        self.assertEqual(result["tool"], "kubectl")
        self.assertIn("Bearer [REDACTED_SECRET]", result["args"][1])
        self.assertEqual(result["env"]["nested"]["password"], "[REDACTED_SECRET]")
        self.assertEqual(result["meta"], ("contact [REDACTED_EMAIL]",))

    def test_sensitive_key_holding_a_container_still_recurses(self):
        result = AuditRedactor.redact({"credentials": {"user": "alice@example.com"}})
        self.assertEqual(result["credentials"]["user"], "[REDACTED_EMAIL]")

    def test_bytes_stay_bytes(self):
        self.assertEqual(
            AuditRedactor.redact(b"mail alice@example.com"), b"mail [REDACTED_EMAIL]"
        )

    def test_non_text_scalars_pass_through_unchanged(self):
        payload = {"count": 3, "ok": True, "ratio": 1.5, "missing": None}
        self.assertEqual(AuditRedactor.redact(payload), payload)

    def test_non_string_keys_do_not_raise(self):
        self.assertEqual(AuditRedactor.redact({1: "a", None: "b"}), {1: "a", None: "b"})


class TestHmacHash(unittest.TestCase):

    def setUp(self):
        self._previous = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"
        redactor_module._fallback_salt = None

    def tearDown(self):
        if self._previous is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous
        redactor_module._fallback_salt = None

    def test_hash_is_stable_and_hex(self):
        first = AuditRedactor.hmac_hash("alice@example.com")
        self.assertEqual(first, AuditRedactor.hmac_hash("alice@example.com"))
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_different_inputs_give_different_hashes(self):
        self.assertNotEqual(
            AuditRedactor.hmac_hash("alice@example.com"),
            AuditRedactor.hmac_hash("bob@example.com"),
        )

    def test_the_salt_changes_the_hash(self):
        with_test_salt = AuditRedactor.hmac_hash("alice@example.com")
        os.environ[SALT_ENV_VAR] = "another-salt"
        self.assertNotEqual(with_test_salt, AuditRedactor.hmac_hash("alice@example.com"))

    def test_the_plaintext_never_appears_in_the_digest(self):
        self.assertNotIn("alice", AuditRedactor.hmac_hash("alice@example.com"))

    def test_empty_value_is_empty(self):
        self.assertEqual(AuditRedactor.hmac_hash(""), "")
        self.assertEqual(AuditRedactor.hmac_hash(None), "")

    def test_missing_salt_degrades_instead_of_raising(self):
        os.environ.pop(SALT_ENV_VAR, None)
        with self.assertLogs(redactor_module.logger, level=logging.WARNING) as captured:
            first = AuditRedactor.hmac_hash("alice@example.com")
        self.assertEqual(len(first), 64)
        self.assertIn(SALT_ENV_VAR, captured.output[0])

        # Stable within the process, and the warning is not repeated per call.
        with self.assertRaises(AssertionError):
            with self.assertLogs(redactor_module.logger, level=logging.WARNING):
                second = AuditRedactor.hmac_hash("alice@example.com")
        self.assertEqual(first, second)


class TestPseudonymiseIdentity(unittest.TestCase):

    def setUp(self):
        self._previous = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"

    def tearDown(self):
        if self._previous is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous

    def test_an_address_is_hashed(self):
        result = AuditRedactor.pseudonymise_identity("alice@example.com")
        self.assertEqual(result, AuditRedactor.hmac_hash("alice@example.com"))
        self.assertNotIn("@", result)

    def test_an_opaque_slack_id_is_left_readable(self):
        # Already a pseudonym; hashing it would only cost operators the ability
        # to correlate a session with the Slack member directory.
        self.assertEqual(AuditRedactor.pseudonymise_identity("U012ABCDEF"), "U012ABCDEF")

    def test_empty_and_none_are_empty(self):
        self.assertEqual(AuditRedactor.pseudonymise_identity(""), "")
        self.assertEqual(AuditRedactor.pseudonymise_identity(None), "")

    def test_non_string_input_does_not_raise(self):
        self.assertEqual(AuditRedactor.pseudonymise_identity(42), "42")


class TestPackageSurface(unittest.TestCase):
    """`common` is a plain import target, not a Hermes plugin."""

    def test_exports(self):
        package = importlib.import_module("common")
        self.assertIs(package.AuditRedactor, AuditRedactor)
        self.assertEqual(package.SALT_ENV_VAR, SALT_ENV_VAR)

    def test_it_has_no_plugin_manifest(self):
        # plugin.yaml is what makes a directory under plugins/ a Hermes plugin.
        # This one is a shared library that happens to live beside them, so the
        # loader must keep walking past it.
        here = Path(__file__).resolve().parent
        self.assertFalse((here / "plugin.yaml").exists())
        self.assertFalse((here / "plugin.py").exists())

    def test_it_declares_no_plugin_entry_point(self):
        package = importlib.import_module("common")
        for attribute in ("register", "plugin", "Plugin", "setup"):
            self.assertFalse(hasattr(package, attribute), attribute)


if __name__ == "__main__":
    unittest.main()
