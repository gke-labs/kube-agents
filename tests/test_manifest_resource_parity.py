"""Every rendered container declares its resources, and its scratch volumes fit under them.

The credential-proxy's 5Gi state volume shipped under a 2Gi ephemeral-storage
limit, so the kubelet evicted the pod the first time the broker filled the
volume (gke-labs/kube-agents#4314), and Autopilot clamps reach `main` unseen
because nothing reads the rendered manifests for resource consistency. This
test does, over every pod the repository renders (`_rendered_manifests`: the
chart through `helm template`, the operator goldens, the kustomize manifests)
and asserts, per container and init container:

- `resources-declared`: a limit for each of cpu, memory and ephemeral-storage,
  and a request or a limit (the API server copies a lone limit into the
  request, so a limit with no request is complete; a request with no limit is
  not, because Autopilot sets the limit equal to the request and a LimitRange
  elsewhere would default a smaller one);
- `container-emptydir-fit`: the sizeLimits of the disk-backed emptyDirs the
  container mounts writable, plus ALLOWANCE for its own logs and writable
  layer, fit under its ephemeral-storage limit. The kubelet charges a volume to
  the pod and evicts on the writer's usage, so a sidecar that mounts nothing
  owes nothing for the volumes it does not write; this is the per-container
  rule the operator sizes by (emptyDirLogsAllowance in
  k8s-operator/internal/controller/emptydir_limits.go, which ALLOWANCE mirrors
  and `test_allowance_matches_the_operator` pins);

and per pod, on the effective pod figures (the sum over the regular containers
and sidecar init containers, or the largest ordinary init container, whichever
is larger, as the scheduler computes them):

- `pod-emptydir-fit`: every disk-backed emptyDir's sizeLimit, plus ALLOWANCE,
  fits under the pod's ephemeral-storage limit;
- `pod-memory-per-vcpu`: requested memory per requested vCPU sits in the band
  GKE Autopilot admits unchanged, 1 to 6.5 GiB; outside it Autopilot raises the
  smaller request and the pod runs at a size nobody declared;
- `pod-ephemeral-request`: the ephemeral-storage request is at most 10Gi, the
  Autopilot general-purpose ceiling.

A `medium: Memory` emptyDir is charged to memory, not disk, and is excluded.

Today's violations are pinned in BASELINE, one entry per workload, container
and rule with the figure observed and the issue that removes it. The baseline
may only shrink: a violation not listed fails, a listed one no longer observed
fails until its line is removed, a larger figure fails, a smaller one fails
until the line is lowered to it, and the same figure with a different detail
(the missing declarations swapped for others at the same count) fails as a
changed violation, so the file never carries headroom a regression could
spend. `python3 tests/test_manifest_resource_parity.py` prints the entries as
observed, in the baseline's form, for that edit.

The chart half needs helm. On CI (`CI` set) a missing helm fails the gate
rather than passing it with the chart unread, as the rollout-quota suite
does; elsewhere `test_the_chart_renders_its_pods` shows the skip.

Run:
  python3 -m unittest discover -s tests -p 'test_manifest_resource_parity.py' -v
"""

from __future__ import annotations

import math
import os
import re
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _rendered_manifests import (  # noqa: E402
    HELM,
    REPO_ROOT,
    golden_docs,
    golden_pods,
    is_kustomize_patch,
    kustomize_pods,
    pod_specs,
    rendered_chart,
    workload_pods,
)

TESTDATA = _HERE / "testdata" / "manifest_resource_parity"
BASELINE = TESTDATA / "baseline.yaml"
BROKEN_FIXTURE = TESTDATA / "broken-emptydir.yaml"
# What `test_an_untracked_golden_is_read` writes to a directory git does not track.
UNTRACKED_GOLDEN_NAME = "platformagent-untracked.yaml"
# An editor's duplicate, as `test_a_golden_named_with_a_space_keeps_its_key` writes it.
SPACED_GOLDEN_NAME = "platformagent copy.yaml"
# Set on every GitHub Actions runner; with it a missing helm is a failure, not a skip.
CI_ENV = "CI"
NO_HELM_ON_CI = (
    "helm is not on PATH but CI is set: the chart half of this gate would pass with the chart"
    " unread on a runner. Install helm rather than skipping."
)
NO_HELM = "helm is not installed; the chart half of the gate is not read"

# The operator's allowance for a writing container's logs and writable layer
# on top of the emptyDir it writes; the Go constant is the source of truth.
ALLOWANCE = "64Mi"
OPERATOR_LIMITS_SOURCE = "k8s-operator/internal/controller/emptydir_limits.go"
OPERATOR_ALLOWANCE_RE = re.compile(r'^\s*emptyDirLogsAllowance\s*=\s*"([^"]+)"', re.MULTILINE)
# The operator's webhook warns on the same band; its constants are written as
# `<n> << <shift>` byte counts, which `test_memory_band_matches_the_operator` reads.
OPERATOR_BAND_SOURCE = "k8s-operator/internal/controller/credential_proxy_validation.go"
OPERATOR_BAND_RE = re.compile(r"^\s*autopilot(Min|Max)MemoryBytesPerVCPU\s+int64\s*=\s*(\d+)\s*<<\s*(\d+)", re.MULTILINE)

