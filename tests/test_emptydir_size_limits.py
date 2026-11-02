"""Every emptyDir the product renders has a sizeLimit, and no per-task pod gets a PVC.

An emptyDir without a sizeLimit can fill the node's ephemeral storage, and the
kubelet then evicts whichever pod it ranks worst, which need not be the writer.
With one, the eviction is the writer's pod, for a stated reason, at a known
threshold. Seven of the operator's and gateway's emptyDirs and four in the chart
had none until the round-4 sizing work measured each and bounded it
(gke-labs/kube-agents#3753). PersistentVolumeClaims are created at install
time only: a pod spawned per task or per session (the a2a session pod, the
profile pod) mounts no claim, because on Autopilot a claim per task is a
billed disk per task and a scale-up per burst. Only the session pod has a
builder in the tree today: k8s-operator/internal/profilepod renders volumes and
mounts for a pod template it does not own, and the profile dispatcher's
Deployment or Job (gke-labs/kube-agents#3655) joins PER_TASK_BUILDERS when it
lands.

The YAML side checks rendered objects: the chart through `helm template` (with
Hindsight on, so its StatefulSet renders), and the kustomize manifests parsed as
YAML, which they are with no templating. Every emptyDir there must carry a
sizeLimit that parses as a positive quantity, which is the condition the kubelet
enforces on. The Go side is a source scan, lint-level: it catches an
`EmptyDirVolumeSource` literal with no `SizeLimit` key (comments stripped), and
`new(...)` or zero-value `var` forms. It does not evaluate the value, so
`SizeLimit: somePointerThatIsNil` or a zero quantity passes it; the builders'
own tests (spawn_test.go, profilepod_test.go, emptydir_limits_test.go and the
operator goldens) render the real objects and pin each figure. Each check also
counts what it read: a check that matched nothing would pass, and the floors
below fail it instead.

Run:
  python3 -m unittest discover -s tests -p 'test_emptydir_size_limits.py' -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Go: the operator and the a2a components render pods; test files build fixtures.
GO_ROOTS = ("a2a", "k8s-operator")
GO_SUFFIX = ".go"
GO_TEST_SUFFIX = "_test.go"
EMPTYDIR_LITERAL = "EmptyDirVolumeSource{"
SIZE_LIMIT_FIELD = re.compile(r"\bSizeLimit:\s*(?!nil\b)\S")
# Comments are stripped before the field is looked for, so a comment naming it
# does not stand in for it.
GO_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
# Forms that build an emptyDir with no literal to carry a SizeLimit.
UNBOUNDED_GO_FORMS = re.compile(r"new\(\s*(?:corev1\.)?EmptyDirVolumeSource\s*\)|var\s+\w+\s+(?:corev1\.)?EmptyDirVolumeSource\b")
# The literals in tree when this was written (2026-10-22): 12.
MIN_GO_EMPTYDIRS = 12

# YAML: the chart, rendered, and the operator's kustomize manifests, parsed. The
# CRD schemas name `emptyDir` as a property, not a volume, so they are skipped.
HELM = shutil.which("helm")
CHART = "charts/kube-agents"
CHART_SETS = (
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-east4",
    "platformAgent.harness.projectId=p",
    "hindsight.enabled=true",
)
KUSTOMIZE_ROOT = "k8s-operator/config"
YAML_SUFFIXES = (".yaml", ".yml")
YAML_SKIP_PARTS = ("crds", "crd")
QUANTITY = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?:Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?$")
# The emptyDir volumes rendered when this was written (2026-10-22): the chart's
# LiteLLM tmp, Hindsight run and tmp and the cleanup Job's cache; the kustomize
# copies of LiteLLM tmp and Hindsight run and tmp.
MIN_CHART_EMPTYDIRS = 4
MIN_KUSTOMIZE_EMPTYDIRS = 3

# The builders of per-task and per-session pods (see the module docstring).
PER_TASK_BUILDERS = ("a2a/gateway/spawn.go",)
PVC_MARKERS = ("PersistentVolumeClaim", "persistentVolumeClaim")
MIN_FILE_CHARS = 1000


def _tracked(roots: tuple[str, ...]) -> list[Path]:
    # Tracked files only: a checked-out node_modules or a build directory
    # would otherwise be scanned too.
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *roots], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [REPO_ROOT / name for name in sorted(out.split("\0")) if name]


def _literal_body(text: str, start: int) -> str:
    """The text of the brace-balanced composite literal opening at text[start]."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError("unbalanced EmptyDirVolumeSource literal")


