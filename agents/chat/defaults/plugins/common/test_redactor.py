"""Tests for the shared audit redactor.

Two properties matter more than the individual patterns and are asserted
throughout: nothing in here raises (the callers are `pre_gateway_dispatch` and
`start_span`), and a value that is not a credential survives unchanged — an
over-eager redactor makes the audit log useless, which is the failure mode that
gets redaction switched off.
"""

import importlib
import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.redactor import SALT_ENV_VAR, AuditRedactor, RedactionRule  # noqa: E402
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
        # The header a `curl -u` in a tool argument leaves behind. Deliberately
        # not a `user:pass` base64 payload — it decodes to
        # "not-a-real-credential" — so secret scanners do not flag the fixture.
        # The redactor keys off the `Basic` prefix and the base64 alphabet,
        # never the payload's contents.
        result = self.assertRedacted(
            "Authorization: Basic bm90LWEtcmVhbC1jcmVkZW50aWFs",
            "bm90LWEtcmVhbC1jcmVkZW50aWFs",
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

    def test_aws_and_secret_key_names_are_credential_names(self):
        for text, secret in (
            ("aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUt"),
            ("DJANGO_SECRET_KEY=abc123xyz789", "abc123xyz789"),
            ("accessKey: minio-root-pw", "minio-root-pw"),
        ):
            with self.subTest(text=text):
                self.assertRedacted(text, secret)

    def test_a_boolean_under_a_credential_name_is_left_alone(self):
        # A switch, not a credential: masking it puts a marker where a manifest
        # needs a boolean, and an agent editing that manifest copies it back.
        for text in (
            "  automountServiceAccountToken: false\n",
            '{"automountServiceAccountToken": true}',
            "use_token=False",
            "password: null",
            "api_key: none",
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), text)

    def test_a_credential_name_that_ends_its_line_does_not_take_the_next_key(self):
        # Every pod spec carries both: a projected token volume and a Secret
        # volume. The key opens a nested mapping; the next line is not its value.
        manifest = (
            "      - serviceAccountToken:\n"
            "          expirationSeconds: 3607\n"
            "    secret:\n"
            "      defaultMode: 420\n"
        )
        self.assertEqual(AuditRedactor.redact_text(manifest), manifest)

    def test_an_external_secret_key_name_is_not_a_credential(self):
        manifest = "spec:\n  data:\n    - secretKey: db-password\n"
        self.assertEqual(AuditRedactor.redact_text(manifest), manifest)

    def test_a_quoted_value_with_escapes_or_spaces_is_masked_whole(self):
        for text, expected in (
            ('{"client_secret": "a\\/b"}', '{"client_secret": "[REDACTED_SECRET]"}'),
            ('{"password": "endswith\\\\"}', '{"password": "[REDACTED_SECRET]"}'),
            ('export DB_PASSWORD="Xk9\\"q2!Lm"', 'export DB_PASSWORD="[REDACTED_SECRET]"'),
            ("password: 'has spaces inside'", "password: '[REDACTED_SECRET]'"),
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), expected)

    def test_a_list_under_a_credential_name_keeps_its_json_shape(self):
        text = '{"tokens": ["a", "b"], "api_key": "zzz"}'
        result = AuditRedactor.redact_text(text)
        self.assertEqual(result, '{"tokens": ["a", "b"], "api_key": "[REDACTED_SECRET]"}')
        json.loads(result)

    def test_an_already_masked_value_is_not_masked_again(self):
        # The data-block rule masks first and the key/value rule used to mask
        # the marker again, leaving `[REDACTED_SECRET]]`.
        result = AuditRedactor.redact_text("data:\n  password: aHVudGVyMg==\n")
        self.assertEqual(result, "data:\n  password: [REDACTED_SECRET]\n")
        self.assertEqual(
            AuditRedactor.redact_text("token: [REDACTED_SECRET]"), "token: [REDACTED_SECRET]"
        )

    def test_env_value_is_masked_when_its_name_is_credential_shaped(self):
        manifest = (
            "    env:\n"
            "    - name: DB_PASSWORD\n"
            "      value: hunter2-example-pw\n"
            "    - name: LOG_LEVEL\n"
            "      value: debug\n"
            "    - value: 'ghs-example-value'\n"
            "      name: GITHUB_TOKEN\n"
            "    - name: TOKEN_TTL\n"
            "      value: \"3600\"\n"
        )
        result = AuditRedactor.redact_text(manifest)
        for secret in ("hunter2-example-pw", "ghs-example-value"):
            self.assertNotIn(secret, result)
        # The name stays, the quoting stays, and an ordinary entry is untouched.
        self.assertIn("- name: DB_PASSWORD\n      value: [REDACTED_SECRET]\n", result)
        self.assertIn("value: '[REDACTED_SECRET]'\n      name: GITHUB_TOKEN", result)
        self.assertIn("value: debug", result)
        self.assertIn('value: "3600"', result)

    def test_env_values_that_hold_no_credential_are_left_alone(self):
        manifest = (
            "    - name: USE_TOKEN\n"
            '      value: "true"\n'
            "    - name: PG_PASSWORD\n"
            "      value: $(POSTGRES_PASSWORD)\n"
            "    - name: LLM_API_KEY\n"
            "      value: none\n"
        )
        self.assertEqual(AuditRedactor.redact_text(manifest), manifest)

    def test_a_block_scalar_under_a_credential_name_is_masked(self):
        manifest = (
            "config:\n"
            "  password: |\n"
            "    hunter2-on-next-line\n"
            "    second-line\n"
            "  mode: fast\n"
        )
        self.assertEqual(
            AuditRedactor.redact_text(manifest),
            "config:\n  password: |\n    [REDACTED_SECRET]\n  mode: fast\n",
        )

    def test_a_block_scalar_in_a_secret_data_block_is_masked(self):
        manifest = (
            "kind: Secret\n"
            "data:\n"
            "  tls.key: |\n"
            "    abc123line\n"
            "    def456line\n"
            "  other: eHl6\n"
            "type: Opaque\n"
        )
        self.assertEqual(
            AuditRedactor.redact_text(manifest),
            "kind: Secret\n"
            "data:\n"
            "  tls.key: |\n"
            "    [REDACTED_SECRET]\n"
            "  other: [REDACTED_SECRET]\n"
            "type: Opaque\n",
        )

    def test_a_block_scalar_with_an_empty_line_is_masked_to_its_end(self):
        # go-yaml prints an empty line inside a block scalar unindented.
        for manifest, expected in (
            (
                "- name: SERVICE_TOKEN\n  value: |\n    line1\n\n    line2-secret\n",
                "- name: SERVICE_TOKEN\n  value: |\n    [REDACTED_SECRET]\n",
            ),
            (
                "config:\n  password: |\n    l1\n\n    l2-secret\n  mode: fast\n",
                "config:\n  password: |\n    [REDACTED_SECRET]\n  mode: fast\n",
            ),
        ):
            with self.subTest(manifest=manifest):
                self.assertEqual(AuditRedactor.redact_text(manifest), expected)

    def test_text_cut_off_inside_an_escaped_string_stays_linear(self):
        # Output clipped mid-annotation: every `\"` used to start a scan to
        # the end of the text.
        started = time.monotonic()
        AuditRedactor.redact_text('"' + '\\"' * 80000)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_long_hyphenated_run_stays_linear(self):
        # The key/value name prefix used to rescan from every word boundary.
        started = time.monotonic()
        AuditRedactor.redact_text("a-" * 10000 + "password=x")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_env_block_scalar_value_is_masked_and_stays_a_block(self):
        manifest = (
            "        - name: SERVICE_TOKEN\n"
            "          value: |\n"
            "            multi-line-token-value-abc\n"
            "            second-line\n"
            "        - name: LOG_LEVEL\n"
            "          value: info\n"
        )
        self.assertEqual(
            AuditRedactor.redact_text(manifest),
            "        - name: SERVICE_TOKEN\n"
            "          value: |\n"
            "            [REDACTED_SECRET]\n"
            "        - name: LOG_LEVEL\n"
            "          value: info\n",
        )

    def test_env_value_is_masked_with_crlf_line_endings(self):
        self.assertEqual(
            AuditRedactor.redact_text("    - name: DB_PASSWORD\r\n      value: hunter2\r\n"),
            "    - name: DB_PASSWORD\r\n      value: [REDACTED_SECRET]\r\n",
        )

    def test_a_long_run_of_spaces_in_an_env_value_stays_linear(self):
        # The value capture used to rescan the run once per character: 64 KB
        # took about nine seconds. The bound is loose on purpose.
        for text in (
            "- name: A\n  value: x" + " " * 65536 + "y\n",
            "- value: x" + " " * 65536 + "y\n  name: A\n",
        ):
            started = time.monotonic()
            AuditRedactor.redact_text(text)
            self.assertLess(time.monotonic() - started, 2.0)

    def test_env_value_from_a_secret_ref_is_left_alone(self):
        manifest = (
            "    - name: DB_PASSWORD\n"
            "      valueFrom:\n"
            "        secretKeyRef:\n"
            "          name: db\n"
            "          key: password\n"
        )
        self.assertEqual(AuditRedactor.redact_text(manifest), manifest)

    def test_env_value_is_masked_in_json_either_order(self):
        for text, secret in (
            ('{"name": "DB_PASSWORD", "value": "hunter2-example-pw"}', "hunter2-example-pw"),
            ('{\n  "name": "API_KEY",\n  "value": "a\\"b-c"\n}', 'a\\"b-c'),
            ('{"value":"s3cr3t-value","name":"CLIENT_SECRET"}', "s3cr3t-value"),
        ):
            with self.subTest(text=text):
                result = self.assertRedacted(text, secret)
                self.assertIn('"[REDACTED_SECRET]"', result)
        ordinary = '{"name": "LOG_LEVEL", "value": "debug"}'
        self.assertEqual(AuditRedactor.redact_text(ordinary), ordinary)

    def test_url_password_is_masked_and_the_user_and_host_kept(self):
        for text, expected in (
            (
                "DATABASE_URL=postgres://checkout:hunter2-example-pw@db.shop.internal:5432/orders",
                "DATABASE_URL=postgres://checkout:[REDACTED_SECRET]@db.shop.internal:5432/orders",
            ),
            ("redis://:Hunter2ExamplePw@cache:6379/0", "redis://:[REDACTED_SECRET]@cache:6379/0"),
            ("amqp://u:p%40ss:w0rd@mq/vhost", "amqp://u:[REDACTED_SECRET]@mq/vhost"),
            # A raw `@` in the password: parsers split on the last one.
            ("postgres://app:p@ssw0rd@db:5432/x", "postgres://app:[REDACTED_SECRET]@db:5432/x"),
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), expected)

    def test_urls_without_a_password_are_untouched(self):
        for text in (
            "http://example.com:8080/path?a=b",
            "https://user@host/x",
            "see https://example.com:443/@team/page",
            # Placeholders are not credentials, and the marker would hide that.
            "mysql://root:<password>@localhost:3306/app",
            "mongodb+srv://app:${MONGO_PASSWORD}@cluster0/x",
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), text)

    def test_a_url_does_not_swallow_the_fields_after_it(self):
        # Compact JSON and CSV carry no whitespace to end the userinfo: the
        # match must still stop at the field boundary rather than run to the
        # next `@`, which would eat the fields between and break the JSON.
        for text, expected in (
            (
                '{"upstream":"http://payments:8443","user":"alice@example.com"}',
                '{"upstream":"http://payments:8443","user":"[REDACTED_EMAIL]"}',
            ),
            (
                '{"url":"http://nginx:80","image":"nginx@sha256:3f1e"}',
                '{"url":"http://nginx:80","image":"nginx@sha256:3f1e"}',
            ),
            (
                "endpoint=http://svc:8080;contact=ops@example.com",
                "endpoint=http://svc:8080;contact=[REDACTED_EMAIL]",
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), expected)

    def test_json_secret_data_is_blanked(self):
        secret = (
            '{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "db"},'
            ' "data": {"db-pass": "aHVudGVy", "config.json": "{\\"pw\\": \\"x\\"}"},'
            ' "stringData": {"url": "plain-text-value"}}'
        )
        result = AuditRedactor.redact_text(secret)
        for value in ("aHVudGVy", '\\"pw\\"', "plain-text-value"):
            self.assertNotIn(value, result)
        # The keys and the envelope stay.
        self.assertIn('"db-pass": "[REDACTED_SECRET]"', result)
        self.assertIn('"metadata": {"name": "db"}', result)

    def test_json_inside_a_json_string_is_redacted_and_stays_valid(self):
        # `kubectl get -o json` repeats a kubectl-applied object, escaped, in
        # its last-applied-configuration annotation.
        applied = json.dumps({"kind": "Secret", "data": {"password": "aHVudGVy"}})
        secret = json.dumps(
            {
                "kind": "Secret",
                "data": {"password": "aHVudGVy"},
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/last-applied-configuration": applied}
                },
            },
            indent=2,
        )
        result = AuditRedactor.redact_text(secret)
        self.assertNotIn("aHVudGVy", result)
        parsed = json.loads(result)
        inner = json.loads(
            parsed["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"]
        )
        self.assertEqual(inner["data"], {"password": "[REDACTED_SECRET]"})
        env = json.dumps(
            {"env": [{"name": "DB_PASSWORD", "value": "hunter2-example-pw"}]}
        )
        deployment = json.dumps({"metadata": {"annotations": {"applied": env}}})
        result = AuditRedactor.redact_text(deployment)
        self.assertNotIn("hunter2-example-pw", result)
        json.loads(result)

    def test_yaml_inside_a_json_string_keeps_its_booleans_and_line_breaks(self):
        config_map = json.dumps(
            {"data": {"pod.yaml": "spec:\n  automountServiceAccountToken: false\n  password: pw\n"}}
        )
        self.assertEqual(
            json.loads(AuditRedactor.redact_text(config_map))["data"]["pod.yaml"],
            "spec:\n  automountServiceAccountToken: false\n  password: [REDACTED_SECRET]\n",
        )

    def test_json_data_that_is_not_a_secret_is_untouched(self):
        # `data` is also the envelope of ordinary JSON: a Prometheus result.
        for text in (
            '{"status": "success", "data": {"resultType": "vector", "result": []}}',
            '{"kind": "ConfigMap", "data": {"LOG_LEVEL": "debug"}}',
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), text)

    def test_anthropic_and_hyphenated_openai_keys(self):
        for token in (
            "sk-ant-api03-" + "A1b2C3d4" * 5,
            "sk-proj-" + "E5f6_G7h-" * 5,
            "sk-svcacct-" + "J8k9L0m1" * 5,
        ):
            with self.subTest(token=token):
                self.assertRedacted(f"key={token} ", token)

    def test_hyphenated_names_that_contain_a_key_prefix_are_untouched(self):
        for text in (
            "pod task-proj-abcdefghijklmnopqrstu is Running",
            "disk-proj-abcdefghijklmnopqrstuvwx attached",
            # Kubernetes names are lower-case by rule; real keys are not.
            "kubectl logs deploy/sk-proj-ingestion-pipeline-worker-7d9f8b6c5d -n data",
            "configmap/sk-admin-console-feature-flags-production-eu",
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), text)

    def test_aws_access_key_ids(self):
        # AWS's documented example id, and the same body under the STS prefix.
        # The second is assembled here: as a literal it matches GitHub secret
        # scanning's temporary-key pattern, which allowlists only the first.
        for key_id in ("AKIAIOSFODNN7EXAMPLE", "ASIA" + "IOSFODNN7EXAMPLE"):
            with self.subTest(key_id=key_id):
                self.assertRedacted(f"id {key_id} used", key_id)
        # A longer upper-case run is not a key id, and neither is one with a
        # digit outside the base32 alphabet.
        for text in ("AKIAIOSFODNN7EXAMPLEXX", "location: ASIASOUTHEAST1ZONEAB"):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text), text)

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

    def test_a_parsed_secret_has_its_data_blanked(self):
        secret = {
            "kind": "Secret",
            "metadata": {"name": "db"},
            "data": {"db-pass": "aHVudGVy"},
            "stringData": {"url": "plain-text-value"},
        }
        self.assertEqual(
            AuditRedactor.redact(secret),
            {
                "kind": "Secret",
                "metadata": {"name": "db"},
                "data": {"db-pass": "[REDACTED_SECRET]"},
                "stringData": {"url": "[REDACTED_SECRET]"},
            },
        )
        # The same fields on any other kind are only redacted by shape.
        config_map = {"kind": "ConfigMap", "data": {"LOG_LEVEL": "debug"}}
        self.assertEqual(AuditRedactor.redact(config_map), config_map)

    def test_a_parsed_env_entry_and_credential_names_are_masked_by_key(self):
        payload = {
            "env": [
                {"name": "DB_PASSWORD", "value": "hunter2"},
                {"name": "LOG_LEVEL", "value": "debug"},
            ],
            "accessKey": "minio-root-pw",
            "token": "true",
        }
        redacted, counts = AuditRedactor.redact_counted(payload)
        self.assertEqual(
            redacted,
            {
                "env": [
                    {"name": "DB_PASSWORD", "value": "[REDACTED_SECRET]"},
                    {"name": "LOG_LEVEL", "value": "debug"},
                ],
                "accessKey": "[REDACTED_SECRET]",
                # A switch, as in the text rules.
                "token": "true",
            },
        )
        self.assertEqual(counts, {"credential": 2})
        self.assertEqual(AuditRedactor.redact(payload), redacted)

    def test_a_value_that_only_starts_with_a_marker_is_still_masked(self):
        self.assertEqual(
            AuditRedactor.redact({"password": "[REDACTED_SECRET] ghp_" + "a" * 36}),
            {"password": "[REDACTED_SECRET]"},
        )
        self.assertEqual(
            AuditRedactor.redact_counted({"token": "[REDACTED_SECRET]"}),
            ({"token": "[REDACTED_SECRET]"}, {}),
        )

    def test_keys_that_name_or_point_at_a_credential_are_not_masked(self):
        payload = {
            "secretName": "db-creds",
            "tokenPath": "/var/run/secrets/token",
            "authMode": "iam",
            "passwordFile": "/etc/pw",
            "api_key": "abcd1234efgh",
        }
        self.assertEqual(
            AuditRedactor.redact(payload),
            {**payload, "api_key": "[REDACTED_SECRET]"},
        )

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


