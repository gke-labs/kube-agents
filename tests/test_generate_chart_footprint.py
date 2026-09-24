"""Tests for scripts/generate_chart_footprint.py.

    python3 -m unittest discover -s tests -p 'test_generate_chart_footprint.py'

The generator is the only source for the operator-rendered half of the quota preflight's
arithmetic, so a parser of its that silently returns 0 puts a green render in front of a
quota that cannot fit the release.

These tests import the generator module directly, so the parsers, the formatters,
and every exit path of `main()` are reached and instrumented under coverage.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE_PATH = REPO_ROOT / "scripts" / "generate_chart_footprint.py"
_spec = importlib.util.spec_from_file_location("generate_chart_footprint", _MODULE_PATH)
gcf = importlib.util.module_from_spec(_spec)
sys.modules["generate_chart_footprint"] = gcf
_spec.loader.exec_module(gcf)

_MIB = 1024**2
_GIB = 1024**3

# Names that exist in no golden, so a test using one is asserting about the guard rather
# than about something the operator happens to render today.
_UNMODELLED_WORKLOAD = "platformagent-newthing"
_UNMODELLED_WORKLOAD_KEY = f"Deployment/{_UNMODELLED_WORKLOAD}"
_UNMODELLED_CONTAINER = "newthing"
_ADDED_GATEWAY_CONTAINER = "otel-agent"
_ADDED_GATEWAY_SIDECAR = "token-refresher"


def _find_doc(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and (doc.get("metadata") or {}).get("name") == name:
            return doc
    raise AssertionError(f"{kind}/{name} is not in the golden")


def _gateway_pod_spec(docs):
    gateway = _find_doc(docs, "Deployment", gcf._GATEWAY_WORKLOAD)
    return gateway["spec"]["template"]["spec"]


def _add_unmodelled_deployment(docs):
    docs.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": _UNMODELLED_WORKLOAD},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": _UNMODELLED_CONTAINER,
                                "resources": {
                                    "requests": {"cpu": "500m", "memory": "1Gi"}
                                },
                            }
                        ]
                    }
                }
            },
        }
    )


def _add_gateway_container(docs):
    _gateway_pod_spec(docs)["containers"].append(
        {
            "name": _ADDED_GATEWAY_CONTAINER,
            "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}},
        }
    )


def _add_gateway_sidecar(docs):
    _gateway_pod_spec(docs)["initContainers"].append(
        {
            "name": _ADDED_GATEWAY_SIDECAR,
            "restartPolicy": "Always",
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
        }
    )


def _promote_ignored_init_container(docs):
    """Turn an ignored ordinary init container into a native sidecar."""
    for container in _gateway_pod_spec(docs)["initContainers"]:
        if container["name"] == gcf._SANDBOX_SSH_KEY_CONTAINER:
            container["restartPolicy"] = "Always"
            return
    raise AssertionError(
        f"{gcf._SANDBOX_SSH_KEY_CONTAINER} is no longer in the golden's init containers"
    )


def _rename_shell_container(docs):
    doc = _find_doc(docs, "StatefulSet", gcf._SHELL_WORKLOAD)
    for container in doc["spec"]["template"]["spec"].get("containers", []):
        if container.get("name") == gcf._SHELL_CONTAINER:
            container["name"] = "renamed"


def _scale_shell_statefulset(docs):
    doc = _find_doc(docs, "StatefulSet", gcf._SHELL_WORKLOAD)
    doc["spec"]["replicas"] = 2


def _scale_cred_proxy_deployment(docs):
    doc = _find_doc(docs, "Deployment", gcf._CRED_PROXY_WORKLOAD)
    doc["spec"]["replicas"] = 3


def _scale_gateway_deployment(docs):
    doc = _find_doc(docs, "Deployment", gcf._GATEWAY_WORKLOAD)
    doc["spec"]["replicas"] = 2


@contextlib.contextmanager
def _golden_with(mutate):
    """Run the block against the committed golden with `mutate` applied to its documents."""
    docs = [doc for doc in yaml.safe_load_all(gcf._GOLDEN_MANIFEST.read_text()) if doc]
    mutate(docs)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump_all(docs, fh)
        path = pathlib.Path(fh.name)
    try:
        with unittest.mock.patch.object(gcf, "_GOLDEN_MANIFEST", path):
            yield path
    finally:
        path.unlink(missing_ok=True)


def _container(name="c", requests=None, limits=None):
    """A container as it appears in the golden, with only the keys the generator reads."""
    resources = {}
    if requests is not None:
        resources["requests"] = requests
    if limits is not None:
        resources["limits"] = limits
    container = {"name": name}
    if resources:
        container["resources"] = resources
    return container


class ParseQuantityTest(unittest.TestCase):
    """The parsers. Returning 0 for something unreadable is the failure that matters."""

    def test_binary_si_suffixes(self):
        self.assertEqual(gcf.parse_bytes("1Ki"), 1024)
        self.assertEqual(gcf.parse_bytes("2Mi"), 2 * _MIB)
        self.assertEqual(gcf.parse_bytes("3Gi"), 3 * _GIB)
        self.assertEqual(gcf.parse_bytes("1Ti"), 1024**4)
        self.assertEqual(gcf.parse_bytes("1Pi"), 1024**5)
        self.assertEqual(gcf.parse_bytes("1Ei"), 1024**6)

    def test_decimal_si_suffixes(self):
        self.assertEqual(gcf.parse_bytes("1k"), 1000)
        self.assertEqual(gcf.parse_bytes("1M"), 1000**2)
        self.assertEqual(gcf.parse_bytes("1G"), 1000**3)
        self.assertEqual(gcf.parse_bytes("1T"), 1000**4)
        self.assertEqual(gcf.parse_bytes("1P"), 1000**5)
        self.assertEqual(gcf.parse_bytes("1E"), 1000**6)

    def test_bare_and_non_string_quantities(self):
        self.assertEqual(gcf.parse_bytes("512"), 512)
        self.assertEqual(gcf.parse_bytes(512), 512)
        self.assertEqual(gcf.parse_bytes(512.0), 512)
        self.assertEqual(gcf.parse_bytes(""), 0)
        self.assertEqual(gcf.parse_bytes("  "), 0)

    def test_milli_bytes(self):
        self.assertEqual(gcf.parse_bytes("1000m"), 1)
        self.assertEqual(gcf.parse_bytes("1001m"), 2)
        self.assertEqual(gcf.parse_bytes("1288490188800m"), 1288490189)

    def test_binary_is_preferred_over_decimal(self):
        """`1Ei` must read as a binary exbibyte, not a decimal exabyte."""
        self.assertEqual(gcf.parse_bytes("1Ei"), 1024**6)
        self.assertNotEqual(gcf.parse_bytes("1Ei"), 1000**6)

    def test_cpu_millis(self):
        self.assertEqual(gcf.parse_cpu_millis("150m"), 150)
        self.assertEqual(gcf.parse_cpu_millis("1"), 1000)
        self.assertEqual(gcf.parse_cpu_millis("2.5"), 2500)
        self.assertEqual(gcf.parse_cpu_millis(2), 2000)
        self.assertEqual(gcf.parse_cpu_millis(0.5), 500)
        self.assertEqual(gcf.parse_cpu_millis(""), 0)

    def test_the_forms_the_chart_parser_accepts_parse_here_too(self):
        """kube-agents.parseCpuMillis and parseBytes take a fraction or an exponent.

        `int(val)` raised ValueError on both, so a golden holding a quantity the chart
        itself reads crashed `make chart-check` instead of being summed.
        """
        self.assertEqual(gcf.parse_bytes("1e3"), 1000)
        self.assertEqual(gcf.parse_bytes("1.5"), 1)
        self.assertEqual(gcf.parse_bytes("1.5Gi"), int(1.5 * _GIB))
        self.assertEqual(gcf.parse_cpu_millis("500.5m"), 500)
        self.assertEqual(gcf.parse_cpu_millis("1e1"), 10000)
        self.assertEqual(gcf.parse_cpu_millis("1k"), 1000000)
        self.assertEqual(gcf.parse_cpu_millis("2M"), 2000000000)

    def test_a_declared_zero_request_is_not_replaced_by_the_limit(self):
        """`if not val` called a declared 0 absent and charged the limit in its place."""
        container = {
            "resources": {"requests": {"cpu": 0}, "limits": {"cpu": "2"}},
        }
        self.assertEqual(
            gcf._quantity(container, "requests", "cpu", gcf.parse_cpu_millis), 0
        )
        # An omitted request still defaults to the limit, which is what Kubernetes does.
        omitted = {"resources": {"limits": {"cpu": "2"}}}
        self.assertEqual(
            gcf._quantity(omitted, "requests", "cpu", gcf.parse_cpu_millis), 2000
        )


class FormatQuantityTest(unittest.TestCase):
    """These mirror kube-agents.formatCpu / formatBytes, so the two must agree."""

    def test_format_cpu(self):
        self.assertEqual(gcf.format_cpu(1000), "1")
        self.assertEqual(gcf.format_cpu(4500), "4500m")
        self.assertEqual(gcf.format_cpu(0), "0m")

    def test_format_bytes(self):
        self.assertEqual(gcf.format_bytes(_GIB), "1Gi")
        self.assertEqual(gcf.format_bytes(2560 * _MIB), "2560Mi")
        self.assertEqual(gcf.format_bytes(0), "0")

    def test_format_bytes_falls_back_to_a_bare_count(self):
        """Not every byte count is a whole binary unit; rounding one would be a lie."""
        self.assertEqual(gcf.format_bytes(1000000000), "1000000000")

    def test_the_two_formatters_round_trip_through_the_parsers(self):
        for value in (_GIB, 2560 * _MIB, 3 * _GIB):
            self.assertEqual(gcf.parse_bytes(gcf.format_bytes(value)), value)
        for millis in (1000, 4500, 150):
            self.assertEqual(gcf.parse_cpu_millis(gcf.format_cpu(millis)), millis)


class PrettierDumperTest(unittest.TestCase):
    """The generated file is checked by both `--check` and Prettier, which must agree."""

    def _dump(self, data):
        return yaml.dump(data, Dumper=gcf._PrettierCompatibleDumper, sort_keys=False)

    def test_numeric_looking_strings_are_double_quoted(self):
        out = self._dump({"cpu": "1", "eph": "0"})
        self.assertIn('cpu: "1"', out)
        self.assertIn('eph: "0"', out)
        self.assertNotIn("'1'", out)

    def test_unit_bearing_strings_stay_plain(self):
        out = self._dump({"cpu": "150m", "memory": "2Gi"})
        self.assertIn("cpu: 150m", out)
        self.assertIn("memory: 2Gi", out)
        self.assertNotIn('"150m"', out)


class WorkloadEntryTest(unittest.TestCase):
    def test_containers_are_summed(self):
        entry = gcf._workload_entry(
            [
                _container(requests={"cpu": "100m", "memory": "128Mi"}),
                _container(requests={"cpu": "150m", "memory": "384Mi"}),
            ],
            pods=1,
        )
        self.assertEqual(entry["cpuMillisRequest"], 250)
        self.assertEqual(entry["memoryBytesRequest"], 512 * _MIB)
        self.assertEqual(entry["requests"]["cpu"], "250m")
        self.assertEqual(entry["pods"], 1)

    def test_a_container_without_resources_contributes_zero(self):
        entry = gcf._workload_entry([_container()], pods=0)
        self.assertEqual(entry["cpuMillisLimit"], 0)
        self.assertEqual(entry["memoryBytesLimit"], 0)
        self.assertEqual(entry["ephemeralStorageBytesLimit"], 0)
        self.assertEqual(entry["limits"]["ephemeral-storage"], "0")

    def test_omitted_request_defaults_to_limit(self):
        """When requests omits a resource, Kubernetes defaults requests to match limits."""
        entry = gcf._workload_entry(
            [_container(limits={"ephemeral-storage": "2Gi"})],
            pods=1,
        )
        self.assertEqual(entry["ephemeralStorageBytesRequest"], 2 * _GIB)
        self.assertEqual(entry["ephemeralStorageBytesLimit"], 2 * _GIB)
        self.assertEqual(entry["requests"]["ephemeral-storage"], "2Gi")

    def test_claim_storage_defaults_to_zero(self):
        self.assertEqual(gcf._claim_storage({}), 0)
        self.assertEqual(
            gcf._claim_storage({"spec": {"resources": {"requests": {"storage": "8Gi"}}}}),
            8 * _GIB,
        )

    def test_pod_spec_tolerates_a_document_with_no_template(self):
        self.assertEqual(gcf._pod_spec({}), {})
        self.assertEqual(
            gcf._pod_spec({"spec": {"template": {"spec": {"a": 1}}}}), {"a": 1}
        )
        self.assertEqual(gcf._pod_spec({"kind": "Pod", "spec": {"a": 1}}), {"a": 1})
        self.assertEqual(
            gcf._pod_spec(
                {"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": {"a": 1}}}}}}
            ),
            {"a": 1},
        )


class ExtractFootprintTest(unittest.TestCase):
    """Runs against the committed golden, which is the generator's real input."""

    @classmethod
    def setUpClass(cls):
        cls.data = gcf.extract_footprint()
        cls.op = cls.data["operatorRendered"]

    def test_shape(self):
        for key in ("agentPod", "shellSandbox", "credentialProxy", "storage"):
            self.assertIn(key, self.op)
        self.assertIn("base", self.op["agentPod"])
        self.assertIn("dashboard", self.op["agentPod"])

    def test_the_agent_pod_base_sums_its_three_containers(self):
        """platform-agent + fluent-bit + the agent-api-auth native sidecar."""
        base = self.op["agentPod"]["base"]
        self.assertEqual(base["cpuMillisRequest"], 1000 + 100 + 150)
        self.assertEqual(base["cpuMillisLimit"], 3000 + 500 + 1000)
        self.assertEqual(base["ephemeralStorageBytesRequest"], 3 * _GIB)
        self.assertEqual(base["ephemeralStorageBytesLimit"], 3 * _GIB)
        self.assertEqual(base["pods"], 1)

    def test_the_dashboard_adds_resources_but_no_pod(self):
        self.assertEqual(self.op["agentPod"]["dashboard"]["pods"], 0)
        self.assertGreater(self.op["agentPod"]["dashboard"]["cpuMillisRequest"], 0)

    def test_true_init_containers_are_excluded(self):
        """A pod's request is max(largest init, sum of the rest), and the sum dominates.

        `sandbox-credential-cleanup` (100m) and `sandbox-ssh-key` (10m) are ordinary init
        containers; counting them would inflate every install's footprint. `agent-api-auth`
        carries restartPolicy: Always, so it is a sidecar and does count -- the assertion
        above covers it.
        """
        base = self.op["agentPod"]["base"]
        self.assertEqual(base["cpuMillisRequest"], 1250)
        self.assertNotEqual(base["cpuMillisRequest"], 1250 + 100 + 10)

    def test_claims_are_counted(self):
        self.assertEqual(self.op["storage"]["persistentVolumeClaims"], 4)
        self.assertEqual(self.op["storage"]["storageBytesRequest"], 22 * _GIB)

    def test_a_renamed_container_is_an_error_rather_than_a_zero(self):
        """Finding containers by name means a rename must fail loudly, not sum to 0."""
        with _golden_with(_rename_shell_container):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        self.assertIn(gcf._SHELL_CONTAINER, str(caught.exception))

    def test_shell_replicas_multiplies_pods_resources_and_claims(self):
        with _golden_with(_scale_shell_statefulset):
            data = gcf.extract_footprint()
        shell = data["operatorRendered"]["shellSandbox"]
        baseline_shell = self.op["shellSandbox"]
        self.assertEqual(shell["pods"], 2)
        self.assertEqual(shell["cpuMillisRequest"], baseline_shell["cpuMillisRequest"] * 2)
        self.assertEqual(shell["memoryBytesRequest"], baseline_shell["memoryBytesRequest"] * 2)
        # Shell StatefulSet has 2 volumeClaimTemplates (data=10Gi, sshd=1Gi), so scaling from
        # 1 to 2 replicas adds 2 claims (baseline 4 -> 6) and 11Gi storage (baseline 22Gi -> 33Gi).
        self.assertEqual(data["operatorRendered"]["storage"]["persistentVolumeClaims"], 6)
        self.assertEqual(
            data["operatorRendered"]["storage"]["storageBytesRequest"],
            self.op["storage"]["storageBytesRequest"] + 11 * _GIB,
        )

    def test_cred_proxy_replicas_multiplies_pods_and_resources(self):
        with _golden_with(_scale_cred_proxy_deployment):
            data = gcf.extract_footprint()
        cred = data["operatorRendered"]["credentialProxy"]
        baseline_cred = self.op["credentialProxy"]
        self.assertEqual(cred["pods"], 3)
        self.assertEqual(cred["cpuMillisRequest"], baseline_cred["cpuMillisRequest"] * 3)
        self.assertEqual(cred["memoryBytesRequest"], baseline_cred["memoryBytesRequest"] * 3)

    def test_gateway_replicas_must_be_one(self):
        with _golden_with(_scale_gateway_deployment):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        self.assertIn("expected 1", str(caught.exception))

    def test_empty_documents_in_the_stream_are_skipped(self):
        """A stray `---` yields a None document; iterating it must not crash the sum."""
        padded = "---\n" + gcf._GOLDEN_MANIFEST.read_text() + "\n---\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(padded)
            path = pathlib.Path(fh.name)
        try:
            with unittest.mock.patch.object(gcf, "_GOLDEN_MANIFEST", path):
                padded_data = gcf.extract_footprint()
            self.assertEqual(padded_data, self.data)
        finally:
            path.unlink(missing_ok=True)

    def test_a_missing_golden_is_an_error(self):
        missing = gcf._GOLDEN_MANIFEST.parent / "does-not-exist.yaml"
        with unittest.mock.patch.object(gcf, "_GOLDEN_MANIFEST", missing):
            with self.assertRaises(FileNotFoundError):
                gcf.extract_footprint()


