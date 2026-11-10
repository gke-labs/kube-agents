"""The Python task-credential verifier against the Go one's golden fixtures."""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

import task_credential
from task_credential import TaskCredentialError

# Minted and verified by a2a/taskcred/proxy_fixtures_test.go, which says how to
# regenerate them. Read from the checkout: the file is the contract between the
# two verifiers, so there is one copy of it.
FIXTURES = Path(__file__).resolve().parents[3] / "a2a" / "taskcred" / "testdata" / "credential_proxy_fixtures.json"


def load_fixtures() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


class GoldenFixtureTest(unittest.TestCase):
    """Each fixture's proxy verdict, and the claims an accepted one carries."""

    @classmethod
    def setUpClass(cls):
        cls.doc = load_fixtures()

    def verify(self, case, **overrides):
        return task_credential.verify(
            case["token"],
            tuple(overrides.get("trusted", case.get("trusted") or self.doc["trusted"])),
            now=overrides.get("now", case["now"]),
            max_lifetime=self.doc["max_lifetime_seconds"],
            skew=self.doc["skew_seconds"],
        )

    def test_the_fixture_file_is_the_credential_proxy_audience(self):
        self.assertEqual(task_credential.AUDIENCE, self.doc["audience"])
        self.assertEqual(task_credential.SKEW_SECONDS, self.doc["skew_seconds"])
        self.assertGreater(len(self.doc["cases"]), 40)

    def test_every_case_gets_its_proxy_verdict(self):
        for case in self.doc["cases"]:
            with self.subTest(case=case["name"]):
                if case["proxy_ok"]:
                    claims = self.verify(case)
                    want = case["claims"]
                    self.assertEqual(want["task"], claims.task)
                    self.assertEqual(want["profile"], claims.profile)
                    self.assertEqual(want["tier"], claims.tier)
                    self.assertEqual(want["pod"], claims.pod)
                    self.assertEqual(want["pod_uid"], claims.pod_uid)
                    self.assertEqual(want["exp"], claims.expires)
                    self.assertEqual(
                        task_credential.TaskTarget(**want["target"]), claims.target
                    )
                    self.assertTrue(claims.jti)
                else:
                    with self.assertRaises(TaskCredentialError):
                        self.verify(case)

    def test_the_proxy_is_never_laxer_than_go(self):
        for case in self.doc["cases"]:
            with self.subTest(case=case["name"]):
                self.assertFalse(case["proxy_ok"] and not case["go_ok"])

    def test_the_named_negative_cases_are_present(self):
        # The cases the parity contract names; renaming one in the generator
        # without updating this list fails here rather than silently
        # dropping its coverage.
        names = {case["name"] for case in self.doc["cases"]}
        for name in (
            "tampered after signing",
            "signed by an untrusted key, naming it",
            "a valid token, verified by a verifier that trusts another key",
            "a v1 header, signed the v1 way",
            "a duplicate aud",
            "a duplicate nats key",
            "a case-variant AUD beside aud",
            "an unknown top-level claim",
            "no target",
            "a non-canonical base64url signature",
            "expired a minute ago",
            "a span past the ceiling, with exp-now inside it",
            "expiry past the ceiling plus skew from now",
            "the nats audience",
        ):
            with self.subTest(name=name):
                self.assertIn(name, names)

    def _case(self, name):
        return next(case for case in self.doc["cases"] if case["name"] == name)

    def test_a_valid_token_from_an_untrusted_key_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "not a trusted"):
            self.verify(self._case("valid"), trusted=[self.doc["untrusted"]])

    def test_a_valid_token_expires_with_the_clock(self):
        case = self._case("valid")
        self.verify(case)
        exp = case["claims"]["exp"]
        with self.assertRaisesRegex(TaskCredentialError, "expired"):
            self.verify(case, now=exp + self.doc["skew_seconds"])

    def test_a_valid_token_is_refused_by_a_shorter_ceiling(self):
        case = self._case("valid")
        with self.assertRaisesRegex(TaskCredentialError, "ceiling"):
            task_credential.verify(case["token"], tuple(self.doc["trusted"]), case["now"], max_lifetime=60, skew=30)