RESOURCE_KEYS = ("cpu", "memory", "ephemeral-storage")
EPHEMERAL = "ephemeral-storage"
CPU = "cpu"
MEMORY = "memory"
GIB = 1024**3
MIB = 1024**2
# GKE Autopilot's general-purpose compute class admits 1 to 6.5 GiB of memory
# per vCPU unchanged and raises the smaller request outside that band.
MEMORY_PER_VCPU_MIN = Fraction(1 * GIB)
MEMORY_PER_VCPU_MAX = Fraction(13 * GIB, 2)
# Autopilot's ceiling on a pod's ephemeral-storage request.
POD_EPHEMERAL_REQUEST_MAX = "10Gi"
MEMORY_MEDIUM = "Memory"
SIDECAR_RESTART_POLICY = "Always"

RULE_DECLARED = "resources-declared"
RULE_CONTAINER_EMPTYDIR = "container-emptydir-fit"
RULE_POD_EMPTYDIR = "pod-emptydir-fit"
RULE_POD_MEMORY_PER_VCPU = "pod-memory-per-vcpu"
RULE_POD_EPHEMERAL_REQUEST = "pod-ephemeral-request"
POD_SCOPE = ""

# A workload key opens with its source: `chart <Kind>/<name>`,
# `golden <Kind>/<name>`, or a kustomize manifest's path.
SOURCE_CHART = "chart"
SOURCE_GOLDEN = "golden"
SOURCE_KUSTOMIZE = "kustomize"
GOLDEN_WORKLOAD_PREFIX = f"{SOURCE_GOLDEN} "
CHART_WORKLOAD_PREFIX = f"{SOURCE_CHART} "

# The pods and containers rendered when this was written (2026-11-02): the
# chart's hindsight-api, litellm, controller-manager, github-token-minter,
# web-console, hindsight-postgresql and cr-cleanup; each of nine goldens' shell
# StatefulSet and credential-proxy, gateway and kube-proxy Deployments; the
# kustomize manager, litellm, hindsight api and postgres and inference-replay.
# A count under the floor means a source has gone blind, not that the tree got
# smaller.
MIN_CHART_PODS = 7
MIN_GOLDEN_PODS = 36
MIN_KUSTOMIZE_PODS = 5
MIN_CONTAINERS = 40

QUANTITY_RE = re.compile(r"^(?P<num>[+-]?(?:\d+(?:\.\d*)?|\.\d+))(?P<suffix>[KMGTPE]i|[kMGTPEm]|[eE][+-]?\d+)?$")
BINARY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
DECIMAL_SUFFIXES = {
    "m": Fraction(1, 1000),
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
}


def parse_quantity(value: object) -> Fraction:
    """A Kubernetes quantity as an exact number: bytes for storage, cores for CPU.

    The grammar is `resource.ParseQuantity`'s, which admits no whitespace, plus
    the API server's rule for a request, limit or sizeLimit: not negative."""
    if isinstance(value, bool):
        raise ValueError(f"not a quantity: {value!r}")
    if isinstance(value, (int, float)):
        number = Fraction(value)
    else:
        match = QUANTITY_RE.match(str(value))
        if match is None:
            raise ValueError(f"not a quantity: {value!r}")
        number = Fraction(match["num"])
        suffix = match["suffix"] or ""
        if suffix in BINARY_SUFFIXES:
            number *= BINARY_SUFFIXES[suffix]
        elif suffix in DECIMAL_SUFFIXES:
            number *= DECIMAL_SUFFIXES[suffix]
        elif suffix:
            number *= Fraction(10) ** int(suffix[1:])
    if number < 0:
        raise ValueError(f"negative quantity: {value!r}")
    return number


def fmt_bytes(value: Fraction) -> str:
    """A byte count in the largest binary unit that divides it, as the manifests write them."""
    for suffix, size in reversed(list(BINARY_SUFFIXES.items())):
        if value and value % size == 0:
            return f"{int(value // size)}{suffix}"
    return f"{math.ceil(value)}"


def fmt_cpu(value: Fraction) -> str:
    milli = value * 1000
    if milli.denominator == 1 and milli % 1000:
        return f"{int(milli)}m"
    return f"{float(value):g}"


@dataclass(frozen=True)
class Finding:
    """One rule a container or pod breaks; `measure` grows as it gets worse."""

    rule: str
    workload: str
    container: str
    measure: int
    detail: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.workload, self.container, self.rule)


@dataclass
class Pod:
    workload: str
    spec: dict
    source: str = ""
    file: str = ""


def _resources(container: dict) -> tuple[dict, dict]:
    resources = container.get("resources") or {}
    return resources.get("requests") or {}, resources.get("limits") or {}