class MainExitCodeTest(unittest.TestCase):
    """`make chart-check` reads these apart: 1 is drift, anything else is "cannot run"."""

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with unittest.mock.patch.object(sys, "argv", argv):
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    gcf.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_check_passes_against_the_committed_file(self):
        code, out, _ = self._run_main(["generate_chart_footprint.py", "--check"])
        self.assertEqual(code, 0)
        self.assertIn("in sync", out)

    def test_check_reports_drift_as_exit_1(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("operatorRendered: {}\n")
            path = pathlib.Path(fh.name)
        try:
            with unittest.mock.patch.object(gcf, "_FOOTPRINT_FILE", path):
                code, _, err = self._run_main(["generate_chart_footprint.py", "--check"])
            self.assertEqual(code, gcf._EXIT_DRIFT)
            self.assertIn("out of date", err)
        finally:
            path.unlink(missing_ok=True)

    def test_a_missing_footprint_is_drift_not_a_crash(self):
        missing = pathlib.Path(tempfile.gettempdir()) / "no-such-footprint.yaml"
        with unittest.mock.patch.object(gcf, "_FOOTPRINT_FILE", missing):
            code, _, err = self._run_main(["generate_chart_footprint.py", "--check"])
        self.assertEqual(code, gcf._EXIT_DRIFT)
        self.assertIn("does not exist", err)

    def test_a_missing_golden_is_exit_2_not_drift(self):
        """Reporting this as drift would send the reader to re-sync, which cannot help."""
        missing = gcf._GOLDEN_MANIFEST.parent / "does-not-exist.yaml"
        with unittest.mock.patch.object(gcf, "_GOLDEN_MANIFEST", missing):
            code, _, err = self._run_main(["generate_chart_footprint.py", "--check"])
        self.assertEqual(code, gcf._EXIT_CANNOT_RUN)
        self.assertIn("cannot build the chart footprint", err)

    def test_write_mode_reproduces_the_committed_file(self):
        committed = REPO_ROOT / "charts" / "kube-agents" / "files" / "footprint.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "footprint.yaml"
            with unittest.mock.patch.object(gcf, "_FOOTPRINT_FILE", path):
                code, out, _ = self._run_main(["generate_chart_footprint.py"])
            self.assertEqual(code, 0)
            self.assertIn("Generated", out)
            self.assertEqual(path.read_text(), committed.read_text())

    def test_the_generated_file_carries_the_do_not_edit_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "footprint.yaml"
            with unittest.mock.patch.object(gcf, "_FOOTPRINT_FILE", path):
                self._run_main(["generate_chart_footprint.py"])
            self.assertTrue(path.read_text().startswith(gcf._HEADER))
            self.assertIn("do not edit", path.read_text())

    def test_an_unreadable_golden_is_exit_2_not_drift(self):
        """A present-but-unreadable golden used to raise past the except clause and exit 1."""
        with unittest.mock.patch.object(
            pathlib.Path, "read_text", side_effect=PermissionError("denied")
        ):
            code, _, err = self._run_main(["generate_chart_footprint.py", "--check"])
        self.assertEqual(code, gcf._EXIT_CANNOT_RUN)
        self.assertIn("cannot build the chart footprint", err)

    def test_a_missing_pyyaml_is_exit_2_not_drift(self):
        """A bare `import yaml` exits 1, which the sync script reads as drift."""
        real_import = builtins.__import__

        def refuse_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("no module named yaml")
            return real_import(name, *args, **kwargs)

        namespace = {"__name__": "generate_chart_footprint_without_yaml"}
        err = io.StringIO()
        with unittest.mock.patch.object(builtins, "__import__", refuse_yaml):
            with self.assertRaises(SystemExit) as caught:
                with redirect_stderr(err):
                    exec(  # noqa: S102 - the module's import guard is the thing under test
                        compile(_MODULE_PATH.read_text(), str(_MODULE_PATH), "exec"),
                        namespace,
                    )
        self.assertEqual(caught.exception.code, gcf._EXIT_CANNOT_RUN)
        self.assertIn("PyYAML", err.getvalue())

    def test_a_golden_the_guard_rejects_is_exit_2_not_drift(self):
        """Re-syncing cannot fix an unaccounted workload, so this must not read as drift."""
        with _golden_with(_add_unmodelled_deployment):
            code, _, err = self._run_main(["generate_chart_footprint.py", "--check"])
        self.assertEqual(code, gcf._EXIT_CANNOT_RUN)
        self.assertIn("does not account for", err)

    def test_check_prints_the_diff_it_claims_to(self):
        """hack/sync-chart-manifests.sh tells the reader to see the diff above."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("operatorRendered: {}\n")
            path = pathlib.Path(fh.name)
        try:
            with unittest.mock.patch.object(gcf, "_FOOTPRINT_FILE", path):
                code, _, err = self._run_main(["generate_chart_footprint.py", "--check"])
            self.assertEqual(code, gcf._EXIT_DRIFT)
            self.assertIn(f"--- {gcf._DIFF_COMMITTED_LABEL}", err)
            self.assertIn(f"+++ {gcf._DIFF_GENERATED_LABEL}", err)
            self.assertIn("-operatorRendered: {}", err)
        finally:
            path.unlink(missing_ok=True)


class CompletenessGuardTest(unittest.TestCase):
    """The guard that makes an addition to the golden as loud as a rename.

    Finding things by name means the generator cannot see what it was never told about:
    before this guard, a workload the operator started rendering was simply absent from
    footprint.yaml and `make chart-check` reported no drift, because nothing looked for
    it. The quota preflight then passed the install it exists to stop.
    """

    @classmethod
    def setUpClass(cls):
        cls.baseline = gcf.extract_footprint()

    def test_the_committed_golden_passes(self):
        """Every pod-bearing workload the operator renders today is summed or ignored."""
        self.assertIn("operatorRendered", self.baseline)

    def test_a_new_workload_is_rejected(self):
        with _golden_with(_add_unmodelled_deployment):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        message = str(caught.exception)
        self.assertIn(_UNMODELLED_WORKLOAD_KEY, message)
        self.assertIn("contributes nothing", message)

    def test_a_new_container_in_a_summed_workload_is_rejected(self):
        with _golden_with(_add_gateway_container):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        message = str(caught.exception)
        self.assertIn(_ADDED_GATEWAY_CONTAINER, message)
        self.assertIn(gcf._GATEWAY_WORKLOAD_KEY, message)

    def test_a_new_native_sidecar_is_rejected(self):
        """A sidecar counts toward the pod's request, so an unsummed one undercounts it."""
        with _golden_with(_add_gateway_sidecar):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        self.assertIn(_ADDED_GATEWAY_SIDECAR, str(caught.exception))

    def test_the_message_says_what_was_found_and_what_to_do(self):
        with _golden_with(_add_gateway_container):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        message = str(caught.exception)
        # The size, so a reader can judge whether it belongs in the totals.
        self.assertIn("requests 250m cpu, 256Mi memory", message)
        self.assertIn("_IGNORED_CONTAINERS", message)
        self.assertIn("make chart-sync", message)

    def test_an_ignored_workload_is_accepted_and_changes_no_total(self):
        with _golden_with(_add_unmodelled_deployment):
            with unittest.mock.patch.dict(
                gcf._IGNORED_WORKLOADS, {_UNMODELLED_WORKLOAD_KEY: "a test's reason"}
            ):
                data = gcf.extract_footprint()
        self.assertEqual(data, self.baseline)

    def test_an_ignored_container_is_accepted_and_changes_no_total(self):
        key = gcf._container_key(gcf._GATEWAY_WORKLOAD_KEY, _ADDED_GATEWAY_CONTAINER)
        with _golden_with(_add_gateway_container):
            with unittest.mock.patch.dict(gcf._IGNORED_CONTAINERS, {key: "a test's reason"}):
                data = gcf.extract_footprint()
        self.assertEqual(data, self.baseline)

    def test_every_ignore_entry_carries_a_reason(self):
        """An ignore list without reasons is a list nobody can review."""
        for key, reason in {**gcf._IGNORED_WORKLOADS, **gcf._IGNORED_CONTAINERS}.items():
            self.assertTrue(reason.strip(), f"{key} is ignored without a reason")

    def test_an_ignored_init_container_promoted_to_a_sidecar_is_rejected(self):
        """The entry's reason is that it is an init container; Always ends that."""
        with _golden_with(_promote_ignored_init_container):
            with self.assertRaises(ValueError) as caught:
                gcf.extract_footprint()
        message = str(caught.exception)
        self.assertIn(gcf._SANDBOX_SSH_KEY_CONTAINER, message)
        self.assertIn("restarts Always", message)

    def test_the_ignored_containers_are_the_goldens_ordinary_init_containers(self):
        """The list covers exactly today's non-sidecar init containers, nothing stale."""
        docs = [doc for doc in yaml.safe_load_all(gcf._GOLDEN_MANIFEST.read_text()) if doc]
        gateway = _gateway_pod_spec(docs)
        ordinary_init = {
            container["name"]
            for container in gateway.get("initContainers", [])
            if container.get("restartPolicy") != "Always"
        }
        self.assertEqual(
            set(gcf._IGNORED_CONTAINERS),
            {
                gcf._container_key(gcf._GATEWAY_WORKLOAD_KEY, name)
                for name in ordinary_init
            },
        )

    def test_the_guard_reads_every_pod_bearing_kind(self):
        """A Job or DaemonSet the operator starts rendering has to fail the guard too."""
        for kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"):
            self.assertIn(kind, gcf._POD_BEARING_KINDS)

    def test_a_workload_with_no_containers_at_all_is_reported(self):
        """An empty pod spec sums to nothing, which must not read as "fully accounted"."""
        with self.assertRaises(ValueError) as caught:
            gcf._check_completeness({"DaemonSet/node-thing": {}}, set())
        self.assertIn("DaemonSet/node-thing", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
