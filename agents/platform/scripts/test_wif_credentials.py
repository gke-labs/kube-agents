import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import wif_credentials

AUDIENCE = (
    "//iam.googleapis.com/projects/123456789012/locations/global/"
    "workloadIdentityPools/kubeagents/providers/kage-management"
)
SERVICE_ACCOUNT = "kubeagents-platform-gsa@example-project.iam.gserviceaccount.com"


class DocumentTest(unittest.TestCase):
    def setUp(self):
        self.document = wif_credentials.build_document(
            AUDIENCE, SERVICE_ACCOUNT, "/var/run/secrets/kubeagents/wif/token"
        )

    def test_it_reads_a_file_rather_than_the_metadata_server(self):
        # The whole reason this exists. A metadata-server identity is resolved by
        # pod IP, which every container in the pod shares; a file is behind a
        # mount namespace, which is the one boundary a pod has per container.
        self.assertEqual("external_account", self.document["type"])
        self.assertEqual(
            {"file": "/var/run/secrets/kubeagents/wif/token", "format": {"type": "text"}},
            self.document["credential_source"],
        )

    def test_the_projected_token_is_declared_as_a_jwt(self):
        # STS defaults a file source to an opaque token and rejects the
        # projection with an error that names neither the token nor the format.
        self.assertEqual(
            "urn:ietf:params:oauth:token-type:jwt", self.document["subject_token_type"]
        )

    def test_it_impersonates_rather_than_holding_roles_itself(self):
        self.assertEqual(
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{SERVICE_ACCOUNT}:generateAccessToken",
            self.document["service_account_impersonation_url"],
        )


class MainTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.token = self.root / "token"
        self.token.write_text("a.projected.jwt", encoding="utf-8")
        self.credential_file = self.root / "runtime" / "wif-credentials.json"

    def environment(self, **overrides):
        values = {
            "CREDENTIAL_PROXY_WIF_AUDIENCE": AUDIENCE,
            "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT": SERVICE_ACCOUNT,
            "CREDENTIAL_PROXY_WIF_TOKEN_FILE": str(self.token),
            "CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE": str(self.credential_file),
        }
        values.update(overrides)
        return values

    def run_main(self, **overrides):
        with mock.patch.dict(os.environ, self.environment(**overrides), clear=True):
            return wif_credentials.main([])

    def test_writes_the_document_and_nothing_else_can_read_it(self):
        self.assertEqual(0, self.run_main())
        written = json.loads(self.credential_file.read_text(encoding="utf-8"))
        self.assertEqual(AUDIENCE, written["audience"])
        # The file names the token path and the impersonation target, so it is
        # not itself a secret — but it is the pod's whole GCP identity in one
        # place, and the container runs as the sandbox login's uid.
        mode = stat.S_IMODE(self.credential_file.stat().st_mode)
        self.assertEqual(0o600, mode)

    def test_no_audience_is_the_standalone_placement_and_writes_nothing(self):
        self.assertEqual(0, self.run_main(CREDENTIAL_PROXY_WIF_AUDIENCE=""))
        self.assertFalse(self.credential_file.exists())

    def test_a_half_configured_pod_fails_rather_than_starting(self):
        # The operator sets all four together, so reaching this means the pod
        # spec was edited by hand. A CrashLoopBackOff is faster to notice than
        # every credentialed command reporting itself unconfigured.
        for missing in (
            "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT",
            "CREDENTIAL_PROXY_WIF_TOKEN_FILE",
            "CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE",
        ):
            with self.subTest(missing=missing):
                self.assertEqual(1, self.run_main(**{missing: ""}))

    def test_an_unmounted_token_volume_fails_loudly(self):
        # The mistake this design is most exposed to: the pod comes up either
        # way, and only the credentialed commands fail.
        self.token.unlink()
        self.assertEqual(1, self.run_main())
        self.assertFalse(self.credential_file.exists())

    def test_rewriting_leaves_no_partial_file_behind(self):
        self.assertEqual(0, self.run_main())
        self.assertEqual(0, self.run_main())
        leftovers = [p.name for p in self.credential_file.parent.iterdir()]
        self.assertEqual(["wif-credentials.json"], leftovers)


