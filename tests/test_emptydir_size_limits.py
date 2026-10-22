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

The checks read source, so each one also counts what it read: a scan that
matched nothing would pass, and the floors below fail it instead.

Run:
  python3 -m unittest discover -s tests -p 'test_emptydir_size_limits.py' -v
"""

from __future__ import annotations

import re
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

# YAML: the chart's templates and the operator's kustomize config. The CRD
# schemas name `emptyDir` as a property, not a volume, so they are skipped.
YAML_ROOTS = ("charts/kube-agents/templates", "k8s-operator/config")
YAML_SUFFIXES = (".yaml", ".yml", ".tpl")
YAML_SKIP_PARTS = ("crds", "crd")
EMPTYDIR_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?emptyDir:\s*(?P<rest>.*)$")
SIZE_LIMIT_KEY = re.compile(r"^\s*sizeLimit:\s*\S")
SIZE_LIMIT_FLOW = re.compile(r"\bsizeLimit\s*:\s*[^\s,}]")
# The emptyDir volumes in those files when this was written: 7.
MIN_YAML_EMPTYDIRS = 7

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


def _yaml_emptydirs() -> list[tuple[str, int, bool]]:
    """(file, line, has a sizeLimit) for each emptyDir volume in the YAML roots."""
    found = []
    for path in _tracked(YAML_ROOTS):
        rel = path.relative_to(REPO_ROOT)
        if not rel.name.endswith(YAML_SUFFIXES) or any(part in YAML_SKIP_PARTS for part in rel.parts):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            match = EMPTYDIR_KEY.match(line)
            if not match:
                continue
            # A YAML comment never carries the key; a template action may hold a `#`.
            rest = match.group("rest")
            if not rest.lstrip().startswith("{{"):
                rest = rest.split("#", 1)[0].strip()
            if rest:
                # The flow form: `emptyDir: {}` or `emptyDir: {sizeLimit: 1Gi}`.
                found.append((rel.as_posix(), i + 1, bool(SIZE_LIMIT_FLOW.search(rest))))
                continue
            # The block form: the keys indented under it, skipping template
            # directives and comments, up to the first line back at its level.
            indent = len(match.group("indent")) + (2 if line.lstrip().startswith("-") else 0)
            has_limit = False
            for following in lines[i + 1 :]:
                stripped = following.strip()
                if not stripped or stripped.startswith(("#", "{{")):
                    continue
                if len(following) - len(following.lstrip()) <= indent:
                    break
                if SIZE_LIMIT_KEY.match(following):
                    has_limit = True
            found.append((rel.as_posix(), i + 1, has_limit))
    return found


class EmptyDirSizeLimitTest(unittest.TestCase):
    longMessage = False

    def test_every_go_rendered_emptydir_has_a_size_limit(self):
        found = _go_emptydirs()
        self.assertGreaterEqual(
            len(found), MIN_GO_EMPTYDIRS, f"read only {len(found)} EmptyDirVolumeSource literals; the scan has gone blind"
        )
        unbounded = [f"{name}:{line}" for name, line, body in found if not SIZE_LIMIT_FIELD.search(body)]
        self.assertEqual([], unbounded, f"emptyDir without a SizeLimit: {unbounded}")

    def test_every_chart_and_kustomize_emptydir_has_a_size_limit(self):
        found = _yaml_emptydirs()
        self.assertGreaterEqual(
            len(found), MIN_YAML_EMPTYDIRS, f"read only {len(found)} emptyDir volumes in YAML; the scan has gone blind"
        )
        unbounded = [f"{name}:{line}" for name, line, has_limit in found if not has_limit]
        self.assertEqual([], unbounded, f"emptyDir without a sizeLimit: {unbounded}")

    def test_no_per_task_pod_mounts_a_claim(self):
        for name in PER_TASK_BUILDERS:
            with self.subTest(builder=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertGreater(len(text), MIN_FILE_CHARS, f"{name} read back suspiciously short")
                self.assertIn(EMPTYDIR_LITERAL, text, f"{name} no longer renders the emptyDir this test expects")
                for marker in PVC_MARKERS:
                    self.assertNotIn(marker, text, f"{name} names a {marker}: a per-task pod gets no claim")


if __name__ == "__main__":
    unittest.main()