def _go_emptydirs() -> list[tuple[str, int, str]]:
    found = []
    for path in _tracked(GO_ROOTS):
        name = path.relative_to(REPO_ROOT).as_posix()
        if not name.endswith(GO_SUFFIX) or name.endswith(GO_TEST_SUFFIX):
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(re.escape(EMPTYDIR_LITERAL), text):
            brace = match.end() - 1
            line = text.count("\n", 0, match.start()) + 1
            found.append((name, line, GO_COMMENTS.sub("", _literal_body(text, brace))))
        for match in UNBOUNDED_GO_FORMS.finditer(text):
            # No literal: recorded with an empty body, so it reads as unbounded.
            found.append((name, text.count("\n", 0, match.start()) + 1, ""))
    return found


def _pod_specs(doc: dict) -> list[dict]:
    """The pod specs a workload object carries, whatever its kind."""
    spec = doc.get("spec") or {}
    if doc.get("kind") == "Pod":
        return [spec]
    if "jobTemplate" in spec:
        return [spec["jobTemplate"]["spec"]["template"]["spec"]]
    template = (spec.get("template") or {}).get("spec")
    return [template] if template else []


def _emptydirs(docs, where: str) -> list[tuple[str, object]]:
    """(object/volume, sizeLimit) for every emptyDir volume in the documents."""
    found = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for pod in _pod_specs(doc):
            for volume in pod.get("volumes") or []:
                if "emptyDir" in volume:
                    name = f"{where} {doc.get('kind')}/{doc.get('metadata', {}).get('name')} {volume.get('name')}"
                    found.append((name, (volume.get("emptyDir") or {}).get("sizeLimit")))
    return found