class IdentityTokenTest(unittest.TestCase):
    """`fetch_identity_token`, which is the GitHub path under federation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.token = self.root / "token"
        self.token.write_text("a.projected.jwt", encoding="utf-8")
        self.credential_file = self.root / "wif-credentials.json"
        wif_credentials.write_document(
            str(self.credential_file),
            wif_credentials.build_document(AUDIENCE, SERVICE_ACCOUNT, str(self.token)),
        )
        self.posted = []

    def fetch(self, responses, **environment):
        def fake_post(url, body, headers):
            self.posted.append((url, body, headers))
            return responses[len(self.posted) - 1]

        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(wif_credentials, "_post", fake_post):
                return wif_credentials.fetch_identity_token("https://broker.example/token")

    def test_it_exchanges_the_projected_token_and_then_mints_an_id_token(self):
        minted = self.fetch(
            [{"access_token": "federated-access-token"}, {"token": "an.id.token"}],
            CREDENTIAL_PROXY_WIF_CREDENTIAL_FILE=str(self.credential_file),
        )
        self.assertEqual("an.id.token", minted)

        sts_url, sts_body, _ = self.posted[0]
        self.assertEqual(wif_credentials.STS_TOKEN_URL, sts_url)
        exchange = dict(urllib.parse.parse_qsl(sts_body.decode("utf-8")))
        self.assertEqual("a.projected.jwt", exchange["subject_token"])
        self.assertEqual(AUDIENCE, exchange["audience"])
        self.assertEqual("urn:ietf:params:oauth:token-type:jwt", exchange["subject_token_type"])

        id_url, id_body, id_headers = self.posted[1]
        # generateIdToken, not generateAccessToken: the credential file's
        # impersonation URL is the access-token one and reusing it silently
        # returns the wrong kind of token.
        self.assertEqual(
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{SERVICE_ACCOUNT}:generateIdToken",
            id_url,
        )
        # Authorised by the federated token rather than the impersonated one.
        # iam.workloadIdentityUser carries getOpenIdToken, which is the binding
        # every install surface already creates; going through the service
        # account's own access token would be self-impersonation and needs a
        # tokenCreator binding on itself that nothing grants.
        self.assertEqual("Bearer federated-access-token", id_headers["Authorization"])
        request = json.loads(id_body.decode("utf-8"))
        self.assertEqual("https://broker.example/token", request["audience"])
        # Without it the token carries the numeric subject and the broker's rule,
        # which matches on assertion.email, never fires.
        self.assertTrue(request["includeEmail"])

    def test_it_declines_when_the_identity_is_not_federated(self):
        # The standalone placement, where gcloud and the metadata server are
        # still the right answer and the caller must fall through to them.
        self.assertIsNone(self.fetch([]))
        self.assertEqual([], self.posted)

    def test_a_service_account_key_is_not_mistaken_for_a_federated_one(self):
        key_file = self.root / "key.json"
        key_file.write_text(json.dumps({"type": "service_account"}), encoding="utf-8")
        self.assertIsNone(self.fetch([], GOOGLE_APPLICATION_CREDENTIALS=str(key_file)))

    def test_an_external_account_these_legs_cannot_drive_is_declined(self):
        """Not every external_account is the one this module writes.

        The test above establishes that a hand-placed credential is honoured, so
        the shapes that arrive that way have to be answered. Direct-access
        federation carries no impersonation URL, and credential_source may name
        a `url` or an `executable` rather than a `file`. Both are valid
        credentials and neither can drive the two legs here, so the answer is
        the same None an unfederated identity gets -- the caller then falls
        through to gcloud. Raising KeyError instead would take down a GitHub
        token refresh that had a working route to the metadata server.
        """
        shapes = {
            "direct access, no impersonation": {
                "type": "external_account",
                "audience": AUDIENCE,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "token_url": wif_credentials.STS_TOKEN_URL,
                "credential_source": {"file": str(self.token)},
            },
            "a url credential_source": {
                "type": "external_account",
                "audience": AUDIENCE,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "token_url": wif_credentials.STS_TOKEN_URL,
                "service_account_impersonation_url": wif_credentials.IMPERSONATION_URL.format(
                    email=SERVICE_ACCOUNT
                ),
                "credential_source": {"url": "http://169.254.169.254/token"},
            },
            "no credential_source at all": {
                "type": "external_account",
                "audience": AUDIENCE,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "token_url": wif_credentials.STS_TOKEN_URL,
                "service_account_impersonation_url": wif_credentials.IMPERSONATION_URL.format(
                    email=SERVICE_ACCOUNT
                ),
            },
        }
        for label, document in shapes.items():
            with self.subTest(shape=label):
                self.posted = []
                path = self.root / "hand-placed.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                self.assertIsNone(
                    self.fetch([], GOOGLE_APPLICATION_CREDENTIALS=str(path))
                )
                # Declined before the network, so nothing was exchanged on the
                # way to finding out.
                self.assertEqual([], self.posted)

    def test_it_reads_the_credential_the_rest_of_the_container_uses(self):
        # gcloud's override and google-auth's variable, not just the one this
        # module writes -- a caller under a hand-placed credential should see the
        # same identity every other command in the container is using.
        for variable in ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"):
            with self.subTest(variable=variable):
                self.posted = []
                minted = self.fetch(
                    [{"access_token": "federated-access-token"}, {"token": "an.id.token"}],
                    **{variable: str(self.credential_file)},
                )
                self.assertEqual("an.id.token", minted)


if __name__ == "__main__":
    unittest.main()
