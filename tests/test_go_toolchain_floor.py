"""Every go.mod selects the same Go, and none selects one older than Go 1.27.2.

The HTTP/1 client of Go 1.27.0 and 1.27.1 can leave a response body's reader
parked on a pooled connection after an early Close (golang/go#81404), so a
watch re-stream on that connection delivers nothing until the connection
closes. Go 1.27.2 fixes it. Each Go job in CI installs its Go with
actions/setup-go from the module's go.mod (`go-version-file`), which takes the
toolchain line when there is one and the go line otherwise. A toolchain line
that a `go get` or `go mod tidy` drops or lowers therefore puts CI back on the
faulty net/http, and nothing else fails.

Run:
  python3 -m unittest discover -s tests -p 'test_go_toolchain_floor.py' -v
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The first release with golang/go#81404 fixed (CL 830424 on the 1.27 branch).
NET_HTTP_FIX_FLOOR = (1, 27, 2)
GO_MOD_PATHSPEC = ":(glob)**/go.mod"
# The module whose event-watcher tests stalled on the faulty net/http, named so
# that a discovery which finds nothing fails instead of passing.
OPERATOR_GO_MOD = "k8s-operator/go.mod"
TOOLCHAIN_LINE = re.compile(r"^toolchain\s+go(\S+)", re.MULTILINE)
GO_LINE = re.compile(r"^go\s+(\S+)", re.MULTILINE)
RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def _go_mods() -> list[str]:
    # Tracked files only, so a module cache or vendored copy in the checkout
    # is not read.
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", GO_MOD_PATHSPEC], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return sorted(name for name in out.split("\0") if name)


def _selected(text: str) -> str:
    """The Go setup-go installs for a go.mod: its toolchain line, else its go line."""
    match = TOOLCHAIN_LINE.search(text) or GO_LINE.search(text)
    return match.group(1) if match else ""


def _release(version: str) -> tuple[int, int, int] | None:
    match = RELEASE.match(version)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


class GoToolchainFloorTest(unittest.TestCase):
    def setUp(self):
        self.selected = {name: _selected((REPO_ROOT / name).read_text()) for name in _go_mods()}
        self.assertIn(OPERATOR_GO_MOD, self.selected, "git ls-files found no go.mod to check")

    def test_no_module_selects_a_go_without_the_net_http_fix(self):
        floor = ".".join(str(part) for part in NET_HTTP_FIX_FLOOR)
        for name, version in self.selected.items():
            with self.subTest(go_mod=name):
                release = _release(version)
                self.assertIsNotNone(release, f"{name} names no Go release in a toolchain or go line")
                self.assertGreaterEqual(
                    release,
                    NET_HTTP_FIX_FLOOR,
                    f"{name} selects Go {version}, older than {floor}: its net/http has golang/go#81404, "
                    f"and CI's setup-go installs that Go. Give it a toolchain line at or above go{floor}, "
                    "the same as every other go.mod.",
                )

    def test_every_module_selects_the_same_go(self):
        self.assertEqual(
            len(set(self.selected.values())),
            1,
            "the go.mod files select different Go versions, so their CI jobs test different "
            f"net/http code: {self.selected}",
        )

    def test_selection_matches_setup_go(self):
        # setup-go's parseGoVersionFile: the toolchain line wins over the go line.
        self.assertEqual(_selected("module x\n\ngo 1.27.0\n\ntoolchain go1.27.3\n"), "1.27.3")
        self.assertEqual(_selected("module x\n\ngo 1.27.3\n"), "1.27.3")
        self.assertEqual(_selected("module x\n"), "")
        self.assertEqual(_release("1.27"), (1, 27, 0))
        self.assertEqual(_release("1.28rc1"), (1, 28, 0))
        self.assertIsNone(_release("default"))


if __name__ == "__main__":
    unittest.main()
