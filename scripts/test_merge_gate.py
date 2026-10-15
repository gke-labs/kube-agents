#!/usr/bin/env python3
"""Tests for merge_gate.py -- the merge queue's gate checks.

Run: cd scripts && python3 -m unittest test_merge_gate

The two wrong answers cost differently. A gate that passes when Tide would
not merges a pull request that is unapproved or unsmoked; a gate
that holds when Tide would merge leaves a ready pull request out of the queue
with nothing on it saying why. So each verdict gets both sides, the smoke
skip list is checked against the paths Prow skips, and the queue branch is
parsed from the shape GitHub names it.
"""

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import merge_gate as gate

REPO = "gke-labs/kube-agents"
SHA = "a" * 40
BASE_SHA = "b" * 40
APPROVED = {"lgtm", "approved", "size/M"}


def pull(labels=APPROVED, statuses=None, base="main"):
    return {
        "number": 7,
        "sha": SHA,
        "base": base,
        "labels": set(labels),
        "statuses": statuses or {},
    }


def green(*contexts):
    return {context: ("success", "") for context in contexts}


def no_files():
    raise AssertionError("the file list was read when it was not needed")


class FakeAPI:
    """Just enough of `GitHubAPI` for the functions that call it."""

    def __init__(self, raw_pull=None, statuses=(), files=()):
        self.repo = REPO
        self.raw_pull = raw_pull
        self.statuses = list(statuses)
        self.files = list(files)
        self.posts = []

    def get(self, path, tolerate=()):
        if path.endswith("/status?per_page=100"):
            return {"statuses": self.statuses}
        return self.raw_pull

    def get_all(self, path, per_page=100):
        if path.endswith("/files"):
            return self.files
        return [self.raw_pull]

    def post(self, path, payload=None, tolerate=()):
        self.posts.append((path, payload))


class LabelsTest(unittest.TestCase):
    def test_both_labels_and_no_hold_pass(self):
        self.assertEqual(gate.labels_verdict(APPROVED)[0], gate.SUCCESS)

    def test_a_missing_label_waits_and_names_it(self):
        state, description = gate.labels_verdict({"approved"})
        self.assertEqual(state, gate.PENDING)
        self.assertIn("lgtm", description)

    def test_each_label_tide_excludes_holds(self):
        for hold in ("do-not-merge", "do-not-merge/hold", "do-not-merge/invalid-owners-file", "do-not-merge/work-in-progress"):
            with self.subTest(label=hold):
                state, description = gate.labels_verdict(APPROVED | {hold})
                self.assertEqual(state, gate.PENDING)
                self.assertIn(hold, description)

    def test_a_do_not_merge_label_tide_does_not_exclude_does_not_hold(self):
        """Holding on it would keep a pull request out of the queue that Tide merges today."""
        for label in ("do-not-merge/release-note", "do-not-merge-ish"):
            with self.subTest(label=label):
                self.assertEqual(gate.labels_verdict(APPROVED | {label})[0], gate.SUCCESS)


class SmokeTest(unittest.TestCase):
    def test_a_green_smoke_passes_without_reading_files(self):
        results = gate.verdicts(pull(statuses=green(gate.SMOKE_CONTEXT)), no_files)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_red_smoke_fails(self):
        statuses = {gate.SMOKE_CONTEXT: ("failure", "Job failed.")}
        results = gate.verdicts(pull(statuses=statuses), lambda: ["a2a/main.go"])
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.FAILURE)

    def test_a_running_smoke_waits(self):
        statuses = {gate.SMOKE_CONTEXT: ("pending", "Job triggered.")}
        results = gate.verdicts(pull(statuses=statuses), lambda: ["a2a/main.go"])
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.PENDING)

    def test_no_smoke_on_a_change_it_runs_for_waits(self):
        results = gate.verdicts(pull(), lambda: ["docs/a.md", "agents/chat/SOUL.md"])
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.PENDING)

    def test_no_smoke_on_skipped_paths_passes(self):
        """Prow posts no smoke status on these, and Tide does not wait for one."""
        files = ["docs/a.md", ".github/workflows/x.yml", "README.md", "scripts/test_x.py", "OWNERS", "install.sh"]
        results = gate.verdicts(pull(), lambda: files)
        self.assertEqual(results[gate.SMOKE_GATE][0], gate.SUCCESS)

    def test_a_red_or_running_smoke_on_skipped_paths_still_decides(self):
        """A hand-run `/test` posts a smoke status on a skip-only change; Tide holds on its red."""
        for state, expected in (("failure", gate.FAILURE), ("pending", gate.PENDING)):
            with self.subTest(state=state):
                statuses = {gate.SMOKE_CONTEXT: (state, "")}
                results = gate.verdicts(pull(statuses=statuses), lambda: ["docs/a.md"])
                self.assertEqual(results[gate.SMOKE_GATE][0], expected)

    def test_the_skip_list_matches_prows_reading(self):
        for path in ("tests/x.py", "examples/a/b.yaml", "scripts/release/x.sh", "scripts/eval_dashboard/x.py", "LICENSE"):
            with self.subTest(path=path):
                self.assertFalse(gate.smoke_required([path]))
        for path in ("scripts/x.py", "scripts/sub/test_x.py", "docs.md/x", "agents/a.md", "install.sh.bak"):
            with self.subTest(path=path):
                self.assertTrue(gate.smoke_required([path]))

    def test_an_overlong_file_list_needs_the_smoke_and_an_empty_one_does_not(self):
        """Prow skips a `skip_if_only_changed` job when no file changed."""
        self.assertTrue(gate.smoke_required(["docs/a.md"] * gate.FILES_CEILING))
        self.assertFalse(gate.smoke_required([]))

    def test_a_rename_counts_by_its_new_path_as_prow_reads_it(self):
        """Prow posts no smoke status for a move into a skipped path, so the gate must not wait for one."""
        api = FakeAPI(files=[{"filename": "docs/x.py", "previous_filename": "scripts/x.py"}])
        self.assertEqual(gate.changed_files(api, 7), ["docs/x.py"])
        self.assertFalse(gate.smoke_required(gate.changed_files(api, 7)))