class TestRedactionRules(unittest.TestCase):
    """The operator-configured layer that runs after the credential patterns."""

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

    def test_no_rules_leaves_the_output_exactly_as_before(self):
        text = "pod at 10.0.0.5 in prod-eu-1 with ya29." + "A" * 40
        self.assertEqual(AuditRedactor.redact_text(text), AuditRedactor.redact_text(text, None))
        self.assertEqual(AuditRedactor.redact_text(text), AuditRedactor.redact_text(text, []))
        self.assertIn("10.0.0.5 in prod-eu-1", AuditRedactor.redact_text(text))

    def test_ip_literals_are_masked(self):
        rules = AuditRedactor.ip_rules("mask")
        self.assertEqual(
            AuditRedactor.redact_text("from 10.0.0.5 to fe80::1 done", rules),
            "from [REDACTED_IP] to [REDACTED_IP] done",
        )

    def test_ip_literals_are_pseudonymised_stably(self):
        rules = AuditRedactor.ip_rules("pseudonym")
        first = AuditRedactor.redact_text("10.0.0.5", rules)
        self.assertRegex(first, r"^\[ip:[0-9a-f]{12}\]$")
        self.assertEqual(first, AuditRedactor.redact_text("10.0.0.5", rules))
        self.assertNotEqual(first, AuditRedactor.redact_text("10.0.0.6", rules))
        # The token is the salted HMAC prefix, so it moves with the salt and
        # nothing in it is the address.
        os.environ[SALT_ENV_VAR] = "another-salt"
        self.assertNotEqual(first, AuditRedactor.redact_text("10.0.0.5", rules))

    def test_equivalent_ipv6_spellings_share_a_pseudonym(self):
        rules = AuditRedactor.ip_rules("pseudonym")
        self.assertEqual(
            AuditRedactor.redact_text("::1", rules),
            AuditRedactor.redact_text("0:0:0:0:0:0:0:1", rules),
        )

    def test_allowlisted_cidrs_are_left_alone(self):
        rules = AuditRedactor.ip_rules("mask", ["127.0.0.0/8", "10.96.0.0/12", "fd00::/8"])
        text = "127.0.0.1 10.96.0.10 fd00::1 stay; 10.0.0.5 fe80::1 go"
        self.assertEqual(
            AuditRedactor.redact_text(text, rules),
            "127.0.0.1 10.96.0.10 fd00::1 stay; [REDACTED_IP] [REDACTED_IP] go",
        )

    def test_ip_action_off_disables_the_ip_rules(self):
        self.assertEqual(AuditRedactor.ip_rules("off"), [])

    def test_things_that_look_like_addresses_but_are_not(self):
        rules = AuditRedactor.ip_rules("mask")
        for text in (
            "at 12:30:45 on 2026-09-09T16:27:58Z",
            "mac aa:bb:cc:dd:ee:ff",
            "image litellm:v1.98.0 and version 1.2.3",
            "octet 999.1.1.1 is not an address",
            "a longer dotted run 1.2.3.4.5",
        ):
            with self.subTest(text=text):
                self.assertEqual(AuditRedactor.redact_text(text, rules), text)

    def test_an_address_with_a_port_or_a_prefix_length_keeps_its_suffix(self):
        rules = AuditRedactor.ip_rules("mask")
        self.assertEqual(
            AuditRedactor.redact_text("10.0.0.5:8080 and 10.0.0.0/8", rules),
            "[REDACTED_IP]:8080 and [REDACTED_IP]/8",
        )

    def test_a_literal_rule_matches_the_exact_string_only(self):
        rules = AuditRedactor.rules_from_config(
            {"rules": [{"name": "cluster-name", "literal": "prod.eu-1", "action": "pseudonym"}]}
        )
        result = AuditRedactor.redact_text("prod.eu-1 is not prodXeu-1", rules)
        self.assertRegex(result, r"^\[cluster-name:[0-9a-f]{12}\] is not prodXeu-1$")

    def test_a_pattern_rule_masks_under_its_own_name(self):
        rules = AuditRedactor.rules_from_config(
            {"ip": {"action": "off"}, "rules": [{"name": "project", "pattern": r"my-proj-\d+"}]}
        )
        self.assertEqual(
            AuditRedactor.redact_text("in my-proj-42 now", rules), "in [REDACTED_PROJECT] now"
        )

    def test_rules_run_after_the_credential_patterns(self):
        # A rule cannot un-redact a credential by matching it first.
        rules = AuditRedactor.rules_from_config(
            {"ip": {"action": "off"}, "rules": [{"name": "t", "pattern": "ya29", "action": "mask"}]}
        )
        token = "ya29." + "A" * 40
        self.assertEqual(AuditRedactor.redact_text(token, rules), "[REDACTED_SECRET]")

    def test_the_config_loader_defaults_to_ip_pseudonyms(self):
        rules = AuditRedactor.rules_from_config({})
        self.assertEqual([r.name for r in rules], ["ip", "ip"])
        self.assertTrue(all(r.action == "pseudonym" for r in rules))
        self.assertEqual(
            [(r.name, r.action) for r in AuditRedactor.rules_from_config(None)],
            [(r.name, r.action) for r in rules],
        )

    def test_the_config_loader_refuses_what_it_does_not_understand(self):
        for config in (
            {"ip": {"action": "reverse"}},
            {"ip": {"allowCidrs": ["not-a-cidr"]}},
            {"ip": {"cidrs": []}},
            {"rules": [{"name": "x", "pattern": "a", "action": "hash"}]},
            {"rules": [{"name": "x", "pattern": "a", "literal": "a"}]},
            {"rules": [{"name": "x"}]},
            {"rules": [{"name": "x", "pattern": "("}]},
            {"rules": [{"name": "bad name", "pattern": "a"}]},
            {"rules": [{"pattern": "a"}]},
            {"rules": [{"name": "x", "regex": "a"}]},
            {"rules": ["x"]},
            {"extra": {}},
            # Empty, or matching the empty string: either would put a marker
            # between every character of every request.
            {"rules": [{"name": "x", "literal": ""}]},
            {"rules": [{"name": "x", "pattern": ""}]},
            {"rules": [{"name": "x", "pattern": "a*"}]},
            {"rules": [{"name": "x", "pattern": "(?:)"}]},
            # What YAML makes of a blank value, a bare `yes` and `1.10`; each
            # would otherwise become a rule for a word the operator never wrote.
            {"rules": [{"name": "x", "literal": None}]},
            {"rules": [{"name": "x", "pattern": None}]},
            {"rules": [{"name": "x", "literal": True}]},
            {"rules": [{"name": "x", "literal": 1.1}]},
            {"rules": [{"name": 7, "literal": "a"}]},
            {"rules": [{"name": "x", "literal": "a", "action": False}]},
            {"ip": {"action": False}},
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    AuditRedactor.rules_from_config(config)

    def test_a_mask_marker_folds_the_name_to_one_spelling(self):
        import re

        for name in ("cluster-name", "cluster.name", "cluster_name", "cluster--name"):
            with self.subTest(name=name):
                rule = RedactionRule(name, re.compile("x"), "mask")
                self.assertEqual(rule.mask, "[REDACTED_CLUSTER_NAME]")

    def test_a_generator_of_rules_is_not_spent_after_the_first_string(self):
        rules = (rule for rule in AuditRedactor.ip_rules("mask"))
        self.assertEqual(
            AuditRedactor.redact({"a": "10.0.0.5", "b": ["10.0.0.6", {"c": "10.0.0.7"}]}, rules),
            {"a": "[REDACTED_IP]", "b": ["[REDACTED_IP]", {"c": "[REDACTED_IP]"}]},
        )

    def test_a_rule_with_an_unknown_action_is_refused_at_construction(self):
        import re

        with self.assertRaises(ValueError):
            RedactionRule("x", re.compile("a"), "hash")

    def test_counts_name_every_layer_that_fired(self):
        rules = AuditRedactor.rules_from_config(
            {"rules": [{"name": "cluster-name", "literal": "prod-eu-1"}]}
        )
        text = "ya29." + "A" * 40 + " alice@example.com 10.0.0.5 10.0.0.6 prod-eu-1"
        redacted, counts = AuditRedactor.redact_text_counted(text, rules)
        self.assertEqual(counts, {"credential": 1, "email": 1, "ip": 2, "cluster-name": 1})
        for literal in ("ya29", "alice", "10.0.0", "prod-eu-1"):
            self.assertNotIn(literal, redacted)
        # Nothing to do, nothing counted -- and an already-masked marker is not
        # counted as work this call did.
        self.assertEqual(AuditRedactor.redact_text_counted("[REDACTED_SECRET] ok", rules)[1], {})

    def test_a_url_password_is_masked_before_the_host_is_pseudonymised(self):
        rules = AuditRedactor.ip_rules("mask")
        redacted, counts = AuditRedactor.redact_text_counted(
            "postgres://checkout:hunter2-example-pw@10.20.3.14:5432/orders", rules
        )
        self.assertEqual(
            redacted, "postgres://checkout:[REDACTED_SECRET]@[REDACTED_IP]:5432/orders"
        )
        self.assertEqual(counts, {"credential": 1, "ip": 1})

    def test_structures_carry_the_rules_down(self):
        rules = AuditRedactor.ip_rules("mask")
        self.assertEqual(
            AuditRedactor.redact({"args": ["10.0.0.5"], "note": ("10.0.0.6",)}, rules),
            {"args": ["[REDACTED_IP]"], "note": ("[REDACTED_IP]",)},
        )


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
