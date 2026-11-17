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
}

# Directories operator tests read today that the filter does not cover. Whether
# to widen the filter to them (an operator run on every a2a change) or to move
# the reads is still open on #6249; listing them here keeps that set from
# growing silently. Remove an entry when it moves into _COVERED_READS.
_KNOWN_UNCOVERED = (
    "a2a/cmd",
    "a2a/dispatch",
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
_RELATIVE_READ = re.compile(r"""["'](?:\.\./){3,}([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*)""")
# The same climb written as filepath.Join("..", "..", "..", "deploy", "shared", ...):
# group 1 is the quoted parts after the last "..".
_JOINED_READ = re.compile(r"""filepath\.Join\(\s*(?:"\.\."\s*,\s*){3,}((?:"[A-Za-z0-9_.\-]+"\s*,?\s*)+)""")


def _operator_filter() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["test"]["steps"]:
        filters = (step.get("with") or {}).get("filters")
        if filters:
            return yaml.safe_load(filters)["operator"]
    raise AssertionError("no operator: paths filter in k8s-operator-test.yml")


def _outside_reads() -> set[str]:
    reads = set()
    for test in OPERATOR.rglob("*_test.go"):
        text = test.read_text()
        for match in _RELATIVE_READ.finditer(text):
            reads.add(match.group(1))
        for match in _JOINED_READ.finditer(text):
            reads.add("/".join(re.findall(r'"([^"]+)"', match.group(1))))
    return reads


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
