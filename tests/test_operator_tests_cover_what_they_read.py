"""Operator Tests runs on every change to a file outside the module that its Go tests read.

`k8s-operator-test.yml` filters on paths (the push `paths:` list and the
`operator:` filter, which test_push_paths_match_change_filter.py keeps equal).
An operator test that reads a file outside `k8s-operator/` through a relative
path, such as the chart under `charts/kube-agents/`, depends on that file, but a
pull request that changes only the file skips the job. The break then lands on
the next operator pull request, which goes red on a change it did not make
(#6249). This holds the filter to every repository directory those tests reach
into with `../` paths.
"""

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "k8s-operator-test.yml"
OPERATOR = REPO_ROOT / "k8s-operator"

# The repository directories an operator test reaches through a relative path,
# keyed by the filter glob that covers them. A new outside read that is in
# neither this map nor _KNOWN_UNCOVERED fails test_every_outside_read_is_listed.
_COVERED_READS = {
    "charts/kube-agents": "charts/kube-agents/**",
    "a2a/authcallout/testdata": "a2a/authcallout/testdata/**",
    "a2a/lib/testdata": "a2a/lib/testdata/**",
    "a2a/dispatch/testdata": "a2a/dispatch/testdata/**",
}

# Directories operator tests read today that the filter does not cover.
# Covering them would run this job on most a2a pull requests, so the plan
# (#6249) is to move these reads into module-local fixtures instead (follow-up
# #6252); listing them here keeps the set from growing silently. Remove
# an entry when its reads move or it moves into _COVERED_READS.
_KNOWN_UNCOVERED = (
    "a2a/cmd",
    "a2a/gateway",
    "a2a/hermes-bridge",
    "a2a/lib",
    "agents",
    "deploy/shared",
    "docs/designs",
    "examples",
    "hack",
)

# A string literal that climbs to the repository root, as in
# `"../../../charts/kube-agents/files/flagged-pods.yaml"`; group 1 is the
# repository path after the climb. Tests sit at least three levels below the
# root (k8s-operator/internal/controller), so three or more `../` leave the
# module, and the climb can end in any top-level directory.
# Interpreted ("...") and raw (`...`) string literals both count.
_RELATIVE_READ = re.compile(r"""["'`](?:\.\./){3,}([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*)""")
# The same climb written as filepath.Join("..", "..", "..", "deploy", "shared", ...):
# group 1 is the quoted parts after the last "..". A part may itself hold a
# slash (`"charts/kube-agents/files/x.yaml"`). A variable part ends the match,
# so the read is recorded only up to it; the listed-directory check then names
# that prefix, which is loud rather than silent.
_JOINED_READ = re.compile(r"""filepath\.Join\(\s*(?:"\.\."\s*,\s*){3,}((?:"[A-Za-z0-9_.\-/]+"\s*,?\s*)+)""")


def _operator_filter() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["test"]["steps"]:
        filters = (step.get("with") or {}).get("filters")
        if filters:
            return yaml.safe_load(filters)["operator"]
    raise AssertionError("no operator: paths filter in k8s-operator-test.yml")


def _reads_in(text: str) -> set[str]:
    reads = {match.group(1) for match in _RELATIVE_READ.finditer(text)}
    for match in _JOINED_READ.finditer(text):
        reads.add("/".join(re.findall(r'"([^"]+)"', match.group(1))))
    return reads


def _outside_reads() -> set[str]:
    reads = set()
    for test in OPERATOR.rglob("*_test.go"):
        reads |= _reads_in(test.read_text())
    return reads


class TheScanSeesEachSpelling(unittest.TestCase):
    """The scan itself: each ordinary Go spelling of a climb out of the module is seen."""

    def test_spellings(self):
        cases = {
            '"../../../charts/kube-agents/files/a.yaml"': "charts/kube-agents/files/a.yaml",
            "`../../../charts/kube-agents/files/a.yaml`": "charts/kube-agents/files/a.yaml",
            'filepath.Join("..", "..", "..", "deploy", "shared", "s.sh")': "deploy/shared/s.sh",
            'filepath.Join("..", "..", "..", "charts/kube-agents/files/a.yaml")': "charts/kube-agents/files/a.yaml",
            'filepath.Join("..", "..", "..", "a2a", subdir, "testdata")': "a2a",
        }
        for spelling, read in cases.items():
            with self.subTest(spelling=spelling):
                self.assertIn(read, _reads_in(spelling))

    def test_an_in_module_path_is_not_a_read(self):
        self.assertEqual(_reads_in('"../testdata/x.json" "../../config/rbac/role.yaml"'), set())


class OperatorTestsCoverWhatTheyRead(unittest.TestCase):
    def test_the_filter_covers_each_listed_read(self):
        operator = _operator_filter()
        for read, glob in _COVERED_READS.items():
            with self.subTest(read=read):
                self.assertIn(glob, operator, f"operator tests read {read}/ but the filter does not run them on it")

    def test_every_outside_read_is_listed(self):
        reads = _outside_reads()
        self.assertTrue(reads, "found no relative reads at all; the pattern no longer matches the tests")
        for read in sorted(reads):
            with self.subTest(read=read):
                self.assertTrue(
                    any(read == listed or read.startswith(listed + "/") for listed in (*_COVERED_READS, *_KNOWN_UNCOVERED)),
                    f"an operator test reads {read} through a relative path; add its directory to "
                    "_COVERED_READS and to both path lists in k8s-operator-test.yml",
                )


if __name__ == "__main__":
    unittest.main()
