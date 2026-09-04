"""The mode spec's grep rule, executable.

docs/designs/spec-mode-switch.md: "A grep for KUBEAGENTS_MODE should hit
exactly two places: the operator builder that writes it and the helper that
reads it. A third hit is a review comment." One answer per agent, computed in
one place -- a component reading its own env var is the drift this catches.

Test files are excluded (they cover the two legitimate sites, and there the
literal is the expected value), as are docs, golden fixtures under testdata/,
and this file. Everything else that names the key is a new writer or reader
and fails here until the review conversation has happened.
"""

import pathlib
import unittest

KEY = "KUBEAGENTS_MODE"

ALLOWED = {
    "k8s-operator/internal/controller/platformagent_manifests.go",  # the writer
    "agents/platform/scripts/runtime_mode.py",  # the reader
}

# testdata: golden fixtures are recorded render output — the writer's own
# product, already covered by the exclusion of the tests that record them.
SKIP_DIRS = {".git", ".worktrees", "docs", "node_modules", "bin", "vendor", "__pycache__", "testdata"}


def _is_test_file(path: pathlib.Path) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.go")


class TestModeGrepRule(unittest.TestCase):
    def test_key_appears_in_exactly_the_writer_and_the_reader(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        hits = set()
        for path in repo.rglob("*"):
            rel = path.relative_to(repo)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if not path.is_file() or path.suffix == ".md" or _is_test_file(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if KEY in text:
                hits.add(str(rel))
        self.assertEqual(
            hits,
            ALLOWED,
            f"{KEY} must appear in exactly the operator builder that writes it "
            f"and the helper that reads it (spec-mode-switch.md). A new site is "
            f"a review conversation, not a grep-test edit made in passing.",
        )


if __name__ == "__main__":
    unittest.main()