class StrictnessTest(unittest.TestCase):
    """The checks the fixtures exercise, one at a time, on a re-signed payload."""

    @classmethod
    def setUpClass(cls):
        cls.doc = load_fixtures()
        cls.valid = next(c for c in cls.doc["cases"] if c["name"] == "valid")

    def test_the_header_must_be_v2_nkey(self):
        head = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "ed25519"}).encode()).rstrip(b"=").decode()
        _, payload, signature = self.valid["token"].split(".")
        with self.assertRaisesRegex(TaskCredentialError, "header"):
            task_credential.verify(f"{head}.{payload}.{signature}", tuple(self.doc["trusted"]), self.valid["now"], 3600)

    def test_a_v1_header_is_refused_even_with_a_valid_signature(self):
        # Re-signed with the fixture's fixed test seed (0x11 * 32, see
        # proxy_fixtures_test.go), so the signature over header.payload
        # verifies and only the header check refuses it.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(bytes([0x11]) * 32)
        _, payload, _ = self.valid["token"].split(".")
        for header in ({"typ": "JWT", "alg": "ed25519"}, {"typ": "JWS", "alg": "ed25519-nkey"}):
            with self.subTest(header=header):
                head = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
                signature = base64.urlsafe_b64encode(key.sign(f"{head}.{payload}".encode())).rstrip(b"=").decode()
                with self.assertRaisesRegex(TaskCredentialError, "header"):
                    task_credential.verify(f"{head}.{payload}.{signature}", tuple(self.doc["trusted"]), self.valid["now"], 3600)
        # Control: the same re-signing under the v2 header verifies.
        head = self.valid["token"].split(".")[0]
        signature = base64.urlsafe_b64encode(key.sign(f"{head}.{payload}".encode())).rstrip(b"=").decode()
        task_credential.verify(f"{head}.{payload}.{signature}", tuple(self.doc["trusted"]), self.valid["now"], 3600)

    def test_padding_and_whitespace_are_refused(self):
        token = self.valid["token"]
        for bad in (token + "=", token.replace(".", ".\n", 1), " " + token):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(TaskCredentialError):
                    task_credential.verify(bad, tuple(self.doc["trusted"]), self.valid["now"], 3600)

    def test_a_non_canonical_signature_is_go_ok_and_proxy_refused(self):
        # Go's decoder ignores the signature's unused trailing bits; the
        # proxy re-encodes and refuses. The fixture pins both verdicts, and
        # this pins that the refusal is the canonical-encoding check rather
        # than some earlier one.
        case = next(c for c in self.doc["cases"] if c["name"] == "a non-canonical base64url signature")
        self.assertTrue(case["go_ok"])
        self.assertFalse(case["proxy_ok"])
        with self.assertRaisesRegex(TaskCredentialError, "not canonical base64url"):
            task_credential.verify(case["token"], tuple(self.doc["trusted"]), case["now"], 3600)

    def test_an_oversized_token_is_refused_before_decoding(self):
        with self.assertRaisesRegex(TaskCredentialError, "too long"):
            task_credential.verify("a" * (task_credential.MAX_TOKEN_BYTES + 1), tuple(self.doc["trusted"]), 0, 3600)

    def test_the_verifier_refuses_a_bad_configuration(self):
        token, trusted, now = self.valid["token"], tuple(self.doc["trusted"]), self.valid["now"]
        for kwargs in (
            {"trusted": ()},
            {"trusted": trusted * 3},
            {"trusted": ("CNOTAKEY",)},
            {"max_lifetime": 0},
            {"skew": -1},
        ):
            with self.subTest(kwargs=kwargs):
                args = {"trusted": trusted, "now": now, "max_lifetime": 3600, "skew": 30, **kwargs}
                with self.assertRaises(TaskCredentialError):
                    task_credential.verify(token, args["trusted"], args["now"], args["max_lifetime"], args["skew"])


class KeyDecodingTest(unittest.TestCase):
    def test_the_fixture_keys_decode(self):
        doc = load_fixtures()
        for key in (*doc["trusted"], doc["untrusted"]):
            self.assertEqual(32, len(task_credential.decode_cluster_public_key(key)))

    def test_a_flipped_character_fails_the_checksum(self):
        key = load_fixtures()["trusted"][0]
        flipped = key[:10] + ("A" if key[10] != "A" else "B") + key[11:]
        with self.assertRaisesRegex(TaskCredentialError, "checksum|cluster"):
            task_credential.decode_cluster_public_key(flipped)

    def test_another_key_type_is_refused(self):
        # An account key (A...) with a valid checksum: the right length and
        # alphabet, the wrong prefix.
        raw = bytes([0 << 3]) + bytes(32)
        crc = task_credential._crc16_xmodem(raw).to_bytes(2, "little")
        account = base64.b32encode(raw + crc).decode().rstrip("=")
        self.assertEqual(56, len(account))
        with self.assertRaisesRegex(TaskCredentialError, "cluster"):
            task_credential.decode_cluster_public_key(account)

    def test_the_key_list_is_one_or_two_keys(self):
        doc = load_fixtures()
        one, two = doc["trusted"][0], doc["untrusted"]
        self.assertEqual((), task_credential.parse_trusted_keys(""))
        self.assertEqual((one,), task_credential.parse_trusted_keys(f" {one} "))
        self.assertEqual((one, two), task_credential.parse_trusted_keys(f"{one},{two}"))
        for bad in (f"{one},", f"{one},{one}", f"{one},{two},{one}", "nope", f"{one},{two},C" + "A" * 55):
            with self.subTest(bad=bad[:30]):
                with self.assertRaises(TaskCredentialError):
                    task_credential.parse_trusted_keys(bad)