def _positive_quantity(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    match = QUANTITY.match(str(value).strip())
    return bool(match) and float(match["num"]) > 0


def _rendered_chart() -> list:
    import yaml

    args = [HELM, "template", "t", str(REPO_ROOT / CHART)]
    for value in CHART_SETS:
        args += ["--set", value]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return list(yaml.safe_load_all(out))


def _kustomize_docs() -> list[tuple[str, list]]:
    import yaml

    out = []
    for path in _tracked((KUSTOMIZE_ROOT,)):
        rel = path.relative_to(REPO_ROOT)
        if not rel.name.endswith(YAML_SUFFIXES) or any(part in YAML_SKIP_PARTS for part in rel.parts):
            continue
        out.append((rel.as_posix(), list(yaml.safe_load_all(path.read_text(encoding="utf-8")))))
    return out


class EmptyDirSizeLimitTest(unittest.TestCase):
    longMessage = False

    def test_every_go_rendered_emptydir_has_a_size_limit(self):
        found = _go_emptydirs()
        self.assertGreaterEqual(
            len(found), MIN_GO_EMPTYDIRS, f"read only {len(found)} EmptyDirVolumeSource literals; the scan has gone blind"
        )
        unbounded = [f"{name}:{line}" for name, line, body in found if not SIZE_LIMIT_FIELD.search(body)]
        self.assertEqual([], unbounded, f"emptyDir without a SizeLimit: {unbounded}")

    @unittest.skipUnless(HELM, "helm is not installed")
    def test_every_rendered_chart_emptydir_has_a_positive_size_limit(self):
        found = _emptydirs(_rendered_chart(), "chart")
        self.assertGreaterEqual(
            len(found), MIN_CHART_EMPTYDIRS, f"rendered only {len(found)} emptyDir volumes from the chart; the check has gone blind"
        )
        unbounded = [f"{name} (sizeLimit {limit!r})" for name, limit in found if not _positive_quantity(limit)]
        self.assertEqual([], unbounded, f"emptyDir without a positive sizeLimit: {unbounded}")

    def test_every_kustomize_emptydir_has_a_positive_size_limit(self):
        found = [entry for path, docs in _kustomize_docs() for entry in _emptydirs(docs, path)]
        self.assertGreaterEqual(
            len(found), MIN_KUSTOMIZE_EMPTYDIRS, f"read only {len(found)} emptyDir volumes from the kustomize manifests; the check has gone blind"
        )
        unbounded = [f"{name} (sizeLimit {limit!r})" for name, limit in found if not _positive_quantity(limit)]
        self.assertEqual([], unbounded, f"emptyDir without a positive sizeLimit: {unbounded}")

    def test_no_per_task_pod_mounts_a_claim(self):
        for name in PER_TASK_BUILDERS:
            with self.subTest(builder=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertGreater(len(text), MIN_FILE_CHARS, f"{name} read back suspiciously short")
                self.assertIn(EMPTYDIR_LITERAL, text, f"{name} no longer renders the emptyDir this test expects")
                for marker in PVC_MARKERS:
                    self.assertNotIn(marker, text, f"{name} names a {marker}: a per-task pod gets no claim")


# The YAML-rendered writers: each requests ephemeral storage with an equal limit,
# and the chart and the kustomize copy agree. The cleanup Job's figure is also
# summed by the quota preflight from a dict of its own, which must match.
CHART_VALUES = "charts/kube-agents/values.yaml"
KUSTOMIZE_LITELLM = "k8s-operator/config/integrations/litellm/base/deployment.yaml"
KUSTOMIZE_POSTGRES = "k8s-operator/config/integrations/hindsight/postgresql.yaml"
CLEANUP_TEMPLATE = "charts/kube-agents/templates/platform-agent-cr-cleanup.yaml"
PREFLIGHT_HELPERS = "charts/kube-agents/templates/_helpers.tpl"
EPHEMERAL = "ephemeral-storage"
LITELLM_EPHEMERAL = "1280Mi"
POSTGRES_EPHEMERAL = "1152Mi"
CLEANUP_EPHEMERAL = "1088Mi"
CLEANUP_BLOCK = re.compile(
    r"requests:\s*\n(?:[^\n]*\n)*?\s*ephemeral-storage:\s*(?P<req>\S+)\s*\n\s*limits:\s*\n(?:[^\n]*\n)*?\s*ephemeral-storage:\s*(?P<lim>\S+)"
)
CLEANUP_PREFLIGHT = re.compile(
    r'\$cleanupRes := dict "resources" \(dict "requests" \(dict [^)]*"ephemeral-storage" "(?P<req>[^"]+)"\) "limits" \(dict [^)]*"ephemeral-storage" "(?P<lim>[^"]+)"\)'
)


def _yaml_container(path: str, kind: str, name: str) -> dict:
    import yaml

    docs = [d for d in yaml.safe_load_all((REPO_ROOT / path).read_text(encoding="utf-8")) if d]
    for doc in docs:
        if doc.get("kind") == kind:
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == name:
                    return c["resources"]
    raise AssertionError(f"no {kind} container {name!r} in {path}")


class YamlWriterEphemeralTest(unittest.TestCase):
    longMessage = False

    def _assert_pair(self, where: str, resources: dict, want: str) -> None:
        req = resources.get("requests", {}).get(EPHEMERAL)
        lim = resources.get("limits", {}).get(EPHEMERAL)
        self.assertEqual((req, lim), (want, want), f"{where}: ephemeral-storage request/limit {req}/{lim}, want {want}/{want}")

    def test_chart_and_kustomize_writers_request_with_an_equal_limit(self):
        import yaml

        values = yaml.safe_load((REPO_ROOT / CHART_VALUES).read_text(encoding="utf-8"))
        self._assert_pair("chart litellm.resources", values["litellm"]["resources"], LITELLM_EPHEMERAL)
        self._assert_pair("chart hindsight.postgresql.resources", values["hindsight"]["postgresql"]["resources"], POSTGRES_EPHEMERAL)
        self._assert_pair("kustomize litellm", _yaml_container(KUSTOMIZE_LITELLM, "Deployment", "litellm-container"), LITELLM_EPHEMERAL)
        self._assert_pair("kustomize hindsight postgres", _yaml_container(KUSTOMIZE_POSTGRES, "StatefulSet", "postgresql"), POSTGRES_EPHEMERAL)

    def test_cleanup_job_and_its_preflight_entry_agree(self):
        template = (REPO_ROOT / CLEANUP_TEMPLATE).read_text(encoding="utf-8")
        match = CLEANUP_BLOCK.search(template)
        self.assertIsNotNone(match, "cleanup Job resources block not found")
        self.assertEqual((match["req"], match["lim"]), (CLEANUP_EPHEMERAL, CLEANUP_EPHEMERAL), "cleanup Job ephemeral request/limit")
        preflight = CLEANUP_PREFLIGHT.search((REPO_ROOT / PREFLIGHT_HELPERS).read_text(encoding="utf-8"))
        self.assertIsNotNone(preflight, "the quota preflight's $cleanupRes dict not found")
        self.assertEqual((preflight["req"], preflight["lim"]), (match["req"], match["lim"]), "the preflight sums a different cleanup figure from the one the Job requests")


if __name__ == "__main__":
    unittest.main()
