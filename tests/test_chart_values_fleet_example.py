"""The chart's fleet example, `examples/values-fleet.yaml`, renders and reaches the CR.

The file is what the site's fleet-sizing page tells an operator of 25 or more clusters to
pass, so four things have to hold: `helm lint` accepts it against the chart's schema; every
value it sets lands on the `PlatformAgent` CR (or the operator Deployment) under the path the
operator reads; every `platformAgent.deployment.env` name in it is one the operator forwards,
because a name outside the allowlist is dropped silently and the file would ship a key that
changes nothing; and the figures the page's comments rest on hold against the operator's
rules (the operator memory floor, the Autopilot memory-per-vCPU band, requests under limits).

The page's "What each pod needs" table also carries a declared column: what the chart and the
operator render at the defaults. Those figures live in `files/footprint.yaml` (generated from
the operator golden) and `values.yaml`, and a hand-written copy of either drifts the first time
one moves, so `DeclaredColumnTest` derives every declared cell, the pod count, the ephemeral
and storage totals and the Hindsight bullet from the two files and fails when the page says
otherwise. The fleet column is modeled and is not held here.

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

# The page's declared column, derived from the two files the page cites.
_FOOTPRINT = _CHART / "files" / "footprint.yaml"
_VALUES = _CHART / "values.yaml"
_PAGE_TABLE_HEADING = "## What each pod needs"
_PAGE_ROW_PROXY = "Credential proxy"
_PAGE_ROW_GATEWAY = "Gateway pod"
_PAGE_ROW_SANDBOX = "Shell sandbox"
_PAGE_ROW_OPERATOR = "Operator"
_PAGE_ROW_LITELLM = "LiteLLM"
_PAGE_ROW_MINTER = "GitHub token minter"
_PAGE_ROW_TOTAL = "**Total**"
_PAGE_HINDSIGHT_BULLET = "**Hindsight memory**"
_PAGE_FLEET_DATA_CLAIM_GI = 100

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


def _decimal(value: float) -> str:
    """Exact decimal text with trailing zeros dropped: 1506 millicores reads 1.506, 64Mi reads 0.0625 GiB."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _vcpu(millicores: float) -> str:
    return _decimal(millicores / 1000)


def _gib(mebibytes: float) -> str:
    return _decimal(mebibytes / 1024)


def _footprint_block(block: dict):
    """(request millicores, request MiB, limit millicores, limit MiB, ephemeral limit MiB) of a footprint entry."""
    return (
        block["cpuMillisRequest"],
        block["memoryBytesRequest"] / (1024 * 1024),
        block["cpuMillisLimit"],
        block["memoryBytesLimit"] / (1024 * 1024),
        block["ephemeralStorageBytesLimit"] / (1024 * 1024),
    )


def _values_block(resources: dict):
    """The same tuple from a values.yaml resources block (ephemeral-storage optional)."""
    requests, limits = resources["requests"], resources["limits"]
    ephemeral = limits.get("ephemeral-storage")
    return (
        _millicores(requests["cpu"]),
        _mebibytes(requests["memory"]),
        _millicores(limits["cpu"]),
        _mebibytes(limits["memory"]),
        _mebibytes(ephemeral) if ephemeral is not None else 0,
    )


def _cell(block, each=False) -> str:
    req_cpu, req_mem, lim_cpu, lim_mem, _ = block
    text = f"{_vcpu(req_cpu)} / {_gib(req_mem)} then {_vcpu(lim_cpu)} / {_gib(lim_mem)}"
    return text + ", each" if each else text


def _page_table_rows() -> dict:
    """The pod table's rows keyed by the text their first cell opens with."""
    page = _SITE_PAGE.read_text()
    section = page.split(_PAGE_TABLE_HEADING, 1)[1]
    rows = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows[cells[0]] = cells
    return rows


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