def effective_request(container: dict, key: str) -> Fraction | None:
    """The request the API server records: the request, else the limit, else nothing."""
    requests, limits = _resources(container)
    value = requests.get(key, limits.get(key))
    return None if value is None else parse_quantity(value)


def effective_limit(container: dict, key: str) -> Fraction | None:
    _, limits = _resources(container)
    return None if limits.get(key) is None else parse_quantity(limits[key])


def _disk_emptydirs(pod: dict) -> dict[str, Fraction]:
    """volume name -> sizeLimit for every disk-backed emptyDir (a missing sizeLimit reads as 0)."""
    found = {}
    for volume in pod.get("volumes") or []:
        empty = volume.get("emptyDir")
        if empty is None:
            continue
        if (empty or {}).get("medium") == MEMORY_MEDIUM:
            continue
        limit = (empty or {}).get("sizeLimit")
        found[volume["name"]] = Fraction(0) if limit is None else parse_quantity(limit)
    return found


def _all_containers(pod: dict) -> list[tuple[dict, bool]]:
    """(container, is a regular or sidecar container) for every container in the pod."""
    out = [(c, True) for c in pod.get("containers") or []]
    for c in pod.get("initContainers") or []:
        out.append((c, c.get("restartPolicy") == SIDECAR_RESTART_POLICY))
    return out


def pod_effective(pod: dict, key: str, figure) -> Fraction | None:
    """The pod's figure for key: sum of the regular and sidecar containers, or the
    largest ordinary init container, whichever is larger; None when a container
    that counts towards it has no figure, since the pod total is then unbounded."""
    total = Fraction(0)
    init_max = Fraction(0)
    for container, counts_in_sum in _all_containers(pod):
        value = figure(container, key)
        if value is None:
            return None
        if counts_in_sum:
            total += value
        else:
            init_max = max(init_max, value)
    return max(total, init_max)


def _missing_declarations(container: dict) -> list[str]:
    requests, limits = _resources(container)
    missing = []
    for key in RESOURCE_KEYS:
        if limits.get(key) is None:
            missing.append(f"limits.{key}")
            if requests.get(key) is None:
                missing.append(f"requests.{key}")
    return missing


def container_findings(workload: str, pod: dict, container: dict) -> list[Finding]:
    name = container["name"]
    findings = []
    missing = _missing_declarations(container)
    if missing:
        findings.append(Finding(RULE_DECLARED, workload, name, len(missing), "missing " + ", ".join(missing)))
    limit = effective_limit(container, EPHEMERAL)
    disk = _disk_emptydirs(pod)
    # One volume mounted at two paths (the subPath pattern) is charged once, as the kubelet charges it.
    writable = list(
        dict.fromkeys(
            m["name"] for m in container.get("volumeMounts") or [] if m.get("name") in disk and not m.get("readOnly")
        )
    )
    if limit is not None and writable:
        total = sum(disk[v] for v in writable) + parse_quantity(ALLOWANCE)
        if total > limit:
            over = total - limit
            findings.append(
                Finding(
                    RULE_CONTAINER_EMPTYDIR,
                    workload,
                    name,
                    math.ceil(over),
                    f"writable emptyDirs {', '.join(writable)} sum {fmt_bytes(sum(disk[v] for v in writable))}"
                    f" + allowance {ALLOWANCE} = {fmt_bytes(total)}, over limits.ephemeral-storage"
                    f" {fmt_bytes(limit)} by {fmt_bytes(over)}",
                )
            )
    return findings


def pod_findings(workload: str, pod: dict) -> list[Finding]:
    findings = []
    disk = _disk_emptydirs(pod)
    limit = pod_effective(pod, EPHEMERAL, effective_limit)
    if disk and limit is not None:
        total = sum(disk.values()) + parse_quantity(ALLOWANCE)
        if total > limit:
            over = total - limit
            findings.append(
                Finding(
                    RULE_POD_EMPTYDIR,
                    workload,
                    POD_SCOPE,
                    math.ceil(over),
                    f"emptyDirs {', '.join(disk)} sum {fmt_bytes(sum(disk.values()))} + allowance {ALLOWANCE}"
                    f" = {fmt_bytes(total)}, over the pod's ephemeral-storage limit {fmt_bytes(limit)} by {fmt_bytes(over)}",
                )
            )
    cpu = pod_effective(pod, CPU, effective_request)
    memory = pod_effective(pod, MEMORY, effective_request)
    if cpu and memory is not None:
        ratio = memory / cpu
        outside = max(MEMORY_PER_VCPU_MIN - ratio, ratio - MEMORY_PER_VCPU_MAX)
        if outside > 0:
            findings.append(
                Finding(
                    RULE_POD_MEMORY_PER_VCPU,
                    workload,
                    POD_SCOPE,
                    math.ceil(outside / MIB),
                    f"{fmt_bytes(memory)} memory per {fmt_cpu(cpu)} CPU requested = {math.ceil(ratio / MIB)}Mi per vCPU;"
                    f" Autopilot admits {fmt_bytes(MEMORY_PER_VCPU_MIN)} to {fmt_bytes(MEMORY_PER_VCPU_MAX)}",
                )
            )
    request = pod_effective(pod, EPHEMERAL, effective_request)
    ceiling = parse_quantity(POD_EPHEMERAL_REQUEST_MAX)
    if request is not None and request > ceiling:
        findings.append(
            Finding(
                RULE_POD_EPHEMERAL_REQUEST,
                workload,
                POD_SCOPE,
                math.ceil(request - ceiling),
                f"pod requests {fmt_bytes(request)} ephemeral-storage, over Autopilot's {POD_EPHEMERAL_REQUEST_MAX}",
            )
        )
    return findings


