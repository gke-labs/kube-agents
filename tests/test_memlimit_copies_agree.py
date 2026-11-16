"""a2a/lib/memlimit is a copy of k8s-operator/internal/memlimit, and this holds the two bodies equal.

The bus module's binaries (hermes-bridge, the consoles) cannot import the operator's internal
package, so the parser that turns a Downward API byte count into a Go soft memory limit lives
twice. A fix to one copy that does not reach the other would have the bridge and the manager
derive different limits from the same manifest, with no test red; so from the `package` line
down, the two files have to read the same. The package comments above it differ on purpose:
each names its own callers.

Run: python3 -m unittest discover -s tests -p 'test_memlimit_copies_agree.py' -v
"""

import pathlib
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_OPERATOR_COPY = _REPO_ROOT / "k8s-operator" / "internal" / "memlimit" / "memlimit.go"
_BUS_COPY = _REPO_ROOT / "a2a" / "lib" / "memlimit" / "memlimit.go"
_PACKAGE_LINE = "package memlimit\n"


def _body(path):
    text = path.read_text()
    assert _PACKAGE_LINE in text, f"{path} has no {_PACKAGE_LINE.strip()} line"
    return text.split(_PACKAGE_LINE, 1)[1]


class MemlimitCopiesAgreeTest(unittest.TestCase):
    def test_the_two_bodies_are_identical(self):
        self.assertEqual(
            _body(_OPERATOR_COPY),
            _body(_BUS_COPY),
            f"{_BUS_COPY} is a copy of {_OPERATOR_COPY}; change both, the bridge and the manager "
            "derive their soft limits through the same parser",
        )

    def test_each_header_names_the_other(self):
        self.assertIn("a2a/lib/memlimit", _OPERATOR_COPY.read_text().split(_PACKAGE_LINE, 1)[0])
        self.assertIn("internal/memlimit", _BUS_COPY.read_text().split(_PACKAGE_LINE, 1)[0])


if __name__ == "__main__":
    unittest.main()
