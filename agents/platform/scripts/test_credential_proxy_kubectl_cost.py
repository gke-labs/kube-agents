"""The broker's kubectl cost gate: a cluster-wide read reserves its profiled peak.

The classifier is the shell sandbox's own (deploy/sandbox/kubectl-admit.py),
loaded by path as the broker image loads it; the table is a small synthetic
one, so these tests do not move when the real table is re-profiled.
"""

import contextlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import credential_proxy
import no_live_kubectl
from credential_proxy import (
    MEBIBYTE,
    REQUEST_CHILD_MEMORY_RESERVE_BYTES,
    CommandExecutor,
    CredentialProxyHandler,
    KubectlCostGate,
    Policy,
)

ADMIT_PATH = Path(__file__).resolve().parents[3] / "deploy" / "sandbox" / "kubectl-admit.py"
FLAT = REQUEST_CHILD_MEMORY_RESERVE_BYTES
# What the synthetic budget gives children: the broker's resident reserve is
# taken off the limit before anything is charged.
CHILDREN_BUDGET = 1024 * MEBIBYTE
TABLE = {
    "tiers": {
        "default": {
            "commands": {
                "get pods cluster json": {"peak_mib": 300},
                "get nodes cluster table": {"peak_mib": 50},
                "get events cluster json": {"peak_mib": 1000},
            },
            "other": {"peak_mib": 40},
        },
        "fleet": {
            "commands": {"get pods cluster json": {"peak_mib": 2000}},
            "other": {"peak_mib": 400},
        },
    },
}


def setUpModule():
    global _NO_KUBECTL
    _NO_KUBECTL = no_live_kubectl.install()


def tearDownModule():
    _NO_KUBECTL.uninstall()


class _GateFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.table_path = Path(self.temp_dir.name) / "kubectl-costs.json"
        self.table_path.write_text(json.dumps(TABLE), encoding="utf-8")

    def gate(self, tier="default", table_path=None, admit_path=None):
        return KubectlCostGate(
            admit_path=str(admit_path or ADMIT_PATH),
            table_path=str(table_path or self.table_path),
            tier=tier,
        )

    def executor(self, budget=True, tier="default", **gate_args):
        executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=1024,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
            memory_limit_bytes=(credential_proxy.BROKER_RESIDENT_RESERVE_BYTES + CHILDREN_BUDGET) if budget else None,
        )
        executor.kubectl_cost_gate = self.gate(tier=tier, **gate_args)
        return executor