class LifetimeTest(unittest.TestCase):
    def test_go_durations(self):
        for raw, seconds in (("6h0m0s", 21600), ("21600s", 21600), ("1h30m", 5400), ("1.5h", 5400), ("90m", 5400), ("500ms", 0.5)):
            with self.subTest(raw=raw):
                self.assertAlmostEqual(seconds, task_credential.parse_go_duration(raw))

    def test_the_lifetime_setting(self):
        self.assertEqual(task_credential.DEFAULT_MAX_LIFETIME_SECONDS, task_credential.max_lifetime_from_env(None))
        self.assertEqual(task_credential.DEFAULT_MAX_LIFETIME_SECONDS, task_credential.max_lifetime_from_env("  "))
        self.assertEqual(3600, task_credential.max_lifetime_from_env("1h"))
        for bad in ("6", "-1h", "0s", "169h", "1d", "h"):
            with self.subTest(bad=bad):
                with self.assertRaises(TaskCredentialError):
                    task_credential.max_lifetime_from_env(bad)


class IdentityMapTest(unittest.TestCase):
    SA = "system:serviceaccount:kubeagents-profiles:agentprofile-auditor-read"

    def test_a_rendered_map_parses(self):
        identities = task_credential.parse_identity_map(json.dumps({self.SA: {"profile": "auditor", "tier": "read"}}))
        self.assertEqual(task_credential.ProfileIdentity("auditor", "read"), identities[self.SA])
        self.assertEqual({}, task_credential.parse_identity_map(""))

    def test_a_malformed_map_is_refused(self):
        for raw in (
            "[]",
            json.dumps({"auditor": {"profile": "auditor", "tier": "read"}}),
            json.dumps({self.SA: {"profile": "auditor"}}),
            json.dumps({self.SA: {"profile": "auditor", "tier": "read", "extra": "x"}}),
            json.dumps({self.SA: {"profile": "Auditor", "tier": "read"}}),
            '{"%s": {"profile": "a", "tier": "read"}, "%s": {"profile": "b", "tier": "read"}}' % (self.SA, self.SA),
        ):
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(TaskCredentialError):
                    task_credential.parse_identity_map(raw)


class BindingTest(unittest.TestCase):
    SA = "system:serviceaccount:kubeagents-profiles:agentprofile-auditor-read"

    def setUp(self):
        doc = load_fixtures()
        valid = next(c for c in doc["cases"] if c["name"] == "valid")
        self.claims = task_credential.verify(valid["token"], tuple(doc["trusted"]), valid["now"], 3600)
        self.identities = {self.SA: task_credential.ProfileIdentity("auditor", "read")}

    def bind(self, **overrides):
        args = {
            "workload": self.SA,
            "pod": "auditor-read-x7k2p",
            "pod_uid": "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0",
            "identities": self.identities,
            **overrides,
        }
        task_credential.bind(self.claims, **args)

    def test_the_attested_pod_binds(self):
        self.bind()

    def test_another_pod_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "another pod$"):
            self.bind(pod="auditor-read-other")

    def test_no_attested_pod_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "another pod$"):
            self.bind(pod="")

    def test_another_pod_uid_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "pod UID"):
            self.bind(pod_uid="11111111-2222-3333-4444-555555555555")

    def test_no_attested_pod_uid_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "pod UID"):
            self.bind(pod_uid="")

    def test_another_profile_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "profile"):
            self.bind(identities={self.SA: task_credential.ProfileIdentity("other", "read")})

    def test_another_tier_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "tier"):
            self.bind(identities={self.SA: task_credential.ProfileIdentity("auditor", "unattended")})

    def test_an_unmapped_service_account_is_refused(self):
        with self.assertRaisesRegex(TaskCredentialError, "no profile identity"):
            self.bind(workload="system:serviceaccount:kubeagents-profiles:someone-else")


if __name__ == "__main__":
    unittest.main()
