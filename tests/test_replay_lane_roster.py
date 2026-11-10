"""hack/eval/replay-lane-cases.txt: what the replay lane replays.

The lane replays what the nightly recorded, through the inject door the nightly
grades on, and gates nothing, so every entry must be a registered case the
inject lane runs and must not request a pull request (a write the lane would
make for real on a replayed answer, with no case to grade it).
"""

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_EVAL = _ROOT / "hack" / "eval"
_LANE = _EVAL / "replay-lane-cases.txt"
_ENTRY_RE = re.compile(r"^\./tasks/([^/]+)/task\.yaml$")
_PULL_REQUEST_CHECKS = ("pull_request_opened", "pull_request_diff_contains")


def _entries(path):
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


class ReplayLaneRosterTest(unittest.TestCase):
    def setUp(self):
        self.entries = _entries(_LANE)

    def test_it_names_cases(self):
        self.assertTrue(self.entries)
        self.assertEqual(len(self.entries), len(set(self.entries)), "no case twice")
        for entry in self.entries:
            with self.subTest(entry=entry):
                self.assertRegex(entry, _ENTRY_RE)
                self.assertTrue((_ROOT / "bench" / entry).is_file())

    def test_every_case_is_one_the_nightly_records(self):
        registered = set(_entries(_EVAL / "presubmit-cases.txt")) | set(_entries(_EVAL / "nightly-cases.txt"))
        self.assertEqual([e for e in self.entries if e not in registered], [])

    def test_every_case_runs_on_the_inject_lane(self):
        excluded = set(_entries(_EVAL / "inject-lane-exclusions.txt"))
        names = [_ENTRY_RE.match(e).group(1) for e in self.entries]
        self.assertEqual([n for n in names if n in excluded], [])

    def test_no_case_requests_a_pull_request(self):
        for entry in self.entries:
            with self.subTest(entry=entry):
                text = (_ROOT / "bench" / entry).read_text()
                self.assertFalse(any(check in text for check in _PULL_REQUEST_CHECKS))


if __name__ == "__main__":
    unittest.main()
