"""The chart's fleet example, `examples/values-fleet.yaml`, renders and reaches the CR.

The file is what the site's fleet-sizing page tells an operator of 25 or more clusters to
pass, so four things have to hold: `helm lint` accepts it against the chart's schema; every
value it sets lands on the `PlatformAgent` CR (or the operator Deployment) under the path the
operator reads; every `platformAgent.deployment.env` name in it is one the operator forwards,
because a name outside the allowlist is dropped silently and the file would ship a key that
changes nothing; and the figures the page's comments rest on hold against the operator's
rules (the operator memory floor, the Autopilot memory-per-vCPU band, requests under limits).

Run: python3 -m unittest discover -s tests -p 'test_chart_values_fleet_example.py' -v
"""

import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_EXAMPLE = _CHART / "examples" / "values-fleet.yaml"
_README = _CHART / "README.md"
_SITE_PAGE = _REPO_ROOT / "docs" / "site" / "src" / "content" / "docs" / "deploy" / "fleet-sizing.md"
_PREREQUISITES_PAGE = _REPO_ROOT / "docs" / "site" / "src" / "content" / "docs" / "install" / "prerequisites.md"
_UPGRADE_PAGE = _REPO_ROOT / "docs" / "site" / "src" / "content" / "docs" / "install" / "upgrade.md"
_AGENT_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_SANDBOX_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "shell_sandbox_manifests.go"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_OPERATOR_TEMPLATE = "templates/operator-deployment.yaml"
_OPERATOR_CONTAINER = "manager"
_SITE_ROUTE = "/kube-agents/deploy/fleet-sizing/"
_EXAMPLE_NAME = "values-fleet.yaml"

# The operator's allowlist for spec.deployment.env onto the agent container
# (safeSandboxEnvOverrides), plus the one name it forwards to the shell sandbox
# instead (envAuditAutoPromote). Read from the Go source so a name dropped there
# fails here rather than shipping in the example.
_ALLOWLIST_RE = re.compile(
    r"func safeSandboxEnvOverrides\(.*?allowed := map\[string\]struct\{\}\{(?P<body>.*?)\n\t\}",
    re.DOTALL,
)
_ALLOWLIST_ENTRY_RE = re.compile(r'^\s*"(?P<name>[A-Z0-9_]+)":\s*\{\}', re.MULTILINE)
_SANDBOX_FORWARD_RE = re.compile(r'envAuditAutoPromote\s*=\s*"(?P<name>[A-Z0-9_]+)"')

# What the operator and Autopilot hold the figures to.
_OPERATOR_MEMORY_FLOOR_MI = 512
_AUTOPILOT_MIN_MIB_PER_VCPU = 1024
_AUTOPILOT_MAX_MIB_PER_VCPU = 6.5 * 1024
_AUTOPILOT_EPHEMERAL_CEILING_MI = 10 * 1024
_PROXY_DEFAULT_EPHEMERAL_MI = 8 * 1024

