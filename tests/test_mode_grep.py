"""The retired mode key has one writer and no reader, executable.

The operator still pins KUBEAGENTS_MODE=next into the managed .env for an
agent image from before spec.mode today was removed; nothing in the current
tree reads it. A second hit is a new writer or a new reader, which is the
drift this catches: the key goes away with the pin, not back into use.

Test files are excluded (there the literal is the expected value), as are
docs, golden fixtures under testdata/, and this file.
"""

import pathlib
import unittest

KEY = "KUBEAGENTS_MODE"

ALLOWED = {
    "k8s-operator/internal/controller/platformagent_manifests.go",  # the writer
}

# testdata: golden fixtures are recorded render output — the writer's own
# product, already covered by the exclusion of the tests that record them.
SKIP_DIRS = {".git", ".worktrees", "docs", "node_modules", "bin", "vendor", "__pycache__", "testdata"}


def _is_test_file(path: pathlib.Path) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.go")


class TestModeGrepRule(unittest.TestCase):
    def test_key_appears_in_exactly_the_writer(self):
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
            f"and no reader (the agent image dropped runtime_mode.py). A new site is "
            f"a review conversation, not a grep-test edit made in passing.",
        )


if __name__ == "__main__":
    unittest.main()
