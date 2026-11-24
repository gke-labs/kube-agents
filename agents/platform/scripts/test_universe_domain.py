#!/usr/bin/env python3
"""Unit tests for custom Google Cloud universe_domain runtime wiring (#5890)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api_policy
import credential_proxy
import credential_proxy_client
import gke_cluster_reads
import mcp_policy
import watcher_policy
import wif_credentials

CUSTOM_UNIVERSE = "universe.example.test"
# A variable no code reads: what the restore test sets and expects to be gone.
LEAKED_VARIABLE = "TEST_UNIVERSE_DOMAIN_LEAKED_VARIABLE"


class EnvironmentRestoredTestCase(unittest.TestCase):
    """A test case that gets the process environment back as it found it.

    Code under test here writes `os.environ`: the broker's metadata cache
    exports a probed universe domain as `GOOGLE_CLOUD_UNIVERSE_DOMAIN`, and
    the Pub/Sub adapter sets `PUBSUB_HOME_CHANNEL` when it is imported. A
    variable a test leaves behind is read by every test after it in the
    process (#6096), so each test runs on the environment it started with.
    Every test case of this module is one of these and has no `setUp` of its
    own, so this one always runs; a step a test needs goes into the test, with
    `addCleanup` for its undo.
    """

    def setUp(self) -> None:
        super().setUp()
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)


class EveryTestCaseRestoresTheEnvironmentTest(EnvironmentRestoredTestCase):
    def test_every_test_case_of_this_module_restores_the_environment(self) -> None:
        module = sys.modules[__name__]
        cases = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
            and value not in (EnvironmentRestoredTestCase, type(self))
        ]
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case.__name__):
                self.assertTrue(issubclass(case, EnvironmentRestoredTestCase))
                self.assertIs(case.setUp, EnvironmentRestoredTestCase.setUp)

    def test_a_variable_a_test_sets_is_gone_after_it(self) -> None:
        class Leaks(EnvironmentRestoredTestCase):
            def test_sets_a_variable(self) -> None:
                os.environ[LEAKED_VARIABLE] = CUSTOM_UNIVERSE

        # `os.environ.get`, not `assertNotIn`: a failure must not print the environment into the log.
        self.assertIsNone(os.environ.get(LEAKED_VARIABLE))
        result = unittest.TestResult()
        Leaks("test_sets_a_variable").run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)
        self.assertIsNone(os.environ.get(LEAKED_VARIABLE))


class ClientUniverseDomainTest(EnvironmentRestoredTestCase):
    def test_normalize_and_env_precedence(self) -> None:
        self.assertEqual(credential_proxy_client.normalize_universe_domain(""), "googleapis.com")
        self.assertEqual(
            credential_proxy_client.normalize_universe_domain("  Universe.Example.Test. "),
            CUSTOM_UNIVERSE,
        )
        for bad in (
            ".",
            "...",
            "universe.example.test..",
            "localhost",
            "https://universe.example.test",
            "bad/path",
            "-leading.example",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    credential_proxy_client.normalize_universe_domain(bad)
                with self.assertRaises(ValueError):
                    credential_proxy_client.google_universe_domain(
                        {"GOOGLE_CLOUD_UNIVERSE_DOMAIN": bad}
                    )

        env = {
            "GOOGLE_CLOUD_UNIVERSE_DOMAIN": CUSTOM_UNIVERSE,
            "CLOUDSDK_CORE_UNIVERSE_DOMAIN": "other.example.test",
        }
        self.assertEqual(credential_proxy_client.google_universe_domain(env), CUSTOM_UNIVERSE)
        self.assertEqual(
            credential_proxy_client.google_universe_domain(
                {"CLOUDSDK_CORE_UNIVERSE_DOMAIN": CUSTOM_UNIVERSE}
            ),
            CUSTOM_UNIVERSE,
        )

    def test_a_universe_domain_under_googleapis_com_is_refused(self) -> None:
        # `<service>.mtls.googleapis.com` is a host of the commercial universe,
        # so a value under googleapis.com names no other universe.
        for under in ("mtls.googleapis.com", " MTLS.GoogleAPIs.com. ", "a.b.googleapis.com"):
            with self.subTest(under=under):
                with self.assertRaises(ValueError):
                    credential_proxy_client.normalize_universe_domain(under)
                with self.assertRaises(ValueError):
                    credential_proxy_client.google_universe_domain(
                        {"GOOGLE_CLOUD_UNIVERSE_DOMAIN": under}
                    )
                # What the broker's `serve` calls before it starts a listener.
                with self.assertRaises(ValueError):
                    credential_proxy.resolve_universe_domain(
                        environ={"GOOGLE_CLOUD_UNIVERSE_DOMAIN": under}
                    )
        # The issue's request: the relay has no routed host in such a value.
        decision = api_policy.evaluate(
            "GET", "monitoring.mtls.googleapis.com", "v3/projects/sample-project/timeSeries", "",
            universe_domain="mtls.googleapis.com",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule_id, api_policy.RULE_HOST)
        # The commercial domain itself, and domains that only look like it, are as before.
        for accepted, normalized in (
            ("googleapis.com", "googleapis.com"),
            (" GoogleAPIs.com. ", "googleapis.com"),
            ("notgoogleapis.com", "notgoogleapis.com"),
            ("googleapis.com.example", "googleapis.com.example"),
            (CUSTOM_UNIVERSE, CUSTOM_UNIVERSE),
        ):
            with self.subTest(accepted=accepted):
                self.assertEqual(normalized, credential_proxy_client.normalize_universe_domain(accepted))

    def test_the_broker_does_not_start_with_a_universe_domain_under_googleapis_com(self) -> None:
        # The real entry point in a new interpreter, as the pod starts it: the
        # value is refused before a listener exists.
        with tempfile.TemporaryDirectory() as temp:
            policy = Path(temp) / "policy.json"
            policy.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
            listener = Path(temp) / "broker.sock"
            environment = {
                name: value for name, value in os.environ.items() if not name.startswith("CREDENTIAL_PROXY_")
            }
            environment["GOOGLE_CLOUD_UNIVERSE_DOMAIN"] = "mtls.googleapis.com"
            completed = subprocess.run(
                [sys.executable, credential_proxy.__file__, "--policy", str(policy), "--unix-socket", str(listener)],
                env=environment, capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertFalse(listener.exists())
        self.assertEqual(credential_proxy.EXIT_STARTUP_FAILURE, completed.returncode, completed.stderr)
        last = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual("credential proxy failed to start type=ValueError", last["message"])
        self.assertIn("mtls.googleapis.com", last["exception"])

    def test_api_session_rewrites_googleapis_host_in_custom_universe(self) -> None:
        fake_http = mock.MagicMock()
        with mock.patch.dict(
            os.environ,
            {
                "CREDENTIAL_PROXY_URL": "http://broker.local:8639",
                "GOOGLE_CLOUD_UNIVERSE_DOMAIN": CUSTOM_UNIVERSE,
            },
            clear=False,
        ):
            session = credential_proxy_client.ApiSession(http=fake_http)
            relayed = session.relay_url(
                "https://monitoring.googleapis.com/v3/projects/sample-project/timeSeries?pageSize=10"
            )
            self.assertEqual(
                relayed,
                f"http://broker.local:8639/v1/gcp/monitoring.{CUSTOM_UNIVERSE}"
                "/v3/projects/sample-project/timeSeries?pageSize=10",
            )
            self.assertEqual(
                gke_cluster_reads._gke_api(),
                f"https://container.{CUSTOM_UNIVERSE}/v1",
            )

    def test_api_session_keeps_googleapis_host_by_default(self) -> None:
        fake_http = mock.MagicMock()
        with mock.patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://broker.local:8639"},
            clear=True,
        ):
            session = credential_proxy_client.ApiSession(http=fake_http)
            relayed = session.relay_url(
                "https://monitoring.googleapis.com/v3/projects/sample-project/timeSeries"
            )
            self.assertEqual(
                relayed,
                "http://broker.local:8639/v1/gcp/monitoring.googleapis.com"
                "/v3/projects/sample-project/timeSeries",
            )


class PolicyUniverseIsolationTest(EnvironmentRestoredTestCase):
    def test_api_policy_admits_custom_universe_and_refuses_commercial_googleapis(self) -> None:
        path = "v3/projects/sample-project/timeSeries"
        allowed = api_policy.evaluate(
            "GET", f"monitoring.{CUSTOM_UNIVERSE}", path, "", universe_domain=CUSTOM_UNIVERSE
        )
        self.assertTrue(allowed.allowed, allowed.message)
        self.assertEqual(allowed.rule_id, "gcp.api.monitoring.timeseries-list")

        leaked = api_policy.evaluate(
            "GET", "monitoring.googleapis.com", path, "", universe_domain=CUSTOM_UNIVERSE
        )
        self.assertFalse(leaked.allowed)
        self.assertEqual(leaked.rule_id, api_policy.RULE_HOST)

        for refused_host in (f"sts.{CUSTOM_UNIVERSE}", "sts.googleapis.com", f"iam.{CUSTOM_UNIVERSE}"):
            with self.subTest(host=refused_host):
                decision = api_policy.evaluate(
                    "GET", refused_host, "v1/token", "", universe_domain=CUSTOM_UNIVERSE
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.rule_id, api_policy.RULE_HOST_REFUSED)

    def test_mcp_policy_resolves_server_host_in_custom_universe(self) -> None:
        gke = mcp_policy.server("gke", universe_domain=CUSTOM_UNIVERSE)
        self.assertIsNotNone(gke)
        assert gke is not None
        self.assertEqual(gke.host, f"container.{CUSTOM_UNIVERSE}")
        default_gke = mcp_policy.server("gke")
        assert default_gke is not None
        self.assertEqual(default_gke.host, "container.googleapis.com")

    def test_watcher_and_pubsub_policies_refuse_cross_universe_hosts(self) -> None:
        cluster_path = "v1/projects/sample-project/locations/us-central1/clusters/host-cluster"
        sub = "projects/sample-project/subscriptions/drift-sub"
        sub_path = f"v1/{sub}:pull"
        pull_body = b'{"maxMessages":1}'

        ok_gke = watcher_policy.evaluate_gcp(
            "GET", f"container.{CUSTOM_UNIVERSE}", cluster_path, b"", sub, universe_domain=CUSTOM_UNIVERSE
        )
        self.assertTrue(ok_gke.allowed)
        refused_gke = watcher_policy.evaluate_gcp(
            "GET", "container.googleapis.com", cluster_path, b"", sub, universe_domain=CUSTOM_UNIVERSE
        )
        self.assertFalse(refused_gke.allowed)
        self.assertEqual(refused_gke.rule_id, watcher_policy.RULE_GCP_ROUTE)

        ok_pubsub = watcher_policy.evaluate_pubsub(
            "POST", f"pubsub.{CUSTOM_UNIVERSE}", sub_path, pull_body, frozenset({sub}), universe_domain=CUSTOM_UNIVERSE
        )
        self.assertTrue(ok_pubsub.allowed)
        refused_pubsub = watcher_policy.evaluate_pubsub(
            "POST", "pubsub.googleapis.com", sub_path, pull_body, frozenset({sub}), universe_domain=CUSTOM_UNIVERSE
        )
        self.assertFalse(refused_pubsub.allowed)
        self.assertEqual(refused_pubsub.rule_id, watcher_policy.RULE_PUBSUB_ROUTE)


class WifAndExecutorUniverseTest(EnvironmentRestoredTestCase):
    def test_wif_document_and_id_token_use_custom_universe(self) -> None:
        aud = f"//iam.{CUSTOM_UNIVERSE}/projects/12345/locations/global/workloadIdentityPools/p/providers/pr"
        sa = "kubeagents@sample-project.iam.gserviceaccount.com"
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            cred_path = Path(tmp) / "wif.json"
            token_path.write_text("projected-jwt\n", encoding="utf-8")
            doc = wif_credentials.build_document(
                aud, sa, str(token_path), universe_domain=CUSTOM_UNIVERSE
            )
            self.assertEqual(doc["universe_domain"], CUSTOM_UNIVERSE)
            self.assertEqual(doc["token_url"], f"https://sts.{CUSTOM_UNIVERSE}/v1/token")
            self.assertEqual(
                doc["service_account_impersonation_url"],
                f"https://iamcredentials.{CUSTOM_UNIVERSE}/v1/projects/-/serviceAccounts/{sa}:generateAccessToken",
            )
            cred_path.write_text(json.dumps(doc), encoding="utf-8")

            posted_urls: list[str] = []

            def fake_post(url: str, body: bytes, headers: dict) -> dict:
                del body, headers
                posted_urls.append(url)
                if url.endswith("/v1/token"):
                    return {"access_token": "fed-tok"}
                return {"token": "id-tok"}

            with mock.patch.object(wif_credentials, "_post", side_effect=fake_post):
                minted = wif_credentials.fetch_identity_token("https://minter.example", str(cred_path))
            self.assertEqual(minted, "id-tok")
            self.assertEqual(
                posted_urls,
                [
                    f"https://sts.{CUSTOM_UNIVERSE}/v1/token",
                    f"https://iamcredentials.{CUSTOM_UNIVERSE}/v1/projects/-/serviceAccounts/{sa}:generateIdToken",
                ],
            )

    def test_command_executor_sets_universe_and_disables_mtls_client_cert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ex_default = credential_proxy.CommandExecutor(
                timeout_seconds=5,
                max_output_bytes=1024,
                state_dir=str(Path(tmp) / "default"),
                scoped_pool=None,
                universe_domain="googleapis.com",
            )
            self.assertEqual(
                ex_default.environment.get("CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"),
                "false",
            )
            self.assertNotIn("GOOGLE_CLOUD_UNIVERSE_DOMAIN", ex_default.environment)
            self.assertNotIn("CLOUDSDK_CORE_UNIVERSE_DOMAIN", ex_default.environment)

            ex_custom = credential_proxy.CommandExecutor(
                timeout_seconds=5,
                max_output_bytes=1024,
                state_dir=str(Path(tmp) / "custom"),
                scoped_pool=None,
                universe_domain=CUSTOM_UNIVERSE,
            )
            self.assertEqual(
                ex_custom.environment.get("CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"),
                "false",
            )
            self.assertEqual(
                ex_custom.environment.get("GOOGLE_CLOUD_UNIVERSE_DOMAIN"), CUSTOM_UNIVERSE
            )
            self.assertEqual(
                ex_custom.environment.get("CLOUDSDK_CORE_UNIVERSE_DOMAIN"), CUSTOM_UNIVERSE
            )

    def test_sovereign_region_and_partitioned_project_routes_in_api_and_mcp_policy(self) -> None:
        decision = api_policy.evaluate(
            "GET",
            f"container.{CUSTOM_UNIVERSE}",
            "v1/projects/part0:sample-project/locations/u-region-north1/clusters/prod-cluster",
            "",
            universe_domain=CUSTOM_UNIVERSE,
        )
        self.assertTrue(decision.allowed, decision.message)
        bad_universe = api_policy.evaluate(
            "GET",
            f"container.{CUSTOM_UNIVERSE}",
            "v1/projects/sample-project/locations/us-central1/clusters/prod",
            "",
            universe_domain="https://not-a-hostname",
        )
        self.assertFalse(bad_universe.allowed)

        clusters = mcp_policy.k8s_tool_clusters(
            "gke",
            {
                "method": "tools/call",
                "params": {
                    "name": "list_k8s_events",
                    "arguments": {
                        "parent": "projects/part0:sample-project/locations/u-region-north1/clusters/prod-cluster"
                    },
                },
            },
        )
        self.assertEqual(clusters, [("part0:sample-project", "u-region-north1", "prod-cluster")])

    def test_transient_metadata_probe_is_not_cached_while_404_and_200_are(self) -> None:
        credential_proxy._CACHED_METADATA_UNIVERSE_DOMAIN = None
        # Also when an assertion below fails: the cache must not keep the fake universe for later tests.
        self.addCleanup(setattr, credential_proxy, "_CACHED_METADATA_UNIVERSE_DOMAIN", None)
        responses = [
            OSError("connection timed out"),
            (CUSTOM_UNIVERSE, True),
        ]

        def fake_probe() -> tuple[str, bool]:
            item = responses.pop(0)
            if isinstance(item, Exception):
                return credential_proxy.DEFAULT_UNIVERSE_DOMAIN if hasattr(credential_proxy, "DEFAULT_UNIVERSE_DOMAIN") else "googleapis.com", False
            return item

        with mock.patch.object(credential_proxy, "probe_gce_universe_domain_status", side_effect=fake_probe):
            first = credential_proxy.resolve_universe_domain(environ={}, probe_metadata=True)
            self.assertEqual(first, "googleapis.com")
            self.assertIsNone(credential_proxy._CACHED_METADATA_UNIVERSE_DOMAIN)

            second = credential_proxy.resolve_universe_domain(environ={}, probe_metadata=True)
            self.assertEqual(second, CUSTOM_UNIVERSE)
            self.assertEqual(credential_proxy._CACHED_METADATA_UNIVERSE_DOMAIN, CUSTOM_UNIVERSE)
        credential_proxy._CACHED_METADATA_UNIVERSE_DOMAIN = None

        # Empty, whitespace-only, and dot-only HTTP 200 bodies return determined=False and are not cached.
        class _FakeHTTP200:
            def __init__(self, payload: bytes) -> None:
                self.status = 200
                self._payload = payload

            def read(self, _n: int = -1) -> bytes:
                return self._payload

            def __enter__(self) -> _FakeHTTP200:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        class _FakeOpener:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def open(self, *_a: object, **_kw: object) -> _FakeHTTP200:
                return _FakeHTTP200(self._payload)

        for body in (b"", b"   \n", b".", b"..."):
            with self.subTest(body=body):
                dom, determined = credential_proxy.probe_gce_universe_domain_status(
                    opener=_FakeOpener(body)
                )
                self.assertEqual(dom, "googleapis.com")
                self.assertFalse(determined)

    def test_pubsub_platform_adapter_uses_universe_domain(self) -> None:
        import importlib.util
        import sys
        import types

        repo_root = Path(__file__).resolve().parents[3]
        adapter_path = repo_root / "agentplugins" / "pubsub-platform" / "files" / "platforms" / "pubsub" / "adapter.py"
        gw_pkg = types.ModuleType("gateway")
        gw_platforms = types.ModuleType("gateway.platforms")
        gw_base = types.ModuleType("gateway.platforms.base")
        for attr in ("BasePlatformAdapter", "SendResult", "MessageEvent", "MessageType", "ProcessingOutcome"):
            setattr(gw_base, attr, object)
        gw_cfg = types.ModuleType("gateway.config")
        gw_cfg.Platform = object
        gw_cfg.PlatformConfig = object
        gw_sess = types.ModuleType("gateway.session")
        gw_sess.build_session_key = lambda *a, **kw: ""
        saved = {k: sys.modules.get(k) for k in ("gateway", "gateway.platforms", "gateway.platforms.base", "gateway.config", "gateway.session")}
        try:
            sys.modules.update({
                "gateway": gw_pkg,
                "gateway.platforms": gw_platforms,
                "gateway.platforms.base": gw_base,
                "gateway.config": gw_cfg,
                "gateway.session": gw_sess,
            })
            spec = importlib.util.spec_from_file_location("pubsub_adapter_test", adapter_path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            relay = module.PubsubRelay("http://broker.local:8666", "")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    relay._url("projects/p/subscriptions/s", ":pull"),
                    "http://broker.local:8666/v1/pubsub/gcp/pubsub.googleapis.com/v1/projects/p/subscriptions/s:pull",
                )
            with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_UNIVERSE_DOMAIN": CUSTOM_UNIVERSE}, clear=True):
                self.assertEqual(
                    relay._url("projects/p/subscriptions/s", ":pull"),
                    f"http://broker.local:8666/v1/pubsub/gcp/pubsub.{CUSTOM_UNIVERSE}/v1/projects/p/subscriptions/s:pull",
                )
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v



class BrokerStartUniverseCheckTest(EnvironmentRestoredTestCase):
    """At start the broker compares a universe domain the environment names with
    the metadata server's answer, and does not start when the two differ."""

    def check(self, configured, answers):
        """Run the check with the metadata server giving `answers` in turn; return
        (the probe calls made, the pauses slept)."""
        pending = list(answers)
        calls, pauses = [], []

        def probe(*_args, **_kwargs):
            calls.append(1)
            return pending.pop(0)

        with mock.patch.object(credential_proxy, "probe_gce_universe_domain_answer", side_effect=probe):
            credential_proxy.check_universe_domain_against_metadata(configured, sleep=pauses.append)
        return len(calls), pauses

    def test_a_value_the_metadata_server_confirms_starts(self) -> None:
        with self.assertNoLogs(credential_proxy.LOGGER, level="WARNING"):
            self.assertEqual((1, []), self.check(CUSTOM_UNIVERSE, [(CUSTOM_UNIVERSE, True)]))
            self.assertEqual((1, []), self.check("googleapis.com", [("googleapis.com", True)]))

    def test_a_value_the_metadata_server_contradicts_does_not_start(self) -> None:
        cases = (
            # A commercial host name that is not under googleapis.com, on a commercial host.
            ("clients6.google.com", "googleapis.com"),
            # A custom universe on a commercial host, and the other direction.
            (CUSTOM_UNIVERSE, "googleapis.com"),
            ("googleapis.com", CUSTOM_UNIVERSE),
        )
        for configured, reported in cases:
            with self.subTest(configured=configured, reported=reported):
                with self.assertRaises(RuntimeError) as refused:
                    self.check(configured, [(reported, True)])
                message = str(refused.exception)
                self.assertIn(repr(configured), message)
                self.assertIn(repr(reported), message)
                self.assertIn("GOOGLE_CLOUD_UNIVERSE_DOMAIN", message)

    def test_a_host_with_no_universe_route_starts_and_says_the_value_was_not_checked(self) -> None:
        # A 404: nothing to contradict the value, as in the operator's check.
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            self.assertEqual((1, []), self.check(CUSTOM_UNIVERSE, [(None, True)]))
        self.assertEqual(1, len(logs.records))
        self.assertIn(CUSTOM_UNIVERSE, logs.records[0].getMessage())
        self.assertIn("not checked", logs.records[0].getMessage())

    def test_no_answer_is_tried_again_then_starts_with_one_warning(self) -> None:
        attempts = credential_proxy.UNIVERSE_CHECK_ATTEMPTS
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            calls, pauses = self.check(CUSTOM_UNIVERSE, [(None, False)] * attempts)
        self.assertEqual(attempts, calls)
        self.assertEqual([credential_proxy.UNIVERSE_CHECK_PAUSE_SECONDS] * (attempts - 1), pauses)
        self.assertEqual(1, len(logs.records))
        self.assertIn(CUSTOM_UNIVERSE, logs.records[0].getMessage())
        self.assertIn("not checked", logs.records[0].getMessage())

    def test_an_answer_after_a_fault_still_decides(self) -> None:
        with self.assertRaises(RuntimeError):
            self.check(CUSTOM_UNIVERSE, [(None, False), ("googleapis.com", True)])
        self.assertEqual((2, [credential_proxy.UNIVERSE_CHECK_PAUSE_SECONDS]),
                         self.check(CUSTOM_UNIVERSE, [(None, False), (CUSTOM_UNIVERSE, True)]))

    def test_the_probe_tells_an_answer_from_no_route_and_from_no_answer(self) -> None:
        class _Response:
            def __init__(self, status, payload=b""):
                self.status, self._payload = status, payload

            def read(self, _n=-1):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class _Opener:
            def __init__(self, outcome):
                self._outcome = outcome

            def open(self, *_args, **_kwargs):
                if isinstance(self._outcome, Exception):
                    raise self._outcome
                return self._outcome

        cases = (
            (_Response(200, b" Universe.Example.Test. \n"), (CUSTOM_UNIVERSE, True)),
            (_Response(200, b"googleapis.com"), ("googleapis.com", True)),
            (_Response(404), (None, True)),
            (_Response(503), (None, False)),
            (_Response(200, b""), (None, False)),
            (_Response(200, b"not a domain!"), (None, False)),
            (OSError("connection timed out"), (None, False)),
        )
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, credential_proxy.probe_gce_universe_domain_answer(opener=_Opener(outcome)))
        # The older probe keeps its answers: a 404 and no answer both read as the commercial domain.
        self.assertEqual(("googleapis.com", True), credential_proxy.probe_gce_universe_domain_status(opener=_Opener(_Response(404))))
        self.assertEqual(("googleapis.com", False), credential_proxy.probe_gce_universe_domain_status(opener=_Opener(_Response(503))))

    def test_the_broker_does_not_start_when_the_metadata_server_contradicts_the_variable(self) -> None:
        # The real entry point in a new interpreter. Only the metadata server's
        # answer is a stand-in: the commercial domain.
        launcher = (
            "import sys, credential_proxy\n"
            "credential_proxy.probe_gce_universe_domain_answer = lambda *a, **k: ('googleapis.com', True)\n"
            "sys.exit(credential_proxy.main())\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            policy = Path(temp) / "policy.json"
            policy.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
            listener = Path(temp) / "broker.sock"
            # Only what the start needs: the runner's own variables (a log level,
            # relay settings) must not reach a broker, least of all one that a
            # regression lets start.
            environment = {"PATH": os.environ.get("PATH", ""), "GOOGLE_CLOUD_UNIVERSE_DOMAIN": "clients6.google.com"}
            completed = subprocess.run(
                [sys.executable, "-c", launcher, "--policy", str(policy), "--unix-socket", str(listener),
                 "--state-dir", str(Path(temp) / "state")],
                env=environment, cwd=str(Path(credential_proxy.__file__).parent),
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertFalse(listener.exists())
        self.assertEqual(credential_proxy.EXIT_STARTUP_FAILURE, completed.returncode, completed.stderr)
        last = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual("credential proxy failed to start type=RuntimeError", last["message"])
        self.assertIn("clients6.google.com", last["exception"])
        self.assertIn("googleapis.com", last["exception"])


if __name__ == "__main__":
    unittest.main()
