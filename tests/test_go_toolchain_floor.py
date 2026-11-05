"""Every go.mod selects the same Go, and none selects one older than Go 1.27.2.

The HTTP/1 client of Go 1.27.0 and 1.27.1 can leave a response body's reader
parked on a pooled connection after an early Close (golang/go#81404), so a
watch re-stream on that connection delivers nothing until the connection
closes. Go 1.27.2 fixes it. Each Go job in CI installs its Go with
actions/setup-go from the module's go.mod (`go-version-file`), which takes the
toolchain line when there is one and the go line otherwise. A toolchain line
dropped or lowered, by hand or by `go get toolchain@...`, therefore puts CI
back on the faulty net/http, and nothing else fails.

The toolchain line is a second pin beside the go line. setup-go installs the
toolchain line's Go and then sets GOTOOLCHAIN=local, under which the go command
refuses a module whose go line is newer than itself. `go mod edit -go` and
`go mod tidy` leave a toolchain line alone, so a go line raised by hand past it
breaks every Go job in CI while the floor still holds.

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


def _line(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _selected(text: str) -> str:
    """The Go setup-go installs for a go.mod: its toolchain line, else its go line."""
    return _line(TOOLCHAIN_LINE, text) or _line(GO_LINE, text)


def _release(version: str) -> tuple[int, int, int] | None:
    match = RELEASE.match(version)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


class GoToolchainFloorTest(unittest.TestCase):
    def setUp(self):
        self.texts = {name: (REPO_ROOT / name).read_text() for name in _go_mods()}
        self.assertIn(OPERATOR_GO_MOD, self.texts, "git ls-files found no go.mod to check")
        self.selected = {name: _selected(text) for name, text in self.texts.items()}

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

    def test_no_toolchain_line_is_older_than_its_go_line(self):
        for name, text in self.texts.items():
            toolchain = _line(TOOLCHAIN_LINE, text)
            if not toolchain:
                continue
            go = _line(GO_LINE, text)
            with self.subTest(go_mod=name):
                toolchain_release, go_release = _release(toolchain), _release(go)
                self.assertIsNotNone(toolchain_release, f"{name} has a toolchain line that names no Go release")
                self.assertIsNotNone(go_release, f"{name} has a toolchain line and no go line")
                self.assertGreaterEqual(
                    toolchain_release,
                    go_release,
                    f"{name}: toolchain go{toolchain} is older than the go line, go {go}. setup-go installs "
                    f"Go {toolchain} and sets GOTOOLCHAIN=local, and that Go refuses the module "
                    f"(go.mod requires go >= {go}), so every Go job in CI fails. Raise the toolchain "
                    f"line to at least go{go} (go mod edit -toolchain=go{go}), or drop it "
                    "(go mod edit -toolchain=none).",
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