def check_pod(workload: str, pod: dict) -> list[Finding]:
    findings = []
    for container, _ in _all_containers(pod):
        findings.extend(container_findings(workload, pod, container))
    findings.extend(pod_findings(workload, pod))
    return findings


def count_containers(pods: list[Pod]) -> int:
    return sum(len(_all_containers(p.spec)) for p in pods)


def split_workload(where: str) -> tuple[str, str]:
    """(file, `<Kind>/<name>`) from a `workload_pods` label; the file may hold a
    space (an editor's `platformagent copy.yaml`), a kind or a name cannot."""
    path, kind_name = where.rsplit(" ", 1)
    return path, kind_name


def rendered_pods() -> list[Pod]:
    """Every pod the three sources render, labelled by source and file."""
    pods = [Pod(w, s, SOURCE_CHART) for w, s in workload_pods(rendered_chart(), SOURCE_CHART)]
    for where, spec in golden_pods():
        # The goldens render the same workloads once per fixture; one key
        # covers them all and the file is kept for the message.
        path, kind_name = split_workload(where)
        pods.append(Pod(f"{GOLDEN_WORKLOAD_PREFIX}{kind_name}", spec, SOURCE_GOLDEN, path))
    for where, spec in kustomize_pods():
        path, _ = split_workload(where)
        pods.append(Pod(where, spec, SOURCE_KUSTOMIZE, path))
    return pods


@dataclass
class Observed:
    """A finding's worst observation across the files that render its workload."""

    finding: Finding
    files: list[str] = field(default_factory=list)


def observe(pods: list[Pod]) -> dict[tuple[str, str, str], Observed]:
    seen: dict[tuple[str, str, str], Observed] = {}
    for pod in pods:
        for finding in check_pod(pod.workload, pod.spec):
            entry = seen.get(finding.key)
            if entry is None:
                seen[finding.key] = Observed(finding, [pod.file] if pod.file else [])
            else:
                if pod.file and pod.file not in entry.files:
                    entry.files.append(pod.file)
                if finding.measure > entry.finding.measure:
                    entry.finding = finding
    return seen


def load_baseline(path: Path = BASELINE) -> dict[tuple[str, str, str], dict]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for entry in entries.get("entries") or []:
        key = (entry["workload"], entry.get("container") or POD_SCOPE, entry["rule"])
        if key in out:
            raise AssertionError(f"{path.relative_to(REPO_ROOT)} lists {key} twice")
        out[key] = entry
    return out


def _entry_line(finding: Finding, issue: object = None) -> str:
    container = f"\n    container: {finding.container}" if finding.container else ""
    return (
        f"  - workload: {finding.workload!r}{container}\n    rule: {finding.rule}\n"
        f"    measure: {finding.measure}\n    detail: {finding.detail!r}\n    issue: {issue if issue is not None else '<the tracking issue>'}"
    )


def source_of(workload: str) -> str:
    """The source a workload key names; a kustomize key is the manifest's path."""
    if workload.startswith(CHART_WORKLOAD_PREFIX):
        return SOURCE_CHART
    if workload.startswith(GOLDEN_WORKLOAD_PREFIX):
        return SOURCE_GOLDEN
    return SOURCE_KUSTOMIZE


def compare(observed: dict, baseline: dict, sources_read: set[str]) -> list[str]:
    """What the baseline and the observation disagree on, empty when they match."""
    problems = []
    for key, entry in sorted(observed.items()):
        finding = entry.finding
        where = f" (in {', '.join(entry.files)})" if entry.files else ""
        known = baseline.get(key)
        if known is None:
            problems.append(
                f"new violation, not in the baseline{where}: {finding.detail}. Fix the manifest; a baseline line"
                f" is for a violation an issue already owns:\n{_entry_line(finding)}"
            )
        elif finding.measure > int(known["measure"]):
            problems.append(
                f"worse than the baseline ({known['measure']} -> {finding.measure}){where}: {finding.detail}"
            )
        elif finding.measure < int(known["measure"]):
            problems.append(
                f"better than the baseline ({known['measure']} -> {finding.measure}){where}: lower the entry's"
                f" measure to {finding.measure} so the gain holds: {key}"
            )
        elif finding.detail != known.get("detail"):
            # The same count of a different thing: declarations swapped for others.
            problems.append(
                f"changed violation at the baseline's measure {finding.measure}{where}: now {finding.detail!r},"
                f" the baseline says {known.get('detail')!r}. Fix the manifest; the entry pins the detail as well as"
                f" the figure:\n{_entry_line(finding, known.get('issue'))}"
            )
    for key, entry in sorted(baseline.items()):
        if key in observed:
            continue
        if source_of(key[0]) not in sources_read:
            continue
        problems.append(f"baseline entry no longer observed; remove it: {key} (issue #{entry.get('issue')})")
    return problems


