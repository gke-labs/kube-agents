"""No image the repository builds carries the prototype agent SDK harness.

The worker image used to install @anthropic-ai/claude-agent-sdk and run its
native binary as the session and profile worker's harness. That harness was
removed (#6707): the worker refuses to start until another harness is
available. These tests keep it out of every image: no Dockerfile installs an
@anthropic-ai package or copies a vendored harness tree, and the package
directory the worker image installed it from is gone.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SDK_PACKAGE = re.compile(r"@anthropic-ai/claude-(agent-sdk|code)")
_REMOVED_DIR = _REPO_ROOT / "a2a" / "worker-harness"


def _dockerfiles() -> list[Path]:
    return sorted(
        p
        for p in _REPO_ROOT.rglob("Dockerfile*")
        if p.is_file() and "node_modules" not in p.parts and ".git" not in p.parts
    )


class NoImageCarriesTheSdkHarness(unittest.TestCase):
    def test_there_are_dockerfiles_to_check(self) -> None:
        self.assertIn(_REPO_ROOT / "a2a" / "Dockerfile.worker", _dockerfiles())

    def test_no_dockerfile_installs_the_sdk_or_copies_its_package_tree(self) -> None:
        for path in _dockerfiles():
            text = path.read_text(encoding="utf-8")
            with self.subTest(dockerfile=str(path.relative_to(_REPO_ROOT))):
                self.assertIsNone(_SDK_PACKAGE.search(text), "installs the removed SDK package")
                self.assertNotIn("worker-harness", text, "copies the removed harness package tree")

    def test_the_harness_package_directory_is_gone(self) -> None:
        self.assertFalse(_REMOVED_DIR.exists(), f"{_REMOVED_DIR.relative_to(_REPO_ROOT)} is back")


if __name__ == "__main__":
    unittest.main()
