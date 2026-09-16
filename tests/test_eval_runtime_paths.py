"""`REVALIDATION_RUNTIME_PATHS` covers everything that reaches a run.

`hack/ci-eval-pr.sh` skips the eval matrix when a delta touches none of those
paths. That is an allowlist, so it fails open: a path nobody added permits the
skip. These checks derive what belongs on it from the sources, so the list
going stale reds a test instead of silently revalidating.

Two ways in are covered -- a `COPY` in a root-context Dockerfile, and a
`scripts/` entry the eval drivers read. Not covered: a new top-level directory
that reaches a run some other way, and `k8s-operator/Dockerfile`, whose
sub-context makes its sources non-repository-relative.

stdlib unittest on purpose: `tests/` is swept by `python3 -m unittest
discover`, which collects nothing from a pytest-native module.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVAL_SCRIPT = REPO / "hack" / "ci-eval-pr.sh"
# Dockerfiles built from the repository root, so their COPY sources are paths
# the allowlist can be checked against.
ROOT_CONTEXT_DOCKERFILES = ("deploy/docker/Dockerfile", "deploy/sandbox/Dockerfile")
# Scripts the Prow job runs, and what a scripts/ reference looks like in them.
EVAL_DRIVERS = ("ci-eval-pr.sh", "ci-deploy.sh", "ci-env.sh", "ci-teardown.sh")
SCRIPTS_REFERENCE = re.compile(r"scripts/([A-Za-z0-9_.-]+)")
ASSIGNMENT = re.compile(r"^readonly REVALIDATION_RUNTIME_PATHS='(?P<pattern>.+)'$", re.M)
COPY_LINE = re.compile(r"^\s*(?:COPY|ADD)\s+(?P<rest>.+)$", re.M)
CONTINUATION = re.compile(r"\\\s*\n\s*")

MEASURED = (
    "agents/platform/SOUL.md",  # a prompt
    "charts/kube-agents/values.yaml",  # the deployed config
    "bench/tasks/agent-kanban-smoke/task.yaml",  # what grades it
    "hack/ci-eval-pr.sh",  # this gate forces its own full run
    "tags.env",  # a bare file, not a directory prefix
    "scripts/installer/common.sh",  # a covered scripts/ subtree
    "scripts/eval_rosters.py",  # reached only as an import, never named
)
UNMEASURED = (
    "docs/README.md",
    "tests/test_eval_runtime_paths.py",
    "terraform/modules/gke-cluster/main.tf",
    "scripts/dev/dev_rebuild_agent.sh",  # an uncovered scripts/ subtree
    # Root-anchored: must not ride a prefix they merely start with.
    "agents-notes.md",
    "docs/agents/persona.md",
    "images.json.bak",
)


def pattern() -> str:
    match = ASSIGNMENT.search(EVAL_SCRIPT.read_text())
    assert match, "REVALIDATION_RUNTIME_PATHS is not assigned in hack/ci-eval-pr.sh"
    return match.group("pattern")


def measured(path: str) -> bool:
    """Ask the way the gate asks: grep -E, not Python re."""
    return subprocess.run(["grep", "-Eq", pattern()], input=path, text=True).returncode == 0


def copy_sources(dockerfile: Path) -> list[str]:
    text = CONTINUATION.sub(" ", dockerfile.read_text())
    sources: list[str] = []
    for match in COPY_LINE.finditer(text):
        rest = match.group("rest")
        if "--from=" in rest:
            continue  # stage-to-stage: names no repository path
        words = [word for word in rest.split() if not word.startswith("--")]
        sources.extend(words[:-1])  # the last word is the destination
    return sources


class RuntimePathsTest(unittest.TestCase):
    def test_every_dockerfile_copy_is_covered(self) -> None:
        """A COPY is how source reaches the image the eval deploys."""
        for name in ROOT_CONTEXT_DOCKERFILES:
            with self.subTest(dockerfile=name):
                sources = copy_sources(REPO / name)
                self.assertTrue(sources, f"{name}: no COPY sources; the parser has drifted")
                self.assertEqual(
                    sorted({src for src in sources if not measured(src)}),
                    [],
                    f"{name} copies paths the allowlist misses, so edits to them would "
                    "revalidate against a green that never built them",
                )

    def test_every_scripts_entry_the_drivers_read_is_covered(self) -> None:
        """scripts/ is covered in part, so what the drivers reach is derived."""
        referenced: set[str] = set()
        for name in EVAL_DRIVERS:
            script = REPO / "hack" / name
            if not script.is_file():
                continue
            for line in script.read_text().splitlines():
                # Skip comments, and the allowlist's own definition: reading
                # that back would make this agree with itself by construction.
                if line.lstrip().startswith("#") or "REVALIDATION_RUNTIME_PATHS" in line:
                    continue
                referenced.update(SCRIPTS_REFERENCE.findall(line.split("#", 1)[0]))
        self.assertTrue(referenced, "no scripts/ references; the parser has drifted")
        self.assertEqual(
            sorted(entry for entry in referenced if not measured(f"scripts/{entry}/")),
            [],
            "hack/ci-*.sh reads scripts/ entries the allowlist misses",
        )

    def test_measured_paths_force_a_full_run(self) -> None:
        for path in MEASURED:
            with self.subTest(path=path):
                self.assertTrue(measured(path), f"{path} should force a full run")

    def test_unmeasured_paths_allow_revalidation(self) -> None:
        for path in UNMEASURED:
            with self.subTest(path=path):
                self.assertFalse(measured(path), f"{path} should not force a full run")


if __name__ == "__main__":
    unittest.main()