class KubectlCostGateTest(_GateFixture):
    def test_a_cluster_wide_get_is_charged_its_table_figure(self):
        charge = self.gate().charge(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(300 * MEBIBYTE, charge.child_bytes)
        self.assertEqual("get pods cluster json", charge.key)

    def test_a_charge_under_the_flat_reserve_keeps_the_flat_reserve(self):
        charge = self.gate().charge(["kubectl", "get", "nodes"])
        self.assertEqual(FLAT, charge.child_bytes)
        self.assertEqual(50 * MEBIBYTE, charge.table_bytes)

    def test_namespaced_named_and_other_verbs_are_not_charged(self):
        gate = self.gate()
        for argv in (
            ["kubectl", "get", "pods", "-n", "team", "-o", "json"],
            ["kubectl", "get", "pods", "web-0", "-A", "-o", "json"],
            ["kubectl", "get", "pod/web-0", "-o", "json"],
            ["kubectl", "logs", "web-0", "-n", "team"],
            ["kubectl", "top", "nodes"],
            ["kubectl", "get", "pods", "-A", "-w"],
            ["kubectl", "get", "--raw", "/api/v1/pods"],
            ["kubectl", "get", "-f", "pods.yaml"],
            ["kubectl", "get", "pods", "-A", "--help"],
            ["gcloud", "container", "clusters", "list"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(gate.charge(argv))

    def test_a_selector_read_is_never_charged(self):
        gate = self.gate()
        for argv in (
            ["kubectl", "get", "pods", "-A", "-o", "json", "-l", "app=web"],
            ["kubectl", "get", "pods", "-A", "-o", "json", "--field-selector", "spec.nodeName=n1"],
            ["kubectl", "get", "pods", "-A", "-ojson", "-lapp=web"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(gate.charge(argv))

    def test_an_unknown_tier_charges_the_default_column(self):
        charge = self.gate(tier="nope").charge(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(300 * MEBIBYTE, charge.child_bytes)

    def test_a_listed_tier_charges_its_own_column(self):
        charge = self.gate(tier="fleet").charge(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(2000 * MEBIBYTE, charge.child_bytes)

    def test_the_tier_is_held_to_the_entrypoints_pattern(self):
        tier = credential_proxy.kubectl_cost_tier
        self.assertEqual("default", tier({}))
        self.assertEqual("fleet", tier({"KUBEAGENTS_KUBECTL_COST_TIER": "fleet"}))
        self.assertEqual("large-2", tier({"KUBEAGENTS_KUBECTL_COST_TIER": "large-2"}))
        for value in ("", "Fleet", "../etc", "fleet tier", "9fleet", "fleet\n"):
            with self.subTest(value=value):
                self.assertEqual("default", tier({"KUBEAGENTS_KUBECTL_COST_TIER": value}))

    def test_a_missing_table_falls_back_to_flat_and_logs_once(self):
        gate = self.gate(table_path=Path(self.temp_dir.name) / "absent.json")
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            self.assertIsNone(gate.charge(["kubectl", "get", "pods", "-A", "-o", "json"]))
            self.assertIsNone(gate.charge(["kubectl", "get", "pods", "-A", "-o", "json"]))
        self.assertEqual(1, sum("kubectl cost gate disabled" in line for line in logs.output))

    def test_a_corrupt_table_falls_back_to_flat(self):
        for body in ("{not json", json.dumps(["a list"]), json.dumps({"tiers": "none"})):
            with self.subTest(body=body):
                self.table_path.write_text(body, encoding="utf-8")
                with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                    self.assertIsNone(self.gate().charge(["kubectl", "get", "pods", "-A", "-o", "json"]))

    def test_a_missing_classifier_falls_back_to_flat(self):
        gate = self.gate(admit_path=Path(self.temp_dir.name) / "absent.py")
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            self.assertIsNone(gate.charge(["kubectl", "get", "pods", "-A", "-o", "json"]))

    def test_a_table_entry_of_the_wrong_shape_falls_back_to_flat(self):
        self.table_path.write_text(
            json.dumps({"tiers": {"default": {"commands": {"get pods cluster json": {"peak_mib": "lots"}}}}}),
            encoding="utf-8",
        )
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            self.assertIsNone(self.gate().charge(["kubectl", "get", "pods", "-A", "-o", "json"]))
        self.assertTrue(any("could not charge" in line for line in logs.output))

    def test_an_exception_in_the_classifier_falls_back_to_flat_and_logs_once(self):
        gate = self.gate()
        module, _table = gate._load()
        with (
            mock.patch.object(module, "cost", side_effect=RuntimeError("boom")),
            self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs,
        ):
            self.assertIsNone(gate.charge(["kubectl", "get", "pods", "-A", "-o", "json"]))
            self.assertIsNone(gate.charge(["kubectl", "get", "pods", "-A", "-o", "json"]))
        self.assertEqual(1, sum("could not charge" in line for line in logs.output))
        # The argv is never logged.
        self.assertFalse(any("pods" in line for line in logs.output))


class KubectlChargeAdmissionTest(_GateFixture):
    def test_a_cluster_wide_read_reserves_its_charge_and_releases_it(self):
        executor = self.executor()
        charge = executor.kubectl_charge(["kubectl", "get", "pods", "-A", "-o", "json"])
        with self.assertLogs(credential_proxy.LOGGER, level="DEBUG") as logs:
            with executor.request_slot(charge=charge):
                self.assertEqual(300 * MEBIBYTE, executor.reserved_bytes)
        self.assertEqual(0, executor.reserved_bytes)
        self.assertTrue(any("kubectl charged 300 MiB" in line and "get pods cluster json" in line for line in logs.output))

    def test_uncharged_commands_reserve_the_flat_figure(self):
        executor = self.executor()
        for argv in (
            ["kubectl", "get", "pods", "-n", "team", "-o", "json"],
            ["kubectl", "get", "pods", "web-0", "-n", "team"],
            ["kubectl", "logs", "web-0"],
            ["kubectl", "get", "pods", "-A", "-o", "json", "-l", "app=web"],
        ):
            with self.subTest(argv=argv):
                charge = executor.kubectl_charge(argv)
                self.assertIsNone(charge)
                with executor.request_slot(charge=charge):
                    self.assertEqual(FLAT, executor.reserved_bytes)
                self.assertEqual(0, executor.reserved_bytes)

    def test_the_sizing_math_stays_on_the_flat_figure(self):
        executor = self.executor()
        flat = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=1024,
            state_dir=str(Path(self.temp_dir.name) / "flat"),
            scoped_pool=None,
            memory_limit_bytes=credential_proxy.BROKER_RESIDENT_RESERVE_BYTES + CHILDREN_BUDGET,
        )
        executor.kubectl_charge(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(flat.requests_the_budget_admits(), executor.requests_the_budget_admits())
        self.assertEqual(flat.admitted_requests(), executor.admitted_requests())

    def test_a_never_fitting_read_is_refused_at_once_with_nothing_admitted(self):
        executor = self.executor(tier="fleet")
        charge = executor.kubectl_charge(["kubectl", "get", "pods", "-A", "-o", "json"])
        started = time.monotonic()
        with self.assertRaises(credential_proxy.KubectlCapacityExceeded) as raised:
            with executor.request_slot(charge=charge):
                self.fail("admitted a read larger than the whole budget")
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(str(raised.exception).startswith("capacity: "))
        self.assertEqual("get pods cluster json", raised.exception.key)
        self.assertEqual(0, executor.reserved_bytes)
        self.assertEqual(0, executor.slots_in_use)
        self.assertEqual(0, executor.queued_requests)

    def test_a_read_that_does_not_fit_now_waits_and_is_refused_naming_its_charge(self):
        executor = self.executor()
        release = threading.Event()
        held = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        charge = executor.kubectl_charge(["kubectl", "get", "events", "-A", "-o", "json"])
        self.assertEqual(1000 * MEBIBYTE, charge.child_bytes)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot(charge=charge):
                    self.fail("admitted past the budget")
        self.assertIn("needs 1000 MiB for its kubectl", str(raised.exception))
        self.assertEqual(FLAT, executor.reserved_bytes)
        release.set()
        thread.join()
        with executor.request_slot(charge=charge):
            self.assertEqual(1000 * MEBIBYTE, executor.reserved_bytes)

    def test_budget_off_charges_nothing_and_never_loads_the_gate(self):
        executor = self.executor(budget=False, tier="fleet")
        self.assertIsNone(executor.kubectl_charge(["kubectl", "get", "pods", "-A", "-o", "json"]))
        self.assertFalse(executor.kubectl_cost_gate._loaded)
        with executor.request_slot():
            self.assertEqual(0, executor.reserved_bytes)


class ExecRouteKubectlCostTest(_GateFixture):
    """The gate as the agent meets it: over /v1/exec."""

    def setUp(self):
        super().setUp()
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        self.marker = Path(self.temp_dir.name) / "spawned"
        stub_dir = Path(self.temp_dir.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text(f"#!/bin/bash\ntouch '{self.marker}'\necho pods\n", encoding="utf-8")
        stub.chmod(0o755)
        self.stub = stub
        self.saved = {
            name: CredentialProxyHandler.__dict__.get(name) for name in ("policy", "executor", "max_request_bytes")
        }
        self.addCleanup(self.restore)
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.max_request_bytes = 65536
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def restore(self):
        for name, value in self.saved.items():
            setattr(CredentialProxyHandler, name, value)

    def install(self, executor):
        executor.executables["kubectl"] = str(self.stub)
        CredentialProxyHandler.executor = executor
        return executor

    def post(self, argv):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/exec",
            data=json.dumps({"argv": argv}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    @contextlib.contextmanager
    def reservations_seen(self, executor):
        seen = []
        original = executor.execute

        def recording(*args, **kwargs):
            seen.append(executor.reserved_bytes)
            return original(*args, **kwargs)

        with mock.patch.object(executor, "execute", recording):
            yield seen

    def wait_released(self, executor):
        deadline = time.monotonic() + 5
        while (executor.slots_in_use or executor.reserved_bytes) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(0, executor.reserved_bytes)
        self.assertEqual(0, executor.slots_in_use)

    def test_a_cluster_wide_read_runs_under_its_charge(self):
        executor = self.install(self.executor())
        with self.reservations_seen(executor) as seen:
            status, body = self.post(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(200, status, body)
        self.assertEqual([300 * MEBIBYTE], seen)
        self.wait_released(executor)

    def test_a_namespaced_read_runs_under_the_flat_reserve(self):
        executor = self.install(self.executor())
        with self.reservations_seen(executor) as seen:
            status, body = self.post(["kubectl", "get", "pods", "-n", "team", "-o", "json"])
        self.assertEqual(200, status, body)
        self.assertEqual([FLAT], seen)

    def test_a_never_fitting_read_is_refused_with_the_capacity_text_and_nothing_spawned(self):
        executor = self.install(self.executor(tier="fleet"))
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, body = self.post(["kubectl", "get", "pods", "-A", "-o", "json", "--context", "gke_p_l_secret-name"])
        self.assertEqual(422, status)
        self.assertEqual("refused", body["status"])
        self.assertEqual(credential_proxy.KUBECTL_CAPACITY_CODE, body["code"])
        self.assertEqual("kubernetes.capacity", body["rule"])
        self.assertTrue(body["error"].startswith("capacity: "), body["error"])
        self.assertIn("2000 MiB", body["error"])
        self.assertIn("kubectl get --raw '/api/v1/pods?limit=1'", body["error"])
        self.assertIn("kubectl get pods -n <namespace>", body["error"])
        self.assertFalse(self.marker.exists(), "kubectl was spawned for a refused read")
        self.assertEqual(0, executor.reserved_bytes)
        self.assertEqual(0, executor.slots_in_use)
        refusal = [line for line in logs.output if "refused for size" in line]
        self.assertEqual(1, len(refusal), logs.output)
        self.assertIn("key=get pods cluster json", refusal[0])
        self.assertFalse(any("secret-name" in line for line in refusal))

    def test_the_shim_reports_the_refusal_as_a_final_capacity_failure(self):
        import credential_proxy_client

        self.install(self.executor(tier="fleet"))
        status, body = self.post(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertFalse(credential_proxy_client.exec_refusal_retryable(status, {}, body))
        error = urllib.error.HTTPError("http://broker/v1/exec", status, "refused", {}, None)
        with mock.patch("sys.stderr") as stderr:
            code = credential_proxy_client._exec_refused(error, body)
        self.assertNotEqual(0, code)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertTrue(written.startswith("capacity: "), written)

    def test_a_missing_table_runs_the_command_under_the_flat_reserve(self):
        executor = self.install(self.executor(table_path=Path(self.temp_dir.name) / "absent.json"))
        with self.reservations_seen(executor) as seen, self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, body = self.post(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(200, status, body)
        self.assertEqual(0, body["exitCode"])
        self.assertEqual([FLAT], seen)
        self.assertTrue(self.marker.exists())

    def test_a_classifier_exception_runs_the_command_under_the_flat_reserve(self):
        executor = self.install(self.executor())
        module, _table = executor.kubectl_cost_gate._load()
        with (
            mock.patch.object(module, "kinds_of", side_effect=KeyError("boom")),
            self.reservations_seen(executor) as seen,
            self.assertLogs(credential_proxy.LOGGER, level="WARNING"),
        ):
            status, body = self.post(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(200, status, body)
        self.assertEqual([FLAT], seen)

    def test_budget_off_runs_a_large_read_with_nothing_reserved(self):
        executor = self.install(self.executor(budget=False, tier="fleet"))
        with self.reservations_seen(executor) as seen:
            status, body = self.post(["kubectl", "get", "pods", "-A", "-o", "json"])
        self.assertEqual(200, status, body)
        self.assertEqual([0], seen)


if __name__ == "__main__":
    unittest.main()