_QUANTITY_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<suffix>m|Mi|Gi|Ti|)$")
_MIB_PER_SUFFIX = {"Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}


def _millicores(quantity) -> float:
    match = _QUANTITY_RE.match(str(quantity))
    if match is None or match.group("suffix") not in ("m", ""):
        raise AssertionError(f"not a CPU quantity: {quantity!r}")
    number = float(match.group("number"))
    return number if match.group("suffix") == "m" else number * 1000


def _mebibytes(quantity) -> float:
    match = _QUANTITY_RE.match(str(quantity))
    if match is None or match.group("suffix") not in _MIB_PER_SUFFIX:
        raise AssertionError(f"not a memory quantity: {quantity!r}")
    return float(match.group("number")) * _MIB_PER_SUFFIX[match.group("suffix")]


def _example() -> dict:
    return yaml.safe_load(_EXAMPLE.read_text())


def _forwarded_env_names() -> set:
    source = _AGENT_MANIFESTS.read_text()
    block = _ALLOWLIST_RE.search(source)
    if block is None:
        raise AssertionError("safeSandboxEnvOverrides' allowlist is missing from the operator source")
    names = {m.group("name") for m in _ALLOWLIST_ENTRY_RE.finditer(block.group("body"))}
    sandbox = _SANDBOX_FORWARD_RE.search(_SANDBOX_MANIFESTS.read_text())
    if sandbox is None:
        raise AssertionError("envAuditAutoPromote is missing from the shell sandbox source")
    names.add(sandbox.group("name"))
    return names


class ExampleShapeTest(unittest.TestCase):
    """Readable without helm: what the file sets and what the page says about it."""

    def setUp(self):
        self.values = _example()

    def test_the_example_sets_only_what_the_chart_exposes(self):
        self.assertEqual(set(self.values), {"operator", "platformAgent"})
        self.assertEqual(set(self.values["operator"]), {"resources"})
        platform_agent = self.values["platformAgent"]
        self.assertEqual(set(platform_agent), {"storage", "harness", "deployment"})
        self.assertEqual(platform_agent["storage"], {"size": "100Gi"})
        self.assertEqual(platform_agent["harness"], {"tuning": {"maxInProgress": 3}})
        self.assertEqual(set(platform_agent["deployment"]), {"env", "credentialProxy", "agentAPIAuth"})

    def test_every_env_name_is_one_the_operator_forwards(self):
        forwarded = _forwarded_env_names()
        env = self.values["platformAgent"]["deployment"]["env"]
        self.assertEqual(
            [entry["name"] for entry in env],
            ["INCIDENT_WORKLOAD_DEDUP_SECONDS", "KUBEAGENTS_AUDIT_AUTO_PROMOTE"],
        )
        for entry in env:
            with self.subTest(name=entry["name"]):
                self.assertIn(entry["name"], forwarded)
                # The operator forwards literal values only, and the CRD's env
                # value is a string: a bare 3600 or false would fail at apply.
                self.assertIsInstance(entry["value"], str)
                self.assertNotIn("valueFrom", entry)

    def test_the_names_the_maintainers_left_out_stay_out(self):
        names = {entry["name"] for entry in self.values["platformAgent"]["deployment"]["env"]}
        forwarded = _forwarded_env_names()
        for name in ("CLOUDSDK_CORE_DISABLE_FILE_LOGGING", "MALLOC_ARENA_MAX"):
            with self.subTest(name=name):
                self.assertNotIn(name, names)
                self.assertNotIn(name, forwarded, f"{name} is forwarded now; the page and the example can carry it")

    def test_the_operator_memory_limit_meets_the_floor_and_matches_its_request(self):
        resources = self.values["operator"]["resources"]
        limit = _mebibytes(resources["limits"]["memory"])
        self.assertGreaterEqual(limit, _OPERATOR_MEMORY_FLOOR_MI)
        # Autopilot without bursting sets limits equal to requests; the file
        # promises the manager 1Gi there, which only an equal request delivers.
        self.assertEqual(limit, _mebibytes(resources["requests"]["memory"]))

    def test_every_requests_pair_sits_in_the_autopilot_band(self):
        deployment = self.values["platformAgent"]["deployment"]
        pairs = {
            "operator": self.values["operator"]["resources"]["requests"],
            "credentialProxy": deployment["credentialProxy"]["resources"]["requests"],
            "agentAPIAuth": deployment["agentAPIAuth"]["resources"]["requests"],
        }
        for name, requests in pairs.items():
            with self.subTest(container=name):
                per_vcpu = _mebibytes(requests["memory"]) / (_millicores(requests["cpu"]) / 1000)
                self.assertGreaterEqual(per_vcpu, _AUTOPILOT_MIN_MIB_PER_VCPU)
                self.assertLessEqual(per_vcpu, _AUTOPILOT_MAX_MIB_PER_VCPU)

    def test_no_request_exceeds_its_limit_and_the_proxy_keeps_its_ephemeral_default(self):
        deployment = self.values["platformAgent"]["deployment"]
        for name, resources in (
            ("operator", self.values["operator"]["resources"]),
            ("credentialProxy", deployment["credentialProxy"]["resources"]),
            ("agentAPIAuth", deployment["agentAPIAuth"]["resources"]),
        ):
            for key, convert in (("cpu", _millicores), ("memory", _mebibytes)):
                limit = resources.get("limits", {}).get(key)
                request = resources.get("requests", {}).get(key)
                if limit is None or request is None:
                    continue
                with self.subTest(container=name, resource=key):
                    self.assertLessEqual(convert(request), convert(limit))
        proxy = deployment["credentialProxy"]["resources"]
        for block in ("requests", "limits"):
            self.assertNotIn("ephemeral-storage", proxy[block])
        self.assertLessEqual(_PROXY_DEFAULT_EPHEMERAL_MI, _AUTOPILOT_EPHEMERAL_CEILING_MI)
        # The watcher's GOMAXPROCS follows limits.cpu, so the example leaves it alone.
        self.assertNotIn("cpu", deployment["agentAPIAuth"]["resources"]["limits"])

    def test_the_page_and_the_readme_name_the_example_and_each_other(self):
        page = _SITE_PAGE.read_text()
        readme = _README.read_text()
        self.assertIn(_EXAMPLE_NAME, page)
        self.assertIn(_EXAMPLE_NAME, readme)
        self.assertIn(_SITE_ROUTE, readme)
        for linking_page in (_PREREQUISITES_PAGE, _UPGRADE_PAGE):
            with self.subTest(page=linking_page.name):
                self.assertIn(_SITE_ROUTE, linking_page.read_text())
        for entry in self.values["platformAgent"]["deployment"]["env"]:
            with self.subTest(name=entry["name"]):
                self.assertIn(entry["name"], page)


@unittest.skipUnless(_HELM, "helm is not installed")
class ExampleRenderTest(unittest.TestCase):
    def _helm(self, *args):
        return subprocess.run(
            ["helm", *args, str(_CHART), *_REQUIRED, "-f", str(_EXAMPLE)],
            capture_output=True, text=True,
        )

    def _documents(self, template):
        proc = self._helm("template", "test-release", "--show-only", template)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]

    def test_helm_lint_passes(self):
        proc = self._helm("lint")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_cr_carries_every_key(self):
        values = _example()
        cr = next(d for d in self._documents(_CR_TEMPLATE) if d.get("kind") == "PlatformAgent")
        spec = cr["spec"]
        self.assertEqual(spec["storage"], {"size": "100Gi"})
        self.assertEqual(spec["harness"]["tuning"]["maxInProgress"], 3)
        deployment = spec["deployment"]
        expected_env = values["platformAgent"]["deployment"]["env"]
        rendered = {e["name"]: e for e in deployment["env"]}
        for entry in expected_env:
            with self.subTest(name=entry["name"]):
                self.assertEqual(rendered[entry["name"]], entry)
        self.assertEqual(
            deployment["credentialProxy"]["resources"],
            values["platformAgent"]["deployment"]["credentialProxy"]["resources"],
        )
        self.assertEqual(
            deployment["agentAPIAuth"]["resources"],
            values["platformAgent"]["deployment"]["agentAPIAuth"]["resources"],
        )
        # The chart has no value for the agent container's own resources.
        self.assertNotIn("resources", deployment)

    def test_the_operator_deployment_carries_the_raised_resources(self):
        values = _example()
        operator = next(d for d in self._documents(_OPERATOR_TEMPLATE) if d.get("kind") == "Deployment")
        containers = operator["spec"]["template"]["spec"]["containers"]
        manager = next(c for c in containers if c["name"] == _OPERATOR_CONTAINER)
        self.assertEqual(manager["resources"], values["operator"]["resources"])


if __name__ == "__main__":
    unittest.main()
