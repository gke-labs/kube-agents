"""The pods the repository renders, from the three places they come from.

Shared by the manifest tests under tests/ that read rendered objects rather
than templates: `test_emptydir_size_limits.py` (every emptyDir carries a
sizeLimit) and `test_manifest_resource_parity.py` (every container declares its
resources and its scratch volumes fit under them). Three sources:

- the chart, through `helm template` with Hindsight on so its StatefulSet
  renders (`rendered_chart`; empty when helm is not installed, and the caller
  decides whether that is a skip);
- the operator goldens, `k8s-operator/internal/testing/testdata/platform/expected/`,
  which are the reconciler's output for one PlatformAgent each, parsed as YAML
  (`golden_docs`). The directory holds nothing but goldens and is read from
  disk, tracked or not: `go test -update` writes a new golden without staging
  it, and a reader that listed tracked files would pass locally and go red in
  CI at `git add`;
- the kustomize manifests under `k8s-operator/config`, parsed as YAML, which
  they are with no templating (`kustomize_docs`). The CRD schemas name
  `emptyDir` and `resources` as properties, not as a volume or a container, so
  they are skipped; a strategic-merge patch (an `overlays/` file, or one named
  `*patch*`) carries a partial container and is skipped by `kustomize_pods`,
  which reads whole objects only.

`pod_specs` finds the pod template inside a workload of any kind, and
`workload_pods` labels each one with where it came from, so a failure names
the object.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

HELM = shutil.which("helm")
CHART = "charts/kube-agents"
CHART_RELEASE = "t"
CHART_SETS = (
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-east4",
    "platformAgent.harness.projectId=p",
    "hindsight.enabled=true",
)

GOLDEN_DIR = "k8s-operator/internal/testing/testdata/platform/expected"
KUSTOMIZE_ROOT = "k8s-operator/config"
YAML_SUFFIXES = (".yaml", ".yml")
YAML_SKIP_PARTS = ("crds", "crd")
# A kustomize overlay holds strategic-merge patches: a Deployment document that
# names one container with the fields the overlay changes and nothing else.
KUSTOMIZE_PATCH_PARTS = ("overlays",)
KUSTOMIZE_PATCH_MARK = "patch"

# The workload kinds whose spec carries a pod template, and the field it sits under.
POD_KIND = "Pod"
CRONJOB_KIND = "CronJob"


def tracked(roots: tuple[str, ...]) -> list[Path]:
    """The tracked files under roots, sorted; an untracked build directory is not read."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *roots], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [REPO_ROOT / name for name in sorted(out.split("\0")) if name]


def rendered_chart(extra_sets: tuple[str, ...] = ()) -> list:
    """The chart's documents under CHART_SETS plus extra_sets; [] without helm."""
    if not HELM:
        return []
    args = [HELM, "template", CHART_RELEASE, str(REPO_ROOT / CHART)]
    for value in (*CHART_SETS, *extra_sets):
        args += ["--set", value]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)]


def _yaml_files(paths: list[Path], skip_parts: tuple[str, ...] = ()) -> list[tuple[str, list]]:
    """(path, documents) for each YAML file in paths; the path is repository-relative when it can be."""
    out = []
    for path in paths:
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        if not rel.name.endswith(YAML_SUFFIXES) or any(part in skip_parts for part in rel.parts):
            continue
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
        out.append((rel.as_posix(), docs))
    return out


def golden_docs(root: Path = REPO_ROOT / GOLDEN_DIR) -> list[tuple[str, list]]:
    """(path, documents) for each operator golden file in root, whether git tracks it or not."""
    return _yaml_files(sorted(path for path in root.iterdir() if path.is_file()))


def kustomize_docs() -> list[tuple[str, list]]:
    """(path, documents) for each tracked kustomize manifest, overlays and patches included."""
    return _yaml_files(tracked((KUSTOMIZE_ROOT,)), YAML_SKIP_PARTS)


def is_kustomize_patch(path: str) -> bool:
    parts = Path(path).parts
    return any(part in KUSTOMIZE_PATCH_PARTS for part in parts) or KUSTOMIZE_PATCH_MARK in parts[-1]


def pod_specs(doc: dict) -> list[dict]:
    """The pod specs a workload object carries, whatever its kind."""
    spec = doc.get("spec") or {}
    if doc.get("kind") == POD_KIND:
        return [spec]
    if doc.get("kind") == CRONJOB_KIND or "jobTemplate" in spec:
        return [spec["jobTemplate"]["spec"]["template"]["spec"]]
    template = (spec.get("template") or {}).get("spec")
    return [template] if template else []


def workload_pods(docs: list, where: str) -> list[tuple[str, dict]]:
    """(`<where> <Kind>/<name>`, pod spec) for every pod template in docs."""
    found = []
    for doc in docs:
        if not isinstance(doc, dict) or "kind" not in doc:
            continue
        name = f"{where} {doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}"
        found.extend((name, pod) for pod in pod_specs(doc))
    return found


def kustomize_pods() -> list[tuple[str, dict]]:
    """The pods of the whole kustomize objects, patches excluded."""
    found = []
    for path, docs in kustomize_docs():
        if not is_kustomize_patch(path):
            found.extend(workload_pods(docs, path))
    return found


def golden_pods() -> list[tuple[str, dict]]:
    found = []
    for path, docs in golden_docs():
        found.extend(workload_pods(docs, path))
    return found