def _load_pod_fixture(path: Path) -> tuple[str, dict]:
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
    [(workload, spec)] = workload_pods(docs, path.name)
    return workload, spec


class ResourceParityGateTest(unittest.TestCase):
    """The gate itself: every rendered pod against the baseline."""

    longMessage = False

    def test_rendered_pods_match_the_baseline(self):
        if not HELM and os.environ.get(CI_ENV):
            self.fail(NO_HELM_ON_CI)
        pods = rendered_pods()
        by_source = {s: [p for p in pods if p.source == s] for s in (SOURCE_CHART, SOURCE_GOLDEN, SOURCE_KUSTOMIZE)}
        sources_read = {SOURCE_GOLDEN, SOURCE_KUSTOMIZE}
        if HELM:
            sources_read.add(SOURCE_CHART)
            self.assertGreaterEqual(len(by_source[SOURCE_CHART]), MIN_CHART_PODS, "the chart rendered too few pods; the check has gone blind")
        self.assertGreaterEqual(len(by_source[SOURCE_GOLDEN]), MIN_GOLDEN_PODS, "the goldens hold too few pods; the check has gone blind")
        self.assertGreaterEqual(len(by_source[SOURCE_KUSTOMIZE]), MIN_KUSTOMIZE_PODS, "the kustomize manifests hold too few pods; the check has gone blind")
        self.assertGreaterEqual(count_containers(pods), MIN_CONTAINERS, "too few containers read; the check has gone blind")
        problems = compare(observe(pods), load_baseline(), sources_read)
        self.assertEqual([], problems, "\n\n".join(problems))

    @unittest.skipUnless(HELM, NO_HELM)
    def test_the_chart_renders_its_pods(self):
        # Off CI a missing helm shows here as a skip; on CI it fails the gate above.
        self.assertGreaterEqual(len(workload_pods(rendered_chart(), SOURCE_CHART)), MIN_CHART_PODS, "the chart rendered too few pods; the check has gone blind")

    def test_a_patch_is_named_by_a_delimited_word(self):
        # The three patches in the tree are excluded; a stem that merely contains
        # the letters, or a whole object under an overlay, is read.
        self.assertTrue(is_kustomize_patch("k8s-operator/config/integrations/litellm/overlays/chatgpt/deployment-patch.yaml"))
        self.assertTrue(is_kustomize_patch("k8s-operator/config/default/webhookcainjection_patch.yaml"))
        self.assertFalse(is_kustomize_patch("k8s-operator/config/integrations/dispatcher.yaml"))
        self.assertFalse(is_kustomize_patch("k8s-operator/config/integrations/litellm/overlays/chatgpt/pvc.yaml"))

    def test_a_cronjob_patch_without_a_job_template_carries_no_pod(self):
        # A strategic-merge patch that suspends a CronJob has a spec and no jobTemplate.
        self.assertEqual([], pod_specs({"kind": "CronJob", "spec": {"suspend": True}}))
        template = {"spec": {"containers": []}}
        self.assertEqual([template["spec"]], pod_specs({"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": template}}}}))

    def test_an_untracked_golden_is_read(self):
        # `go test -update` writes a golden without staging it; the gate reads it before `git add`.
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / UNTRACKED_GOLDEN_NAME
            golden.write_text(BROKEN_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            [(path, docs)] = golden_docs(Path(tmp))
        self.assertEqual(str(golden), path)
        [(workload, spec)] = workload_pods(docs, SOURCE_GOLDEN)
        self.assertIn(RULE_CONTAINER_EMPTYDIR, {f.rule for f in check_pod(workload, spec)})

    def test_a_golden_named_with_a_space_keeps_its_key(self):
        # The file is read from disk whatever its name; the workload key is still
        # `golden <Kind>/<name>` and the file is kept whole for the message.
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / SPACED_GOLDEN_NAME
            golden.write_text(BROKEN_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            [(path, docs)] = golden_docs(Path(tmp))
        [(where, _)] = workload_pods(docs, path)
        self.assertEqual((str(golden), "Deployment/broken"), split_workload(where))

    def test_baseline_entries_name_a_rule_and_an_issue(self):
        rules = {RULE_DECLARED, RULE_CONTAINER_EMPTYDIR, RULE_POD_EMPTYDIR, RULE_POD_MEMORY_PER_VCPU, RULE_POD_EPHEMERAL_REQUEST}
        for key, entry in load_baseline().items():
            with self.subTest(entry=key):
                self.assertIn(entry["rule"], rules, f"{key}: unknown rule {entry['rule']!r}")
                self.assertIsInstance(entry.get("issue"), int, f"{key}: every baseline entry cites the issue that removes it")
                self.assertGreater(int(entry["measure"]), 0, f"{key}: a measure of 0 is not a violation")

    def test_allowance_matches_the_operator(self):
        text = (REPO_ROOT / OPERATOR_LIMITS_SOURCE).read_text(encoding="utf-8")
        match = OPERATOR_ALLOWANCE_RE.search(text)
        self.assertIsNotNone(match, f"{OPERATOR_LIMITS_SOURCE} declares no emptyDirLogsAllowance")
        self.assertEqual(ALLOWANCE, match.group(1), f"ALLOWANCE {ALLOWANCE} differs from the operator's emptyDirLogsAllowance {match.group(1)}")

    def test_memory_band_matches_the_operator(self):
        text = (REPO_ROOT / OPERATOR_BAND_SOURCE).read_text(encoding="utf-8")
        band = {edge: Fraction(int(n) << int(shift)) for edge, n, shift in OPERATOR_BAND_RE.findall(text)}
        self.assertEqual({"Min", "Max"}, set(band), f"{OPERATOR_BAND_SOURCE} declares no autopilotMin/MaxMemoryBytesPerVCPU in the `n << shift` form")
        self.assertEqual((MEMORY_PER_VCPU_MIN, MEMORY_PER_VCPU_MAX), (band["Min"], band["Max"]), "the band differs from the operator's")


class ResourceParityRulesTest(unittest.TestCase):
    """The rules on fixtures, independent of what the tree renders today."""

    longMessage = False

    def _clean_pod(self) -> dict:
        return {
            "containers": [
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "1Gi"},
                        "limits": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "2Gi"},
                    },
                    "volumeMounts": [{"name": "scratch", "mountPath": "/scratch"}],
                }
            ],
            "volumes": [{"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}}],
        }

    def test_broken_fixture_fails_the_emptydir_rules(self):
        workload, spec = _load_pod_fixture(BROKEN_FIXTURE)
        rules = {f.rule: f for f in check_pod(workload, spec)}
        self.assertIn(RULE_CONTAINER_EMPTYDIR, rules, f"the broken fixture passed the container rule: {rules}")
        self.assertIn(RULE_POD_EMPTYDIR, rules, f"the broken fixture passed the pod rule: {rules}")
        self.assertIn("state", rules[RULE_CONTAINER_EMPTYDIR].detail)
        # 5Gi + 64Mi over a 2Gi limit: 3Gi + 64Mi.
        self.assertEqual(rules[RULE_CONTAINER_EMPTYDIR].measure, 3 * GIB + 64 * MIB)
        self.assertEqual({RULE_CONTAINER_EMPTYDIR, RULE_POD_EMPTYDIR}, set(rules), f"unexpected findings: {rules}")

    def test_clean_pod_passes(self):
        self.assertEqual([], check_pod("w", self._clean_pod()))

    def test_a_limit_without_a_request_counts_as_the_request(self):
        pod = self._clean_pod()
        del pod["containers"][0]["resources"]["requests"]
        self.assertEqual([], check_pod("w", pod))

    def test_a_request_without_a_limit_is_undeclared(self):
        pod = self._clean_pod()
        del pod["containers"][0]["resources"]["limits"]["ephemeral-storage"]
        [finding] = check_pod("w", pod)
        self.assertEqual((RULE_DECLARED, 1, "missing limits.ephemeral-storage"), (finding.rule, finding.measure, finding.detail))

    def test_a_memory_medium_volume_is_excluded(self):
        pod = self._clean_pod()
        pod["volumes"].append({"name": "shm", "emptyDir": {"medium": MEMORY_MEDIUM, "sizeLimit": "10Gi"}})
        pod["containers"][0]["volumeMounts"].append({"name": "shm", "mountPath": "/dev/shm"})
        self.assertEqual([], check_pod("w", pod))

    def test_a_volume_mounted_twice_is_charged_once(self):
        pod = self._clean_pod()
        # 1.5Gi + 64Mi fits under 2Gi once; summed per mount it would not.
        pod["volumes"][0]["emptyDir"]["sizeLimit"] = "1536Mi"
        pod["containers"][0]["volumeMounts"].append({"name": "scratch", "mountPath": "/scratch/cache", "subPath": "cache"})
        self.assertEqual([], check_pod("w", pod))
        pod["volumes"][0]["emptyDir"]["sizeLimit"] = "2Gi"
        rules = {f.rule: f for f in check_pod("w", pod)}
        self.assertIn(RULE_CONTAINER_EMPTYDIR, rules)
        detail = rules[RULE_CONTAINER_EMPTYDIR].detail
        self.assertEqual(1, detail.count("scratch"), f"the volume is named more than once: {detail}")

    def test_a_read_only_mount_owes_nothing_but_the_pod_still_does(self):
        pod = self._clean_pod()
        pod["volumes"].append({"name": "big", "emptyDir": {"sizeLimit": "5Gi"}})
        pod["containers"][0]["volumeMounts"].append({"name": "big", "mountPath": "/big", "readOnly": True})
        [finding] = check_pod("w", pod)
        self.assertEqual(RULE_POD_EMPTYDIR, finding.rule)

    def test_memory_per_vcpu_is_checked_on_the_pod_total(self):
        pod = self._clean_pod()
        # A second container at 20 GiB per vCPU on its own; the pod sits at 3.4 GiB per vCPU.
        pod["containers"].append(
            {
                "name": "fat",
                "resources": {
                    "requests": {"cpu": "100m", "memory": "2Gi", "ephemeral-storage": "64Mi"},
                    "limits": {"cpu": "100m", "memory": "2Gi", "ephemeral-storage": "64Mi"},
                },
            }
        )
        self.assertEqual([], check_pod("w", pod))
        del pod["containers"][0]
        del pod["volumes"]
        [finding] = check_pod("w", pod)
        self.assertEqual(RULE_POD_MEMORY_PER_VCPU, finding.rule)
        self.assertIn("20480Mi per vCPU", finding.detail)

    def test_pod_ephemeral_request_ceiling(self):
        pod = self._clean_pod()
        pod["containers"][0]["resources"]["requests"]["ephemeral-storage"] = "11Gi"
        pod["containers"][0]["resources"]["limits"]["ephemeral-storage"] = "11Gi"
        rules = {f.rule for f in check_pod("w", pod)}
        self.assertEqual({RULE_POD_EPHEMERAL_REQUEST}, rules)

    def test_an_ordinary_init_container_is_not_summed(self):
        pod = self._clean_pod()
        pod["initContainers"] = [
            {
                "name": "setup",
                "resources": {"requests": {"cpu": "100m", "memory": "100Mi"}, "limits": {"cpu": "100m", "memory": "100Mi", "ephemeral-storage": "10Gi"}},
            }
        ]
        # The init container's 10Gi is the pod's effective request (larger than the 1Gi sum), at the ceiling.
        self.assertEqual([], check_pod("w", pod))
        pod["initContainers"][0]["restartPolicy"] = SIDECAR_RESTART_POLICY
        # As a sidecar it is summed: 10Gi + 1Gi is over the ceiling.
        self.assertEqual({RULE_POD_EPHEMERAL_REQUEST}, {f.rule for f in check_pod("w", pod)})

    def test_quantities_parse_as_kubernetes_does(self):
        self.assertEqual(parse_quantity("1Gi"), GIB)
        self.assertEqual(parse_quantity("1536Mi"), Fraction(3 * GIB, 2))
        self.assertEqual(parse_quantity("500m"), Fraction(1, 2))
        self.assertEqual(parse_quantity("1.5"), Fraction(3, 2))
        self.assertEqual(parse_quantity("2e3"), 2000)
        self.assertEqual(parse_quantity("1k"), 1000)
        self.assertEqual(parse_quantity(2), 2)
        for rejected in ("1GiB", " 1Gi ", "-1Gi", -1):
            with self.subTest(value=rejected), self.assertRaises(ValueError):
                parse_quantity(rejected)


class ResourceParityRatchetTest(unittest.TestCase):
    longMessage = False

    def _observed(self, measure: int) -> dict:
        finding = Finding(RULE_DECLARED, "golden Deployment/x", "c", measure, "missing limits.cpu")
        return {finding.key: Observed(finding, ["a.yaml"])}

    def _baseline(self, measure: int, detail: str = "missing limits.cpu") -> dict:
        return {
            ("golden Deployment/x", "c", RULE_DECLARED): {
                "workload": "golden Deployment/x",
                "container": "c",
                "rule": RULE_DECLARED,
                "measure": measure,
                "detail": detail,
                "issue": 1,
            }
        }

    def test_a_matching_entry_passes(self):
        self.assertEqual([], compare(self._observed(2), self._baseline(2), {SOURCE_GOLDEN}))

    def test_an_unlisted_violation_fails(self):
        [problem] = compare(self._observed(2), {}, {SOURCE_GOLDEN})
        self.assertIn("new violation", problem)
        self.assertIn("workload: 'golden Deployment/x'", problem)

    def test_an_unobserved_entry_fails(self):
        [problem] = compare({}, self._baseline(2), {SOURCE_GOLDEN})
        self.assertIn("no longer observed; remove it", problem)

    def test_an_unobserved_kustomize_entry_fails(self):
        key = ("k8s-operator/config/manager/manager.yaml Deployment/controller-manager", "manager", RULE_DECLARED)
        [problem] = compare({}, {key: {"measure": 2, "issue": 1}}, {SOURCE_GOLDEN, SOURCE_KUSTOMIZE})
        self.assertIn("no longer observed; remove it", problem)

    def test_a_workload_key_names_its_source(self):
        self.assertEqual(SOURCE_CHART, source_of("chart Deployment/litellm"))
        self.assertEqual(SOURCE_GOLDEN, source_of("golden Deployment/platformagent-gateway"))
        self.assertEqual(SOURCE_KUSTOMIZE, source_of("k8s-operator/config/manager/manager.yaml Deployment/controller-manager"))

    def test_an_unobserved_entry_of_a_source_not_read_is_left_alone(self):
        baseline = {("chart Deployment/x", "c", RULE_DECLARED): {"measure": 2, "issue": 1}}
        self.assertEqual([], compare({}, baseline, {SOURCE_GOLDEN}))

    def test_a_worse_figure_fails(self):
        [problem] = compare(self._observed(3), self._baseline(2), {SOURCE_GOLDEN})
        self.assertIn("worse than the baseline (2 -> 3)", problem)

    def test_a_better_figure_fails_until_lowered(self):
        [problem] = compare(self._observed(1), self._baseline(2), {SOURCE_GOLDEN})
        self.assertIn("lower the entry's measure to 1", problem)

    def test_a_changed_detail_at_the_same_measure_fails(self):
        # One missing declaration swapped for another: the count holds, the violation moved.
        [problem] = compare(self._observed(2), self._baseline(2, "missing limits.memory"), {SOURCE_GOLDEN})
        self.assertIn("changed violation at the baseline's measure 2", problem)
        self.assertIn("now 'missing limits.cpu', the baseline says 'missing limits.memory'", problem)


class CredentialProxyGoldenTest(unittest.TestCase):
    """The credential-proxy pod fits at the default limit and at an override above it."""

    longMessage = False

    DEFAULT_GOLDEN = "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml"
    OVERRIDE_GOLDEN = "k8s-operator/internal/testing/testdata/platform/expected/platformagent-credential-proxy-resources.yaml"
    OVERRIDE_FIXTURE = "k8s-operator/internal/testing/testdata/platform/platformagent-credential-proxy-resources.yaml"
    PROXY_SUFFIX = "-credential-proxy"
    PROXY_CONTAINER = "envoy-credential-proxy"
    STATE_VOLUME = "credential-proxy-state"
    DEFAULT_LIMIT = "8Gi"

    def _proxy_pod(self, golden: str) -> dict:
        docs = [d for d in yaml.safe_load_all((REPO_ROOT / golden).read_text(encoding="utf-8")) if isinstance(d, dict)]
        [(_, spec)] = [(w, s) for w, s in workload_pods(docs, golden) if w.endswith(self.PROXY_SUFFIX)]
        return spec

    def _proxy_container(self, pod: dict) -> dict:
        [container] = [c for c in pod["containers"] if c["name"] == self.PROXY_CONTAINER]
        return container

    def test_fits_at_the_default_limit(self):
        pod = self._proxy_pod(self.DEFAULT_GOLDEN)
        self.assertEqual(self.DEFAULT_LIMIT, self._proxy_container(pod)["resources"]["limits"][EPHEMERAL])
        emptydir = [f for f in check_pod("proxy", pod) if f.rule in (RULE_CONTAINER_EMPTYDIR, RULE_POD_EMPTYDIR)]
        self.assertEqual([], emptydir, f"the default credential-proxy render does not fit: {emptydir}")

    def test_fits_at_the_override_and_the_state_volume_grew(self):
        fixture = yaml.safe_load((REPO_ROOT / self.OVERRIDE_FIXTURE).read_text(encoding="utf-8"))
        override = fixture["spec"]["deployment"]["credentialProxy"]["resources"]["limits"][EPHEMERAL]
        self.assertGreater(parse_quantity(override), parse_quantity(self.DEFAULT_LIMIT), "the fixture overrides below the default")
        pod = self._proxy_pod(self.OVERRIDE_GOLDEN)
        self.assertEqual(override, self._proxy_container(pod)["resources"]["limits"][EPHEMERAL], "the golden does not carry the override")
        default_state = _disk_emptydirs(self._proxy_pod(self.DEFAULT_GOLDEN))[self.STATE_VOLUME]
        grown_state = _disk_emptydirs(pod)[self.STATE_VOLUME]
        self.assertEqual(
            grown_state - default_state,
            parse_quantity(override) - parse_quantity(self.DEFAULT_LIMIT),
            f"the state volume grew by {fmt_bytes(grown_state - default_state)}, not by the override's headroom",
        )
        emptydir = [f for f in check_pod("proxy", pod) if f.rule in (RULE_CONTAINER_EMPTYDIR, RULE_POD_EMPTYDIR)]
        self.assertEqual([], emptydir, f"the overridden credential-proxy render does not fit: {emptydir}")


def _print_observed() -> None:
    """The violations as rendered now, in the baseline's form."""
    baseline = load_baseline() if BASELINE.exists() else {}
    for key, entry in sorted(observe(rendered_pods()).items()):
        known = baseline.get(key) or {}
        print(_entry_line(entry.finding, known.get("issue")))
        print(f"    # in {', '.join(entry.files) or 'the chart'}")


if __name__ == "__main__":
    _print_observed()