class DeclaredColumnTest(unittest.TestCase):
    """The page's declared figures are the footprint's and values.yaml's, not a copy of them."""

    @classmethod
    def setUpClass(cls):
        cls.footprint = yaml.safe_load(_FOOTPRINT.read_text())["operatorRendered"]
        cls.values = yaml.safe_load(_VALUES.read_text())
        cls.rows = _page_table_rows()

    def _row(self, label):
        matches = [cells for first, cells in self.rows.items() if first.startswith(label)]
        self.assertEqual(len(matches), 1, f"expected one table row opening {label!r}, found {len(matches)}")
        return matches[0]

    def _declared(self):
        """Every table row's block, replica count and persistent GiB, in the page's order."""
        agent = self.footprint["agentPod"]
        base = _footprint_block(agent["base"])
        # The dashboard container rides in the gateway pod when hermes.dashboardEnabled is
        # on, which the chart's null default leaves to the CRD's true; the footprint lists
        # it apart (pods: 0) so the quota preflight can leave it out.
        dashboard = _footprint_block(agent["dashboard"])
        gateway = tuple(b + d for b, d in zip(base, dashboard))
        data_claim_gi = self.footprint["storage"]["dataClaimBytesRequest"] / (1024 ** 3)
        return {
            _PAGE_ROW_PROXY: (_footprint_block(self.footprint["credentialProxy"]), 1, 0),
            _PAGE_ROW_GATEWAY: (gateway, 1, data_claim_gi + 1),
            _PAGE_ROW_SANDBOX: (_footprint_block(self.footprint["shellSandbox"]), 1, data_claim_gi + 1),
            _PAGE_ROW_OPERATOR: (_values_block(self.values["operator"]["resources"]), self.values["operator"]["replicaCount"], 0),
            _PAGE_ROW_LITELLM: (_values_block(self.values["litellm"]["resources"]), self.values["litellm"]["replicaCount"], 0),
            _PAGE_ROW_MINTER: (_values_block(self.values["githubMinter"]["resources"]), self.values["githubMinter"]["replicaCount"], 0),
        }

    def test_each_row_carries_the_declared_requests_and_limits(self):
        for label, (block, replicas, _) in self._declared().items():
            with self.subTest(row=label):
                cells = self._row(label)
                self.assertEqual(cells[1], str(replicas))
                self.assertEqual(cells[3], _cell(block, each=replicas > 1))

    def test_the_ephemeral_cells_are_the_declared_limits(self):
        declared = self._declared()
        proxy = declared[_PAGE_ROW_PROXY][0][4]
        gateway = declared[_PAGE_ROW_GATEWAY][0][4]
        litellm = declared[_PAGE_ROW_LITELLM][0][4]
        self.assertTrue(self._row(_PAGE_ROW_PROXY)[5].startswith(f"{_gib(proxy)} GiB"))
        self.assertTrue(self._row(_PAGE_ROW_GATEWAY)[5].startswith(f"{_gib(gateway)} GiB"))
        self.assertEqual(self._row(_PAGE_ROW_LITELLM)[5], f"{_gib(litellm)} GiB each")
        for label in (_PAGE_ROW_SANDBOX, _PAGE_ROW_OPERATOR, _PAGE_ROW_MINTER):
            with self.subTest(row=label):
                self.assertEqual(declared[label][0][4], 0)
                self.assertEqual(self._row(label)[5], "none declared")

    def test_the_total_row_sums_the_declared_rows(self):
        declared = self._declared()
        pods = sum(replicas for _, replicas, _ in declared.values())
        total = [0.0] * 5
        for block, replicas, _ in declared.values():
            for i, part in enumerate(block):
                total[i] += part * replicas
        cells = self._row(_PAGE_ROW_TOTAL)
        self.assertEqual(cells[1], str(pods))
        self.assertEqual(cells[3], f"**{_cell(tuple(total))}**")
        self.assertEqual(cells[5], f"**{_gib(total[4])} GiB**")

    def test_the_persistent_total_is_the_footprint_claims_and_the_fleet_raise(self):
        storage = self.footprint["storage"]
        default_gi = storage["storageBytesRequest"] / (1024 ** 3)
        data_claim_gi = storage["dataClaimBytesRequest"] / (1024 ** 3)
        example_gi = _mebibytes(_example()["platformAgent"]["storage"]["size"]) / 1024
        self.assertEqual(example_gi, _PAGE_FLEET_DATA_CLAIM_GI)
        fleet_gi = default_gi + storage["dataClaims"] * (example_gi - data_claim_gi)
        self.assertEqual(
            self._row(_PAGE_ROW_TOTAL)[6],
            f"**{_decimal(default_gi)} GiB default, {_decimal(fleet_gi)} GiB fleet**",
        )
        gateway = self._row(_PAGE_ROW_GATEWAY)[6]
        self.assertIn(f"{_decimal(data_claim_gi)} GiB default", gateway)
        self.assertIn(f"{_decimal(example_gi)} GiB fleet", gateway)

    def test_the_hindsight_bullet_carries_the_declared_figures(self):
        page = _SITE_PAGE.read_text()
        bullet = next(line for line in page.splitlines() if _PAGE_HINDSIGHT_BULLET in line)
        # The bullet wraps over several lines; take the paragraph that follows it.
        start = page.index(bullet)
        paragraph = " ".join(page[start:].split("\n- ", 1)[0].split())
        api = _values_block(self.values["hindsight"]["api"]["resources"])
        postgres = _values_block(self.values["hindsight"]["postgresql"]["resources"])
        for text in (
            f"{_vcpu(api[0])} vCPU and {_gib(api[1])} GiB requests, {_vcpu(api[2])} vCPU and {_gib(api[3])} GiB limits",
            f"{_vcpu(postgres[0])} vCPU and {_decimal(postgres[1])} MiB requests, {_vcpu(postgres[2])} vCPU and {_gib(postgres[3])} GiB limits",
            f"{float(_gib(postgres[4])):.1f} GiB ephemeral",
            f"{_decimal(_mebibytes(self.values['hindsight']['postgresql']['storage']) / 1024)} GiB claim",
        ):
            with self.subTest(text=text):
                self.assertIn(text, paragraph)


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