class PostTest(unittest.TestCase):
    def test_only_changed_statuses_are_posted(self):
        statuses = green(gate.SMOKE_CONTEXT) | {gate.SMOKE_GATE: ("success", f"{gate.SMOKE_CONTEXT} passed")}
        api = FakeAPI()
        gate.gate_pull(api, pull(statuses=statuses))
        self.assertEqual(sorted(payload["context"] for _, payload in api.posts), [gate.LABELS_GATE])
        self.assertTrue(all(path == f"/repos/{REPO}/statuses/{SHA}" for path, _ in api.posts))

    def test_a_non_green_verdict_is_not_posted_over_nothing(self):
        """Tide holds on any non-green context, so a first pending would hold a Tide merge."""
        api = FakeAPI()
        gate.gate_pull(api, pull({"lgtm"}, statuses=green(gate.SMOKE_CONTEXT)))
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.SMOKE_GATE])

    def test_a_non_green_verdict_does_not_replace_a_green(self):
        """Nothing may re-post it once the hold goes, and the queue judges the labels again anyway."""
        statuses = green(gate.SMOKE_CONTEXT, gate.LABELS_GATE)
        api = FakeAPI()
        gate.gate_pull(api, pull(APPROVED | {"do-not-merge/hold"}, statuses=statuses))
        self.assertNotIn(gate.LABELS_GATE, [payload["context"] for _, payload in api.posts])

    def test_a_non_green_verdict_does_not_replace_a_non_green(self):
        statuses = green(gate.SMOKE_CONTEXT) | {gate.LABELS_GATE: ("pending", "held by do-not-merge/hold")}
        api = FakeAPI()
        gate.gate_pull(api, pull({"approved"}, statuses=statuses))
        self.assertNotIn(gate.LABELS_GATE, [payload["context"] for _, payload in api.posts])

    def test_a_dry_run_posts_nothing(self):
        api = FakeAPI()
        lines = gate.gate_pull(api, pull(statuses=green(gate.SMOKE_CONTEXT)), dry_run=True)
        self.assertEqual(api.posts, [])
        self.assertTrue(any("would post" in line for line in lines))

    def test_a_release_line_is_left_to_tide(self):
        api = FakeAPI()
        gate.gate_pull(api, pull(base="release/1.2"))
        self.assertEqual(api.posts, [])

    def test_the_dry_run_branch_is_gated_like_main(self):
        api = FakeAPI()
        gate.gate_pull(api, pull({"lgtm"}, statuses=green(gate.SMOKE_CONTEXT), base=gate.DRY_RUN_BASE))
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.SMOKE_GATE])

    def test_a_status_event_gates_the_pull_request_with_that_head(self):
        raw = {"number": 7, "head": {"sha": SHA}, "base": {"ref": "main"}, "labels": [{"name": "lgtm"}]}
        api = FakeAPI(raw_pull=raw, statuses=[{"context": gate.SMOKE_CONTEXT, "state": "success"}])
        gate.cmd_status(api, SHA)
        self.assertEqual([payload["context"] for _, payload in api.posts], [gate.SMOKE_GATE])
        api = FakeAPI(raw_pull=raw)
        gate.cmd_status(api, BASE_SHA)
        self.assertEqual(api.posts, [])


class GroupTest(unittest.TestCase):
    RAW = {"number": 42, "head": {"sha": SHA}, "base": {"ref": "main"}, "labels": [{"name": "lgtm"}, {"name": "approved"}]}

    def test_the_pull_request_is_read_from_the_queue_branch(self):
        ref = f"refs/heads/gh-readonly-queue/main/pr-42-{BASE_SHA}"
        api = FakeAPI(raw_pull=self.RAW)
        passed, line = gate.cmd_group(api, ref, gate.LABELS_GATE)
        self.assertTrue(passed)
        self.assertIn("#42", line)

    def test_anything_short_of_success_fails_the_group(self):
        ref = f"gh-readonly-queue/main/pr-42-{BASE_SHA}"
        api = FakeAPI(raw_pull=self.RAW, files=[{"filename": "a2a/x.go"}])
        self.assertFalse(gate.cmd_group(api, ref, gate.SMOKE_GATE)[0])

    def test_a_ref_that_is_not_a_queue_branch_fails(self):
        for ref in ("refs/heads/main", "refs/heads/gh-readonly-queue/main/pr-42", ""):
            with self.subTest(ref=ref):
                self.assertFalse(gate.cmd_group(FakeAPI(raw_pull=self.RAW), ref, gate.LABELS_GATE)[0])


if __name__ == "__main__":
    unittest.main()
