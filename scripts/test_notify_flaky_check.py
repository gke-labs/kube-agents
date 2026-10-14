#!/usr/bin/env python3
"""Unit tests for the flaky-check issue reporter.

Run: cd scripts && python3 -m unittest test_notify_flaky_check

The scheduled sweep runs from the default branch's copy of the script; a
`workflow_dispatch` with `dry_run` on exercises a branch's copy against the live
API before merge. Everything that can be decided without a runner is decided here. The
failure modes worth the most: recording a flake that is not one (a re-run of a
cancelled attempt, a re-run that still failed), which teaches everyone to
ignore the label; splitting one flake across several issues because the
fingerprint drifted; and doubling a row when the same event is handled twice.
"""

import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import notify_flaky_check as reporter

REPO = "gke-labs/kube-agents"
WORKFLOW_ID = 327115835
WORKFLOW_FILE = _HERE.parent / ".github" / "workflows" / "flaky-check-notify.yml"


def workflow_document():
    """The notify workflow, parsed with PyYAML's `on` quirk handled: a bare
    `on:` key reads as the boolean True (YAML 1.1)."""
    document = yaml.safe_load(WORKFLOW_FILE.read_text())
    return document, (document.get("on") or document.get(True))


def listed_run(run_id, attempt):
    """A run as the list endpoint returns it, with the fields the sweep reads:
    its id and its latest attempt's number."""
    return {"id": run_id, "run_attempt": attempt, "conclusion": "success", "status": "completed"}


def recording_issue(number, workflow_id, *greens):
    """An issue body carrying the workflow's marker and one occurrence per
    (run id, green attempt) pair, each behind a failed first attempt."""
    lines = [reporter.issue_marker(workflow_id, "abc")]
    for run_id, passed_attempt in greens:
        occurrence = {"run_id": run_id, "failed_attempt": 1, "passed_attempt": passed_attempt}
        lines.append(f"{reporter.OCCURRENCE_PREFIX}{json.dumps(occurrence)}{reporter.OCCURRENCE_SUFFIX}")
    return {"number": number, "body": "\n".join(lines)}
SHA = "dcc2a821d0bcaa3ca21a47103d85cc3c0c9411c6"

UNITTEST_LOG = (
    "﻿2026-09-09T01:01:05.4770915Z Current runner version: '2.337.0'\n"
    "2026-09-09T01:04:59.4322202Z FAIL: test_push_failure_in_ci (test_publish_helm_chart.PublishHelmChartScriptTest.test_push_failure_in_ci)\n"
    "2026-09-09T01:04:59.4322202Z ERROR: test_other (pkg.mod.Case.test_other)\n"
    "2026-09-09T01:04:59.4324213Z FAILED (failures=1, errors=1)\n"
    "2026-09-09T01:05:05.8966316Z ##[error]Process completed with exit code 2.\n"
)


def attempt(number, conclusion, *, sha=SHA, rerun_by="bradhoekstra", branch="dependabot/grpc"):
    """The run as of one attempt, carrying only the fields the reporter reads."""
    return {
        "id": 34297433610,
        "run_number": 3043,
        "run_attempt": number,
        "conclusion": conclusion,
        "name": "Python Unit Tests",
        "workflow_id": WORKFLOW_ID,
        "event": "pull_request",
        "head_branch": branch,
        "head_sha": sha,
        "run_started_at": "2026-09-09T01:01:02Z",
        "actor": {"login": "dependabot[bot]"},
        "triggering_actor": {"login": rerun_by},
        "html_url": "https://github.com/gke-labs/kube-agents/actions/runs/34297433610",
    }


def job(name, conclusion, step_conclusions=("success",), job_id=1):
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": f"step {i}", "conclusion": c} for i, c in enumerate(step_conclusions, start=1)],
    }


class DecideTest(unittest.TestCase):
    """Whether a green attempt with failed attempts behind it is a flake.
    This is the whole design."""

    def failed(self, current, *earlier):
        return reporter.failed_before(current, list(earlier))

    def test_green_after_red_on_the_same_commit_is_a_flake(self):
        current = attempt(2, "success")
        self.assertIsNone(reporter.decide(current, self.failed(current, attempt(1, "failure"))))

    def test_a_timed_out_first_attempt_counts(self):
        current = attempt(2, "success")
        self.assertIsNone(reporter.decide(current, self.failed(current, attempt(1, "timed_out"))))

    def test_a_first_attempt_has_nothing_to_compare_against(self):
        self.assertIn("first attempt", reporter.decide(attempt(1, "success"), []))

    def test_a_re_run_that_still_fails_is_not_a_flake(self):
        """It may be a flake, but this evidence does not say so; the re-run
        that eventually passes will, and will read this attempt then."""
        current = attempt(2, "failure")
        self.assertIn("not success", reporter.decide(current, self.failed(current, attempt(1, "failure"))))

    def test_a_re_run_of_a_cancelled_attempt_is_not_a_flake(self):
        """Cancelled is a person or a concurrency group stopping the run. Read
        as a failure, every superseded-then-re-run check would file an issue."""
        for conclusion in ("cancelled", "skipped", "startup_failure", None):
            with self.subTest(conclusion=conclusion):
                current = attempt(2, "success")
                self.assertIn("no earlier attempt failed", reporter.decide(current, self.failed(current, attempt(1, conclusion))))

    def test_an_unreadable_previous_attempt_records_nothing(self):
        current = attempt(2, "success")
        self.assertIn("no earlier attempt failed", reporter.decide(current, self.failed(current, None)))

    def test_different_commits_are_not_the_same_run(self):
        """Cannot happen for a re-run, and the whole argument rests on it."""
        current = attempt(2, "success")
        self.assertEqual([], self.failed(current, attempt(1, "failure", sha="f" * 40)))

    def test_every_earlier_failed_attempt_is_kept_oldest_first(self):
        """Red, red, green: two failures, two rows, and the first one's log is
        half the evidence."""
        current = attempt(3, "success")
        failed = self.failed(current, attempt(2, "failure"), attempt(1, "failure"))
        self.assertEqual([1, 2], [a["run_attempt"] for a in failed])

    def test_a_cancelled_attempt_in_the_chain_is_skipped_not_a_stop(self):
        """Red, cancelled, green is still a same-commit flake."""
        current = attempt(3, "success")
        failed = self.failed(current, attempt(1, "failure"), attempt(2, "cancelled"))
        self.assertEqual([1], [a["run_attempt"] for a in failed])
        self.assertIsNone(reporter.decide(current, failed))


class FingerprintTest(unittest.TestCase):
    def test_unittest_ids_are_read_off_a_timestamped_log(self):
        self.assertEqual(
            reporter.extract_test_ids(UNITTEST_LOG),
            [
                "test_other (pkg.mod.Case.test_other)",
                "test_push_failure_in_ci (test_publish_helm_chart.PublishHelmChartScriptTest.test_push_failure_in_ci)",
            ],
        )

    def test_the_byte_order_mark_on_the_first_line_does_not_hide_it(self):
        log = "﻿2026-09-09T01:04:59.4322202Z FAIL: test_x (m.C.test_x)\n"
        self.assertEqual(reporter.extract_test_ids(log), ["test_x (m.C.test_x)"])

    def test_go_test_failures_are_read_including_subtests(self):
        log = (
            "2026-09-09T01:04:59.4322202Z --- FAIL: TestReconcile (0.01s)\n"
            "2026-09-09T01:04:59.4322202Z     --- FAIL: TestReconcile/refuses (0.00s)\n"
            "2026-09-09T01:04:59.4322202Z FAIL\n"
        )
        self.assertEqual(reporter.extract_test_ids(log), ["TestReconcile", "TestReconcile/refuses"])

    def test_pytest_failures_and_fixture_errors_are_read(self):
        log = (
            "2026-09-09T01:04:59.4322202Z FAILED bench/tests/test_x.py::test_y - AssertionError: boom\n"
            "2026-09-09T01:04:59.4322202Z ERROR bench/tests/test_z.py::test_w - fixture 'tmp' errored\n"
        )
        self.assertEqual(reporter.extract_test_ids(log), ["bench/tests/test_x.py::test_y", "bench/tests/test_z.py::test_w"])

    def test_a_subtest_failure_is_its_test_not_its_parameters(self):
        """unittest prints `FAIL: test_x (m.C.test_x) (conclusion='a')` per
        failing subTest. Anchored on the id's closing parenthesis alone, the
        pattern misses every one of them and the whole failure falls through
        to the job/step fallback."""
        log = (
            "2026-09-09T01:04:59Z FAIL: test_x (m.C.test_x) (conclusion='cancelled')\n"
            "2026-09-09T01:04:59Z FAIL: test_x (m.C.test_x) (conclusion='skipped')\n"
            "2026-09-09T01:04:59Z ERROR: test_y (m.C.test_y) [a label]\n"
            "2026-09-09T01:04:59Z FAIL: test_z (m.C.test_z) [lbl] (k=1)\n"
        )
        self.assertEqual(
            reporter.extract_test_ids(log),
            ["test_x (m.C.test_x)", "test_y (m.C.test_y)", "test_z (m.C.test_z)"],
        )

    def test_log_text_cannot_close_a_code_span_or_run_long(self):
        """Ids come out of a log the pull request's own code wrote."""
        log = "2026-09-09T01:04:59Z --- FAIL: Test`@someone-->x--!>y" + "x" * 500 + "\n"
        [test_id] = reporter.extract_test_ids(log)
        self.assertNotIn("`", test_id)
        self.assertNotIn("-->", test_id)
        self.assertNotIn("--!>", test_id)
        self.assertNotIn('"', reporter._plain('say "hi" \\ bye'))
        self.assertEqual(reporter._plain("a\x1b[31mb\x00c"), "a [31mb c")
        self.assertNotIn("\\", reporter._plain('say "hi" \\ bye'))
        # Bounded where it is stored and shown; the in-memory id keeps its
        # full length so the container can still be read off it.
        record = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), None, [test_id])
        self.assertLessEqual(len(record["failed"][0]), reporter.MAX_ID_LENGTH)
        body = reporter.render_body(REPO, "W", [test_id], [record], "<!-- m -->")
        self.assertNotIn("x" * (reporter.MAX_ID_LENGTH + 1), body)

    def test_a_long_unittest_id_still_keys_on_its_class(self):
        """A fifth of this repository's unittest ids are longer than the
        display cap. Read the class off a truncated id and there is no
        closing parenthesis to find, so the key degrades to the test."""
        method = "test_" + "very_" * 40 + "long"
        test_id = f"{method} (tests.test_something.SomeLongClassName.{method})"
        self.assertGreater(len(test_id), reporter.MAX_ID_LENGTH)
        log = f"2026-09-09T01:04:59Z FAIL: {test_id}\n"
        [found] = reporter.extract_test_ids(log)
        self.assertEqual(found, test_id)
        jobs = [{"name": "a", "steps": [], "test_ids": [found]}]
        self.assertEqual(reporter.signature(jobs), ["tests.test_something.SomeLongClassName"])

    def test_containers(self):
        cases = {
            "test_x (pkg.module.Class.test_x)": "pkg.module.Class",
            # A flaky shared fixture surfacing in setUpClass and in a test
            # method of the same class is one flake.
            "setUpClass (pkg.module.Class)": "pkg.module.Class",
            "setUpModule (pkg.module)": "pkg.module",
            "TestReconcile/refuses/again": "TestReconcile",
            "TestReconcile": "TestReconcile",
            "bench/tests/test_x.py::TestC::test_y": "bench/tests/test_x.py",
            "bench/tests/test_x.py::test_y": "bench/tests/test_x.py",
            "Run Python Unit Tests / step 2": "Run Python Unit Tests / step 2",
        }
        for test_id, expected in cases.items():
            with self.subTest(test_id=test_id):
                self.assertEqual(reporter.container(test_id), expected)

    def test_a_log_naming_nothing_yields_nothing(self):
        self.assertEqual(reporter.extract_test_ids("2026-09-09T01:04:59Z ##[error]Process completed with exit code 2.\n"), [])

    def test_the_signature_pools_ids_across_failing_jobs(self):
        """Two matrix legs failing on the same test are one flake, not two."""
        jobs = [
            {"name": "a", "steps": [], "test_ids": ["t1", "t2"]},
            {"name": "b", "steps": [], "test_ids": ["t2", "t3"]},
        ]
        self.assertEqual(reporter.signature(jobs), ["t1", "t2", "t3"])

    def test_different_tests_of_one_class_are_one_flake(self):
        """The September episode: three failed attempts, each naming a
        different test of `PublishHelmChartScriptTest`, one cause. Keyed on
        the test id they would have been two issues nobody connected."""
        first = [{"name": "a", "steps": [], "test_ids": ["test_push_failure_in_ci (t.PublishHelmChartScriptTest.test_push_failure_in_ci)"]}]
        second = [{"name": "a", "steps": [], "test_ids": ["test_ci_extract (t.PublishHelmChartScriptTest.test_ci_extract)"]}]
        self.assertEqual(reporter.signature(first), reporter.signature(second))
        self.assertEqual(reporter.signature(first), ["t.PublishHelmChartScriptTest"])

    def test_ids_are_split_by_container(self):
        jobs = [{"name": "a", "steps": [], "test_ids": ["test_1 (m.A.test_1)", "test_2 (m.B.test_2)", "test_3 (m.A.test_3)"]}]
        self.assertEqual(
            reporter.keys(jobs),
            {"m.A": ["test_1 (m.A.test_1)", "test_3 (m.A.test_3)"], "m.B": ["test_2 (m.B.test_2)"]},
        )

    def test_a_leg_that_named_no_test_files_under_its_job_beside_the_leg_that_did(self):
        """macOS died downloading, Ubuntu failed one test: two flakes, two
        issues, and the download one is not lost behind the test one."""
        steps = job("x", "failure", ("success", "failure"))["steps"]
        jobs = [
            {"name": "validate (macos-14)", "steps": steps, "test_ids": []},
            {"name": "validate (ubuntu-22.04)", "steps": steps, "test_ids": ["test_1 (m.A.test_1)"]},
        ]
        self.assertEqual(reporter.keys(jobs), {"m.A": ["test_1 (m.A.test_1)"], "validate / step 2": []})

    def test_past_the_container_cap_the_job_keeps_the_ids_it_named(self):
        many = [f"test_x (m.C{i:03}.test_x)" for i in range(reporter.MAX_CONTAINERS_PER_ATTEMPT + 1)]
        steps = job("x", "failure", ("failure",))["steps"]
        found = reporter.keys([{"name": "unit", "steps": steps, "test_ids": many}])
        self.assertEqual(list(found), ["unit / step 1"])
        self.assertEqual(found["unit / step 1"], many)

    def test_a_job_name_with_a_line_break_stays_on_one_line(self):
        """Job and step names come from the pull request's own workflow file."""
        self.assertNotIn("\n", reporter._plain("job\n@someone look"))

    def test_the_occurrence_keeps_its_own_ids_and_the_body_lists_the_union(self):
        a = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1, ["test_a (m.C.test_a)"])
        b = reporter.occurrence({**attempt(2, "success"), "id": 2}, {**attempt(1, "failure"), "id": 2}, 2, ["test_b (m.C.test_b)"])
        self.assertEqual(reporter.failed_across([a, b]), ["test_a (m.C.test_a)", "test_b (m.C.test_b)"])
        body = reporter.render_body(REPO, "W", ["m.C"], [a, b], "<!-- m -->")
        self.assertIn("- `m.C`", body)
        self.assertIn("- `test_a (m.C.test_a)`", body)
        self.assertIn("- `test_b (m.C.test_b)`", body)

    def test_the_union_says_when_it_is_cut(self):
        ids = [f"test_{i:03} (m.C.test_{i:03})" for i in range(reporter.MAX_OCCURRENCE_IDS * 3)]
        records = []
        for i in range(3):
            current = {**attempt(2, "success"), "id": i}
            chunk = ids[i * reporter.MAX_OCCURRENCE_IDS : (i + 1) * reporter.MAX_OCCURRENCE_IDS]
            records.append(reporter.occurrence(current, attempt(1, "failure"), None, chunk))
        body = reporter.render_body(REPO, "W", ["m.C"], records, "<!-- m -->")
        self.assertIn(f"- (+{reporter.MAX_OCCURRENCE_IDS * 2} more)", body)

    def test_the_signature_falls_back_to_job_and_step_names(self):
        jobs = [
            {"name": "Run Python Unit Tests", "steps": job("x", "failure", ("success", "failure"))["steps"], "test_ids": []},
        ]
        self.assertEqual(reporter.signature(jobs), ["Run Python Unit Tests / step 2"])

    def test_matrix_legs_share_a_fallback_fingerprint(self):
        """The installer matrix has no test ids; the same flake on macOS and
        on Ubuntu must not be two issues."""
        steps = job("x", "failure", ("failure",))["steps"]
        mac = [{"name": "validate (macos-14)", "steps": steps, "test_ids": []}]
        linux = [{"name": "validate (ubuntu-22.04)", "steps": steps, "test_ids": []}]
        self.assertEqual(reporter.signature(mac), reporter.signature(linux))
        self.assertEqual(reporter.signature(mac), ["validate / step 1"])

    def test_a_failing_job_with_no_failing_step_is_named_by_itself(self):
        """A runner lost mid-job reports the job failed with every step green
        or skipped; the job name is all there is."""
        jobs = [{"name": "build", "steps": job("x", "failure", ("success",))["steps"], "test_ids": []}]
        self.assertEqual(reporter.signature(jobs), ["build"])

    def test_an_attempt_that_failed_many_classes_is_the_jobs_failure_not_theirs(self):
        """Sixty classes red is a broken environment; sixty issues would bury
        the real flakes. Past the cap the attempt keys on the job and step."""
        many = [f"test_x (m.C{i:03}.test_x)" for i in range(reporter.MAX_CONTAINERS_PER_ATTEMPT + 1)]
        few = many[: reporter.MAX_CONTAINERS_PER_ATTEMPT]
        steps = job("x", "failure", ("failure",))["steps"]
        self.assertEqual(reporter.signature([{"name": "unit", "steps": steps, "test_ids": many}]), ["unit / step 1"])
        self.assertEqual(len(reporter.signature([{"name": "unit", "steps": steps, "test_ids": few}])), len(few))

    def test_the_digest_depends_on_workflow_and_lines_and_nothing_else(self):
        self.assertEqual(reporter.digest(1, ["a"]), reporter.digest(1, ["a"]))
        self.assertNotEqual(reporter.digest(1, ["a"]), reporter.digest(2, ["a"]))
        self.assertNotEqual(reporter.digest(1, ["a"]), reporter.digest(1, ["b"]))
        self.assertEqual(len(reporter.digest(1, ["a"])), reporter.FINGERPRINT_DIGEST_LENGTH)

    def test_the_issue_marker_starts_with_the_workflow_marker(self):
        """`issues_for_workflow` narrows by the prefix and `reconcile` by the
        whole line, and the two have to agree on the leading characters."""
        marker = reporter.issue_marker(WORKFLOW_ID, "abc")
        self.assertTrue(marker.startswith(reporter.workflow_marker(WORKFLOW_ID)))
        self.assertTrue(marker.startswith("<!--") and marker.endswith("-->"))


class OccurrenceTest(unittest.TestCase):
    def test_the_row_names_the_re_runner_not_the_actor(self):
        """`actor` on a dependabot pull request is dependabot, and on `main`
        it is Tide. The person who clicked re-run is `triggering_actor` of the
        green attempt, and is who a reader would want to ask."""
        record = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1321)
        self.assertEqual(record["rerun_by"], "bradhoekstra")
        self.assertEqual(record["pull_request"], 1321)
        self.assertEqual((record["failed_attempt"], record["passed_attempt"]), (1, 2))
        self.assertEqual(record["date"], "2026-09-09")

    def test_occurrences_round_trip_through_the_body(self):
        record = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1321)
        body = reporter.render_body(REPO, "Python Unit Tests", ["t"], [record], "<!-- m -->")
        self.assertEqual(reporter.occurrences_in(body), [record])

    def test_a_hand_edited_or_garbled_occurrence_line_is_skipped_not_fatal(self):
        body = f"{reporter.OCCURRENCE_PREFIX}not json{reporter.OCCURRENCE_SUFFIX}\n"
        self.assertEqual(reporter.occurrences_in(body), [])

    def test_the_same_event_handled_twice_is_one_row(self):
        record = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1321)
        merged, added = reporter.merge_occurrences([record], [dict(record)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(added, [])

    def test_the_issue_keeps_only_the_most_recent_occurrences(self):
        """GitHub refuses a body over its limit, and a refused update loses
        every later occurrence, so the hidden lines are bounded."""
        records = [
            dict(reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1), run_id=i)
            for i in range(reporter.MAX_OCCURRENCES + 5)
        ]
        merged, added = reporter.merge_occurrences(records[:-1], records[-1:])
        self.assertEqual(len(merged), reporter.MAX_OCCURRENCES)
        self.assertEqual(merged[-1]["run_id"], records[-1]["run_id"])
        self.assertEqual(len(added), 1)
        body = reporter.render_body(REPO, "W", ["t"], merged, "<!-- m -->")
        self.assertIn(f"{reporter.MAX_OCCURRENCES} most recent", body)

    def test_a_body_at_every_cap_fits_github(self):
        """The caps are only a guarantee if the worst case they allow renders
        under the limit; this is the arithmetic the constants' comment
        claims."""
        # The worst character for the hidden JSON lines: non-ASCII, which
        # would be six characters escaped, and the quote and backslash,
        # which would double. `_neutral` removes the last two; the test
        # feeds them anyway so the arithmetic covers the rendered form.
        longest = reporter._plain('é"\\\x1b' * reporter.MAX_ID_LENGTH)
        self.assertEqual(len(longest), reporter.MAX_ID_LENGTH)
        ids = [f"{i:04}{longest}"[: reporter.MAX_ID_LENGTH] for i in range(reporter.MAX_OCCURRENCE_IDS)]
        records = []
        for i in range(reporter.MAX_OCCURRENCES):
            current = attempt(99, "success", branch=longest)
            current["id"] = i
            records.append(reporter.occurrence(current, attempt(98, "failure"), None, ids))
        lines = [f"{0:04}{longest}"[: reporter.MAX_ID_LENGTH]]
        marker = reporter.issue_marker(WORKFLOW_ID, "f" * reporter.FINGERPRINT_DIGEST_LENGTH)
        body = reporter.render_body(REPO, "W" * 40, lines, records, marker, previously={"number": 99999})
        self.assertLess(len(body), reporter.GITHUB_ISSUE_BODY_LIMIT)

    def test_a_later_re_run_of_the_same_run_is_a_new_row(self):
        """Attempt 1 red, 2 red, 3 green: attempts 1→2 recorded nothing, and
        2→3 is the occurrence. A later flake on a different run is another."""
        first = reporter.occurrence(attempt(3, "success"), attempt(2, "failure"), 1321)
        other_run = attempt(2, "success")
        other_run["id"] = 99
        second = reporter.occurrence(other_run, {**attempt(1, "failure"), "id": 99}, 1330)
        merged, added = reporter.merge_occurrences([first], [second])
        self.assertEqual(len(merged), 2)
        self.assertEqual(added, [second])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.record = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1321)
        self.lines = ["test_push_failure_in_ci (test_publish_helm_chart.PublishHelmChartScriptTest.test_push_failure_in_ci)"]

    def test_the_title_names_the_workflow_and_the_first_failure(self):
        title = reporter.render_title("Python Unit Tests", self.lines)
        self.assertIn("Python Unit Tests", title)
        self.assertIn("test_push_failure_in_ci", title)
        self.assertNotIn("more", title)

    def test_the_title_survives_an_empty_signature(self):
        self.assertIn("unknown failure", reporter.render_title("W", []))

    def test_the_body_links_the_failed_attempt_and_the_pull_request(self):
        body = reporter.render_body(REPO, "Python Unit Tests", self.lines, [self.record], "<!-- m -->")
        self.assertIn("/actions/runs/34297433610/attempts/1)", body)
        self.assertIn("| #1321 |", body)
        self.assertIn("bradhoekstra", body)
        self.assertIn("1 time.", body)
        self.assertTrue(body.rstrip().endswith("-->"))

    def test_the_body_never_promises_to_close(self):
        body = reporter.render_body(REPO, "W", self.lines, [self.record], "<!-- m -->")
        self.assertIn("never closes an issue", body)

    def test_a_push_run_shows_the_branch_where_a_pull_request_would_go(self):
        record = reporter.occurrence(attempt(2, "success", branch="main"), attempt(1, "failure", branch="main"), None)
        body = reporter.render_body(REPO, "W", self.lines, [record], "<!-- m -->")
        self.assertIn("| `main` |", body)

    def test_a_fork_branch_name_cannot_break_the_table_or_mention_anyone(self):
        branch = "feat/`@octocat`|x"
        record = reporter.occurrence(attempt(2, "success", branch=branch), attempt(1, "failure", branch=branch), None)
        body = reporter.render_body(REPO, "W", self.lines, [record], "<!-- m -->")
        row = next(line for line in body.splitlines() if "2026-09-09" in line)
        # Six unescaped column separators; the one in the branch is escaped.
        self.assertEqual(row.replace("\\|", "").count("|"), 6)
        self.assertNotIn("`@octocat`", body)

    def test_a_recurrence_names_the_closed_issue(self):
        body = reporter.render_body(REPO, "W", self.lines, [self.record], "<!-- m -->", previously={"number": 42})
        self.assertIn("Previously tracked in #42", body)

    def test_the_marker_is_in_the_body_verbatim(self):
        marker = reporter.issue_marker(WORKFLOW_ID, "abc")
        body = reporter.render_body(REPO, "W", self.lines, [self.record], marker)
        self.assertIn(marker, body)

    def test_a_pipe_in_a_test_id_does_not_break_the_table(self):
        body = reporter.render_body(REPO, "W", ["a|b"], [self.record], "<!-- m -->")
        self.assertIn("a\\|b", body)

    def test_the_comment_counts_occurrences(self):
        comment = reporter.render_comment(REPO, [self.record], 3)
        self.assertIn("Flaked again", comment)
        self.assertIn("3 occurrences", comment)
        self.assertIn("#1321", comment)

    def test_the_comment_names_every_failed_attempt_behind_one_green(self):
        second = reporter.occurrence(attempt(3, "success"), attempt(2, "failure"), 1321)
        comment = reporter.render_comment(REPO, [self.record, second], 4)
        self.assertIn("attempt 1](", comment)
        self.assertIn(" and [run 3043 attempt 2](", comment)


class FakeAPI:
    """Records what `reconcile` asks of the API and answers from lists of open
    and closed issues, which is the whole of the state it reads."""

    def __init__(self, open_issues=(), closed_issues=()):
        self.issues = {"open": list(open_issues), "closed": list(closed_issues)}
        self.actions = []
        self.next_number = 900

    def issues_for_workflow(self, workflow_id, state):
        prefix = reporter.workflow_marker(workflow_id)
        return [issue for issue in self.issues[state] if prefix in (issue.get("body") or "")]

    def ensure_label(self):
        self.actions.append(("label",))

    def create_issue(self, title, body):
        self.next_number += 1
        self.actions.append(("create", self.next_number, title, body))
        return {"number": self.next_number}

    def update_issue(self, number, **fields):
        self.actions.append(("update", number, fields))

    def comment(self, number, body):
        self.actions.append(("comment", number, body))

    def kinds(self):
        return [action[0] for action in self.actions]


def existing_issue(number, lines, records, workflow_id=WORKFLOW_ID, seen=()):
    marker = reporter.issue_marker(workflow_id, reporter.digest(workflow_id, lines))
    return {"number": number, "body": reporter.render_body(REPO, "Python Unit Tests", lines, records, marker, seen=seen)}


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.lines = ["test_x (m.C.test_x)"]
        self.record = reporter.occurrence(attempt(2, "success"), attempt(1, "failure"), 1321)

    def reconcile(self, api, lines=None, records=None):
        return reporter.reconcile(api, REPO, WORKFLOW_ID, "Python Unit Tests", lines or self.lines, records or [self.record])

    def test_a_new_fingerprint_opens_an_issue_with_the_label(self):
        api = FakeAPI()
        done = self.reconcile(api)
        self.assertEqual(api.kinds(), ["label", "create"])
        self.assertIn("opened #901", done)
        _, _, title, body = api.actions[1]
        self.assertIn("test_x", title)
        self.assertEqual(len(reporter.occurrences_in(body)), 1)

    def test_a_known_fingerprint_gains_a_row_and_a_comment(self):
        older = dict(self.record, run_id=1, run_number=1, date="2026-09-08")
        api = FakeAPI(open_issues=[existing_issue(700, self.lines, [older])])
        done = self.reconcile(api)
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertIn("updated #700 (2 occurrences)", done)
        body = api.actions[0][2]["body"]
        self.assertEqual([r["run_id"] for r in reporter.occurrences_in(body)], [1, self.record["run_id"]])
        self.assertIn("2 occurrences", api.actions[1][2])

    def test_the_same_event_twice_changes_nothing(self):
        api = FakeAPI(open_issues=[existing_issue(700, self.lines, [self.record])])
        done = self.reconcile(api)
        self.assertEqual(api.kinds(), [])
        self.assertIn("already records", done)

    def test_two_failed_attempts_behind_one_green_are_two_rows_and_one_comment(self):
        second = reporter.occurrence(attempt(3, "success"), attempt(2, "failure"), 1321)
        older = dict(self.record, run_id=1, failed_attempt=1)
        api = FakeAPI(open_issues=[existing_issue(700, self.lines, [older])])
        done = self.reconcile(api, records=[self.record, second])
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertIn("(3 occurrences)", done)
        self.assertEqual(len(reporter.occurrences_in(api.actions[0][2]["body"])), 3)

    def test_a_record_the_row_cap_trimmed_is_still_known_and_not_re_added(self):
        """The body keeps the newest MAX_OCCURRENCES rows and every key it has
        ever held on a hidden line. A record whose row was trimmed is neither
        a new row nor a comment, or a sweep that revisits every run in its
        window would add it back and announce it four times an hour."""
        rows = [dict(self.record, run_id=100 + n, run_number=n, date="2026-09-08") for n in range(reporter.MAX_OCCURRENCES)]
        trimmed = dict(self.record, run_id=1, run_number=1, date="2026-09-01")
        issue = existing_issue(700, self.lines, rows, seen=[reporter.seen_key(trimmed)])
        self.assertEqual(len(reporter.occurrences_in(issue["body"])), reporter.MAX_OCCURRENCES)
        api = FakeAPI(open_issues=[issue])
        done = self.reconcile(api, records=[trimmed])
        self.assertEqual(api.kinds(), [])
        self.assertIn("already records", done)

    def test_a_write_that_trims_a_body_with_no_key_line_keeps_the_trimmed_key(self):
        """Every issue written before the key line existed shows its rows and
        nothing else. The first write that trims one of those rows must put its
        key on the line it creates, or the next sweep reads that run again."""
        rows = [dict(self.record, run_id=100 + n, run_number=n, date="2026-09-08") for n in range(reporter.MAX_OCCURRENCES)]
        legacy = existing_issue(700, self.lines, rows)
        legacy["body"] = "\n".join(line for line in legacy["body"].splitlines() if not line.startswith(reporter.SEEN_PREFIX))
        self.assertEqual(reporter.seen_in(legacy["body"]), [])
        api = FakeAPI(open_issues=[legacy])
        self.reconcile(api, records=[dict(self.record, run_id=999, run_number=99)])
        self.assertEqual(api.kinds(), ["update", "comment"])
        body = api.actions[0][2]["body"]
        self.assertEqual([r["run_id"] for r in reporter.occurrences_in(body)][0], 101, "the oldest row was trimmed")
        self.assertIn(reporter.seen_key(rows[0]), reporter.seen_in(body), "and its key survived")
        self.assertEqual(len(reporter.seen_in(body)), reporter.MAX_OCCURRENCES + 1)

    def test_the_hidden_keys_survive_the_row_cap_and_bound_themselves(self):
        """Round trip: every key the body has held, newest last, at most
        MAX_SEEN_KEYS of them, whichever rows the table still shows."""
        older = [reporter.seen_key(dict(self.record, run_id=n, failed_attempt=1, passed_attempt=2)) for n in range(reporter.MAX_SEEN_KEYS + 5)]
        body = reporter.render_body(REPO, "Python Unit Tests", self.lines, [self.record], "<!-- flaky-check m -->", seen=older)
        seen = reporter.seen_in(body)
        self.assertEqual(len(seen), reporter.MAX_SEEN_KEYS)
        self.assertEqual(seen[-1], (self.record["run_id"], self.record["failed_attempt"], self.record["passed_attempt"]))
        # 1005 older keys plus the new record is 1006: the oldest six go.
        self.assertNotIn((5, 1, 2), seen)
        self.assertIn((6, 1, 2), seen)
        self.assertEqual(reporter.MAX_SEEN_KEYS, 1000)

    def test_a_different_failure_of_the_same_workflow_is_a_separate_issue(self):
        api = FakeAPI(open_issues=[existing_issue(700, ["test_y (m.C.test_y)"], [self.record])])
        self.reconcile(api)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_the_same_failure_in_another_workflow_is_a_separate_issue(self):
        api = FakeAPI(open_issues=[existing_issue(700, self.lines, [self.record], workflow_id=1)])
        self.reconcile(api)
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_recurrence_after_a_close_opens_a_new_issue_that_links_back(self):
        """Closed means someone fixed it, or believed they had. Reopening
        silently would hide that the fix did not hold."""
        api = FakeAPI(closed_issues=[existing_issue(600, self.lines, [self.record])])
        done = self.reconcile(api)
        self.assertEqual(api.kinds(), ["label", "create"])
        self.assertIn("recurrence of #600", done)
        self.assertIn("Previously tracked in #600", api.actions[1][3])

    def test_the_link_back_survives_the_next_occurrence(self):
        """The body is rebuilt on every update and nothing else remembers
        the closed issue, so it has to be looked up again each time."""
        older = dict(self.record, run_id=1)
        api = FakeAPI(
            open_issues=[existing_issue(901, self.lines, [older])],
            closed_issues=[existing_issue(600, self.lines, [older])],
        )
        self.reconcile(api)
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertIn("Previously tracked in #600", api.actions[0][2]["body"])

    def test_a_hand_renamed_issue_is_still_found_and_keeps_its_name(self):
        """The marker finds it; the update then must not write the title
        back, or the rename that says what the cause was is undone."""
        issue = existing_issue(700, self.lines, [dict(self.record, run_id=1)])
        issue["title"] = "someone renamed this"
        api = FakeAPI(open_issues=[issue])
        self.reconcile(api)
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertNotIn("title", api.actions[0][2])


class IssueListingTest(unittest.TestCase):
    """The real `issues_for_workflow`: label and state in the query, every
    page read, pull requests dropped, and only this workflow's marker kept."""

    def _api(self, pages):
        calls = []

        class API(reporter.GitHubAPI):
            def request(self, method, path, payload=None, tolerate=()):
                calls.append(path)
                return pages.pop(0) if pages else []

        return API(REPO, "tok"), calls

    def test_pull_requests_and_other_workflows_are_dropped(self):
        mine = {"number": 1, "body": reporter.issue_marker(WORKFLOW_ID, "a")}
        other = {"number": 2, "body": reporter.issue_marker(WORKFLOW_ID + 1, "a")}
        pull = {"number": 3, "body": reporter.issue_marker(WORKFLOW_ID, "a"), "pull_request": {}}
        api, calls = self._api([[mine, other, pull]])
        self.assertEqual([i["number"] for i in api.issues_for_workflow(WORKFLOW_ID, "closed")], [1])
        self.assertIn(f"labels={reporter.LABEL.replace(':', '%3A')}", calls[0])
        self.assertIn("state=closed", calls[0])

    def test_every_page_is_read(self):
        """The closed list is what the link back to an old recurrence depends
        on, and it only grows."""
        full = [{"number": n, "body": "unrelated"} for n in range(reporter.PAGE_SIZE)]
        last = [{"number": 999, "body": reporter.issue_marker(WORKFLOW_ID, "a")}]
        api, calls = self._api([full, last])
        self.assertEqual([i["number"] for i in api.issues_for_workflow(WORKFLOW_ID, "closed")], [999])
        self.assertEqual(len(calls), 2)
        self.assertIn("page=2", calls[1])


class JobLogTest(unittest.TestCase):
    """The two-step log fetch: GitHub 302s to a pre-signed blob URL that
    refuses the GitHub token, so the redirect is taken without it."""

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def _api(self, redirect_status=302, location="https://blob.example/log?sig=1", blob=b"log body"):
        seen = {"first": None, "second": None}

        def redirect_opener(request):
            seen["first"] = request
            if redirect_status is None:
                return self._Response(blob)
            raise urllib.error.HTTPError(request.full_url, redirect_status, "redirect", {"Location": location}, None)

        def opener(request):
            seen["second"] = request
            return self._Response(blob)

        api = reporter.GitHubAPI(REPO, "tok", opener=opener, redirect_opener=redirect_opener)
        return api, seen

    def test_the_token_goes_to_github_and_not_to_the_blob_store(self):
        api, seen = self._api()
        self.assertEqual(api.job_log(5), "log body")
        self.assertIn("Authorization", seen["first"].headers)
        self.assertNotIn("Authorization", seen["second"].headers)
        self.assertEqual(seen["second"].full_url, "https://blob.example/log?sig=1")

    def test_a_direct_body_is_accepted_too(self):
        api, seen = self._api(redirect_status=None)
        self.assertEqual(api.job_log(5), "log body")
        self.assertIsNone(seen["second"])

    def test_a_missing_log_is_empty_not_fatal(self):
        """An expired log costs the test ids, not the issue: the fingerprint
        falls back to job and step names."""
        api, _ = self._api(redirect_status=404, location=None)
        self.assertEqual(api.job_log(5), "")

    def test_an_unreachable_api_is_empty_not_fatal(self):
        def redirect_opener(request):
            raise urllib.error.URLError("connection reset")

        api = reporter.GitHubAPI(REPO, "tok", opener=None, redirect_opener=redirect_opener, sleep=lambda s: None)
        self.assertEqual(api.job_log(5), "")

    def test_a_secondary_rate_limit_on_the_first_hop_is_retried_after_retry_after(self):
        """One notify run makes a dozen calls back to back; a 403 with
        Retry-After is GitHub throttling, not refusing, and the base client
        retries it. This path has to as well or the fingerprint degrades."""
        calls = []

        def redirect_opener(request):
            calls.append(1)
            if len(calls) < 2:
                raise urllib.error.HTTPError(request.full_url, 403, "throttled", {"Retry-After": "7"}, None)
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://blob.example/l"}, None)

        slept = []
        api = reporter.GitHubAPI(REPO, "tok", opener=lambda r: self._Response(b"log"), redirect_opener=redirect_opener, sleep=slept.append)
        self.assertEqual(api.job_log(5), "log")
        self.assertEqual(slept, [7])

    def test_a_transient_5xx_on_the_second_hop_is_retried(self):
        """The blob store answering 503 once must not swap the fingerprint
        any more than GitHub answering 502 once may."""
        second = []

        def redirect_opener(request):
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://blob.example/l"}, None)

        def opener(request):
            second.append(1)
            if len(second) < 2:
                raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
            return self._Response(b"log body")

        slept = []
        api = reporter.GitHubAPI(REPO, "tok", opener=opener, redirect_opener=redirect_opener, sleep=slept.append)
        self.assertEqual(api.job_log(5), "log body")
        self.assertEqual((len(second), len(slept)), (2, 1))

    def test_a_transient_5xx_on_the_first_hop_is_retried(self):
        """One 502 must not swap a class-keyed fingerprint for a job/step
        one, which reconcile would file as a second issue."""
        calls = []

        def redirect_opener(request):
            calls.append(1)
            if len(calls) < 2:
                raise urllib.error.HTTPError(request.full_url, 502, "bad gateway", {}, None)
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://blob.example/l"}, None)

        def opener(request):
            return self._Response(b"log body")

        slept = []
        api = reporter.GitHubAPI(REPO, "tok", opener=opener, redirect_opener=redirect_opener, sleep=slept.append)
        self.assertEqual(api.job_log(5), "log body")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(slept), 1)


class PullRequestLookupTest(unittest.TestCase):
    """Every pull request here comes from a fork, and neither the run's own
    `pull_requests` nor `GET /commits/{sha}/pulls` names an open fork pull
    request. Listing by `head=<owner>:<branch>` does."""

    def _api(self, answers):
        calls = []

        class API(reporter.GitHubAPI):
            def get(self, path, tolerate=()):
                calls.append(path)
                return answers.get(path.split("?")[0], [])

        return API(REPO, "tok"), calls

    def test_a_fork_pull_request_is_found_by_owner_and_branch(self):
        run = {**attempt(2, "success", branch="fix/thing"), "head_repository": {"owner": {"login": "kyber775"}}}
        api, calls = self._api({f"/repos/{REPO}/pulls": [{"number": 1331, "head": {"sha": SHA}}, {"number": 9, "head": {"sha": "0" * 40}}]})
        self.assertEqual(api.pull_requests_for(run), [1331])
        self.assertIn("head=kyber775%3Afix%2Fthing", calls[0])
        self.assertIn("state=all", calls[0])

    def test_a_closed_pull_request_from_an_earlier_life_of_the_branch_is_not_matched(self):
        """`state=all` also returns a closed pull request from an earlier life
        of the branch name; it is neither at this commit nor open."""
        run = {**attempt(2, "success", branch="b"), "head_repository": {"owner": {"login": "o"}}}
        api, _ = self._api({f"/repos/{REPO}/pulls": [{"number": 9, "state": "closed", "head": {"sha": "0" * 40}}]})
        self.assertEqual(api.pull_requests_for(run), [])

    def test_an_open_pull_request_whose_head_moved_on_is_still_the_one(self):
        """The author pushed between the failed attempt and now. The open
        pull request on that branch is what the commit was tested for."""
        run = {**attempt(2, "success", branch="b"), "head_repository": {"owner": {"login": "o"}}}
        api, _ = self._api({f"/repos/{REPO}/pulls": [{"number": 9, "state": "open", "head": {"sha": "0" * 40}}]})
        self.assertEqual(api.pull_requests_for(run), [9])

    def test_a_lookup_that_fails_costs_the_number_not_the_record(self):
        class API(reporter.GitHubAPI):
            def get(self, path, tolerate=()):
                raise urllib.error.HTTPError(path, 502, "bad gateway", {}, None)

        run = {**attempt(2, "success", branch="b"), "head_repository": {"owner": {"login": "o"}}}
        self.assertEqual(API(REPO, "tok").pull_requests_for(run), [])

    def test_a_run_without_a_head_repository_falls_back_to_the_commit(self):
        run = {**attempt(2, "success"), "head_repository": None}
        api, calls = self._api({f"/repos/{REPO}/commits/{SHA}/pulls": [{"number": 5, "head": {"sha": SHA}}]})
        self.assertEqual(api.pull_requests_for(run), [5])
        self.assertTrue(calls[0].endswith(f"/commits/{SHA}/pulls"))


class FailedJobsTest(unittest.TestCase):
    def test_only_failing_jobs_have_their_logs_read(self):
        """A matrix leg cancelled because a sibling failed is not part of the
        fingerprint, and a green job's log is not worth the download."""

        class API:
            def __init__(self):
                self.read = []

            def attempt_jobs(self, run_id, number):
                return [
                    job("green", "success", job_id=1),
                    job("red", "failure", ("success", "failure"), job_id=2),
                    job("stopped", "cancelled", job_id=3),
                ]

            def job_log(self, job_id):
                self.read.append(job_id)
                return UNITTEST_LOG

        api = API()
        jobs = reporter.failed_jobs_with_ids(api, 1, 1)
        self.assertEqual(api.read, [2])
        self.assertEqual([j["name"] for j in jobs], ["red"])
        self.assertEqual(len(jobs[0]["test_ids"]), 2)

    def test_a_timed_out_job_reads_cancelled_and_still_names_the_failure(self):
        """A job stopped by `timeout-minutes` is `cancelled` in the jobs API
        while the run is `failure`. With no job saying `failure`, whatever did
        not pass or skip is the failure, and the step that was running names
        it -- otherwise every timeout of a workflow pooled into one issue
        titled 'unknown failure'."""

        class API:
            def attempt_jobs(self, run_id, number):
                return [
                    job("green", "success", job_id=1),
                    job("hung", "cancelled", ("success", "cancelled", "skipped"), job_id=2),
                    job("never ran", "skipped", job_id=3),
                ]

            def job_log(self, job_id):
                return "2026-09-09T01:04:59Z ##[error]The operation was canceled.\n"

        jobs = reporter.failed_jobs_with_ids(API(), 1, 1)
        self.assertEqual([j["name"] for j in jobs], ["hung"])
        self.assertEqual(reporter.signature(jobs), ["hung / step 2"])


class MainTest(unittest.TestCase):
    """The entry point's own decisions, with the API replaced wholesale."""

    def _run(self, attempts, argv, jobs=(), pulls=(1321,), env_token="tok", log_for_attempt=None, refuse_title_ending=None):
        actions = []
        listings = []
        state = {"attempt": None}

        class API:
            def __init__(self, *a, **k):
                pass

            def run(self, run_id):
                return attempts[max(attempts)]

            def attempt(self, run_id, number):
                return attempts.get(number)

            def attempt_jobs(self, run_id, number):
                state["attempt"] = number
                return list(jobs)

            def job_log(self, job_id):
                if log_for_attempt:
                    return log_for_attempt[state["attempt"]]
                return UNITTEST_LOG

            def pull_requests_for(self, run):
                return list(pulls)

            def issues_for_workflow(self, workflow_id, state):
                listings.append(state)
                return []

            def ensure_label(self):
                actions.append("label")

            def create_issue(self, title, body):
                if refuse_title_ending and title.endswith(refuse_title_ending):
                    raise urllib.error.HTTPError("/issues", 422, "body too long", {}, None)
                actions.append(("create", title, body))
                return {"number": 1}

        original_api, original_log = reporter.GitHubAPI, reporter.log
        logged = []
        reporter.GitHubAPI, reporter.log = API, logged.append
        environ = dict(GITHUB_TOKEN=env_token) if env_token else {}
        try:
            with mock.patch.dict(reporter.os.environ, environ, clear=True):
                code = reporter.main(argv)
        finally:
            reporter.GitHubAPI, reporter.log = original_api, original_log
        self.listings = listings
        return code, actions, logged

    def test_a_run_of_an_unwatched_workflow_records_nothing(self):
        """The dispatch takes any run id; the exclusions in `WATCHED_WORKFLOWS`
        hold on this path as they do on the sweep's."""
        attempts = {1: dict(attempt(1, "failure"), name="Security Scanning"), 2: dict(attempt(2, "success"), name="Security Scanning")}
        code, actions, logged = self._run(attempts, ["--run-id", "34297433610", "--attempt", "2"], jobs=[job("red", "failure", job_id=2)])
        self.assertEqual(code, 0)
        self.assertEqual(actions, [])
        self.assertTrue(any("not watched" in line for line in logged))

    def test_a_flake_is_recorded_from_the_named_attempt(self):
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "success")}
        code, actions, _ = self._run(attempts, ["--run-id", "34297433610", "--attempt", "2"], jobs=[job("red", "failure", job_id=2)])
        self.assertEqual(code, 0)
        self.assertEqual(actions[0], "label")
        # Two ids from two classes in the log: two issues, one per class,
        # each listing only its own test.
        creates = [a for a in actions if a != "label"]
        self.assertEqual(len(creates), 2)
        by_title = {a[1]: a[2] for a in creates}
        helm = next(b for t, b in by_title.items() if t.endswith("PublishHelmChartScriptTest"))
        self.assertIn("test_push_failure_in_ci (test_publish_helm_chart", helm)
        self.assertNotIn("test_other", helm)
        self.assertIn("#1321", helm)

    def test_every_failed_attempt_behind_the_green_is_a_row(self):
        """The motivating run: attempts 1 and 2 failed on different tests of
        one class, attempt 3 passed. One issue, two rows, both tests."""
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "failure"), 3: attempt(3, "success")}
        code, actions, _ = self._run(attempts, ["--run-id", "34297433610", "--attempt", "3"], jobs=[job("red", "failure", job_id=2)])
        self.assertEqual(code, 0)
        helm = next(a[2] for a in actions if a != "label" and a[1].endswith("PublishHelmChartScriptTest"))
        records = reporter.occurrences_in(helm)
        self.assertEqual([r["failed_attempt"] for r in records], [1, 2])
        self.assertIn("2 times.", helm)

    def test_two_flaky_classes_in_one_run_are_two_issues_each_keyed_alone(self):
        """Run X fails m.A on attempt 1 and m.B on attempt 2; run Y later
        fails m.A alone. Keyed on the set, Y would open a second m.A issue."""
        logs = {
            1: "2026-09-09T01:04:59Z FAIL: test_1 (m.A.test_1)\n",
            2: "2026-09-09T01:04:59Z FAIL: test_2 (m.B.test_2)\n",
        }
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "failure"), 3: attempt(3, "success")}
        code, actions, _ = self._run(
            attempts,
            ["--run-id", "1", "--attempt", "3"],
            jobs=[job("red", "failure", job_id=2)],
            log_for_attempt=logs,
        )
        self.assertEqual(code, 0)
        creates = [a for a in actions if a != "label"]
        self.assertEqual(len(creates), 2)
        # Two keys, one pair of listings: the lists are fetched once per run.
        self.assertEqual(sorted(self.listings), ["closed", "open"])
        titles = sorted(a[1] for a in creates)
        self.assertTrue(titles[0].endswith("m.A"))
        self.assertTrue(titles[1].endswith("m.B"))
        body_a = next(a[2] for a in creates if a[1].endswith("m.A"))
        # m.A's issue has one row (attempt 1) and its fingerprint is m.A alone.
        self.assertEqual([r["failed_attempt"] for r in reporter.occurrences_in(body_a)], [1])
        self.assertIn(reporter.issue_marker(WORKFLOW_ID, reporter.digest(WORKFLOW_ID, ["m.A"])), body_a)

    def test_an_attempt_whose_log_names_no_test_is_still_a_row_under_its_job(self):
        """Attempt 1 names m.A, attempt 2 died installing deps, attempt 3
        green: m.A gets row 1 and the job-and-step issue gets row 2. Neither
        attempt is attached to the other's issue."""
        logs = {
            1: "2026-09-09T01:04:59Z FAIL: test_1 (m.A.test_1)\n",
            2: "2026-09-09T01:04:59Z ##[error]Process completed with exit code 1.\n",
        }
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "failure"), 3: attempt(3, "success")}
        code, actions, _ = self._run(
            attempts,
            ["--run-id", "1", "--attempt", "3"],
            jobs=[job("red", "failure", ("success", "failure"), job_id=2)],
            log_for_attempt=logs,
        )
        self.assertEqual(code, 0)
        creates = {a[1]: a[2] for a in actions if a != "label"}
        self.assertEqual(len(creates), 2)
        class_body = next(b for t, b in creates.items() if t.endswith("m.A"))
        step_body = next(b for t, b in creates.items() if t.endswith("red / step 2"))
        self.assertEqual([r["failed_attempt"] for r in reporter.occurrences_in(class_body)], [1])
        self.assertEqual([r["failed_attempt"] for r in reporter.occurrences_in(step_body)], [2])

    def test_a_cancelled_attempt_between_red_and_green_is_skipped(self):
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "cancelled"), 3: attempt(3, "success")}
        code, actions, _ = self._run(attempts, ["--run-id", "1", "--attempt", "3"], jobs=[job("red", "failure", job_id=2)])
        self.assertEqual(code, 0)
        helm = next(a[2] for a in actions if a != "label" and a[1].endswith("PublishHelmChartScriptTest"))
        self.assertEqual([r["failed_attempt"] for r in reporter.occurrences_in(helm)], [1])

    def test_the_latest_attempt_is_implied_when_none_is_named(self):
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "success")}
        code, actions, _ = self._run(attempts, ["--run-id", "34297433610"], jobs=[job("red", "failure", job_id=2)])
        self.assertEqual(code, 0)
        self.assertEqual(len([a for a in actions if a != "label"]), 2)

    def test_a_re_run_that_still_fails_records_nothing(self):
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "failure")}
        code, actions, logged = self._run(attempts, ["--run-id", "1", "--attempt", "2"])
        self.assertEqual((code, actions), (0, []))
        self.assertTrue(any("nothing to record" in line for line in logged))

    def test_a_first_attempt_records_nothing(self):
        attempts = {1: attempt(1, "success")}
        code, actions, _ = self._run(attempts, ["--run-id", "1", "--attempt", "1"])
        self.assertEqual((code, actions), (0, []))

    def test_an_unknown_attempt_records_nothing(self):
        attempts = {1: attempt(1, "failure")}
        code, actions, _ = self._run(attempts, ["--run-id", "1", "--attempt", "7"])
        self.assertEqual((code, actions), (0, []))

    def test_a_push_run_has_no_pull_request(self):
        attempts = {1: attempt(1, "failure", branch="main"), 2: attempt(2, "success", branch="main")}
        code, actions, _ = self._run(attempts, ["--run-id", "1", "--attempt", "2"], jobs=[job("red", "failure", job_id=2)], pulls=())
        self.assertEqual(code, 0)
        self.assertIn("| `main` |", actions[1][2])

    def test_a_failure_with_no_test_ids_is_one_issue_per_job_and_step(self):
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "success")}
        code, actions, _ = self._run(
            attempts,
            ["--run-id", "1", "--attempt", "2"],
            jobs=[job("red", "failure", ("success", "failure"), job_id=2)],
            log_for_attempt={1: "2026-09-09T01:04:59Z ##[error]Process completed with exit code 2.\n"},
        )
        self.assertEqual(code, 0)
        creates = [a for a in actions if a != "label"]
        self.assertEqual(len(creates), 1)
        self.assertTrue(creates[0][1].endswith("red / step 2"))
        self.assertEqual(len(reporter.occurrences_in(creates[0][2])), 1)

    def test_one_refused_write_does_not_block_the_other_keys_and_the_run_still_fails(self):
        logs = {1: "2026-09-09T01:04:59Z FAIL: test_1 (m.A.test_1)\n2026-09-09T01:04:59Z FAIL: test_2 (m.B.test_2)\n"}
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "success")}
        code, actions, logged = self._run(
            attempts,
            ["--run-id", "1", "--attempt", "2"],
            jobs=[job("red", "failure", job_id=2)],
            log_for_attempt=logs,
            refuse_title_ending="m.A",
        )
        self.assertEqual(code, 1)
        creates = [a for a in actions if a != "label"]
        self.assertEqual([a[1][-3:] for a in creates], ["m.B"])
        self.assertTrue(any("not recorded" in line for line in logged))

    def test_dry_run_writes_nothing(self):
        attempts = {1: attempt(1, "failure"), 2: attempt(2, "success")}
        code, actions, logged = self._run(attempts, ["--run-id", "1", "--attempt", "2", "--dry-run"], jobs=[job("red", "failure", job_id=2)])
        self.assertEqual((code, actions), (0, []))
        self.assertTrue(any("--dry-run" in line for line in logged))

    def test_no_token_is_an_error(self):
        code, actions, logged = self._run({1: attempt(1, "failure")}, ["--run-id", "1"], env_token=None)
        self.assertEqual((code, actions), (1, []))
        self.assertTrue(any("GITHUB_TOKEN" in line for line in logged))


class ParseArgsTest(unittest.TestCase):
    def test_a_run_id_or_a_sweep_is_required_and_not_both(self):
        with self.assertRaises(SystemExit):
            reporter.parse_args([])
        with self.assertRaises(SystemExit):
            reporter.parse_args(["--run-id", "1", "--sweep"])
        self.assertTrue(reporter.parse_args(["--sweep"]).sweep)
        self.assertEqual(reporter.parse_args(["--run-id", "7", "--attempt", "2"]).attempt, 2)


class _StatefulSweepAPI:
    """A fake for the whole sweep: lists the given runs for workflow 100,
    answers attempt and job reads with a failed first attempt and the unittest
    log, and keeps the issues `record` writes so later listings see them."""

    def __init__(self, names_to_ids, runs_by_workflow):
        self.names_to_ids = names_to_ids
        self.runs_by_workflow = runs_by_workflow
        self.issues = {"open": [], "closed": []}
        self.created = []
        self.updated = []
        self.comments = []
        self.reads = 0
        self.next_number = 900

    def workflows(self):
        return [{"id": workflow_id, "name": name, "path": "x"} for name, workflow_id in self.names_to_ids.items()]

    def successful_runs_between(self, workflow_id, start, end):
        return list(self.runs_by_workflow.get(workflow_id, []))

    def _attempt(self, run_id, number, conclusion):
        return dict(attempt(number, conclusion), id=run_id, workflow_id=100, html_url=f"https://github.com/gke-labs/kube-agents/actions/runs/{run_id}")

    def run(self, run_id):
        self.reads += 1
        return self._attempt(run_id, 2, "success")

    def attempt(self, run_id, number):
        self.reads += 1
        return self._attempt(run_id, number, "failure" if number == 1 else "success")

    def attempt_jobs(self, run_id, number):
        return [job("red", "failure", job_id=2)]

    def job_log(self, job_id):
        return UNITTEST_LOG

    def pull_requests_for(self, run):
        return [1321]

    def issues_for_workflow(self, workflow_id, state):
        prefix = reporter.workflow_marker(workflow_id)
        return [dict(issue) for issue in self.issues[state] if prefix in (issue.get("body") or "")]

    def ensure_label(self):
        pass

    def create_issue(self, title, body):
        self.next_number += 1
        issue = {"number": self.next_number, "title": title, "body": body}
        self.issues["open"].append(issue)
        self.created.append(issue)
        return {"number": issue["number"]}

    def update_issue(self, number, **fields):
        for issue in self.issues["open"]:
            if issue["number"] == number:
                issue.update(fields)
        self.updated.append((number, fields))

    def comment(self, number, body):
        self.comments.append((number, body))


class SweepTest(unittest.TestCase):
    """The scheduled path: no event to start from. The sweep lists each
    watched workflow's recent successful runs, and a run whose latest
    attempt is not its first is a re-run that went green -- the event the
    `workflow_run` trigger used to deliver, six hundred times a day, for
    the handful of runs that were one."""

    NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def _sweep(self, workflows, runs_by_workflow, argv=("--sweep",), issues=None, record=None):
        """Run `main --sweep` against a fake API. `workflows` may be an
        exception to raise from the list read; `runs_by_workflow` maps a
        workflow id to its listed runs, or is an exception, or a callable."""
        api = mock.Mock()
        if isinstance(workflows, BaseException):
            api.workflows.side_effect = workflows
        else:
            api.workflows.return_value = workflows
        if isinstance(runs_by_workflow, BaseException):
            api.successful_runs_between.side_effect = runs_by_workflow
        elif callable(runs_by_workflow):
            api.successful_runs_between.side_effect = runs_by_workflow
        else:
            api.successful_runs_between.side_effect = lambda workflow_id, start, end: list(runs_by_workflow.get(workflow_id, []))
        issues = issues or {}
        api.issues_for_workflow.side_effect = lambda workflow_id, state: list(issues.get((workflow_id, state), []))
        record_patch = (
            mock.patch.object(reporter, "record", side_effect=record)
            if record
            else mock.patch.object(reporter, "record", return_value=0)
        )
        logged = []
        with mock.patch.object(reporter, "GitHubAPI", return_value=api), mock.patch.object(
            reporter, "log", logged.append
        ), mock.patch.object(reporter, "_utcnow", return_value=self.NOW), mock.patch.dict(
            "os.environ", {"GITHUB_TOKEN": "t"}, clear=True
        ), record_patch as recorded:
            status = reporter.main(list(argv))
        self.logged = logged
        return status, api, recorded

    def _workflows(self, names_to_ids):
        return [{"id": workflow_id, "name": name, "path": f".github/workflows/{workflow_id}.yml"} for name, workflow_id in names_to_ids.items()]

    def _ids(self, names=None):
        return {name: 100 + index for index, name in enumerate(names or reporter.WATCHED_WORKFLOWS)}

    def test_only_re_run_greens_of_watched_workflows_are_recorded(self):
        names_to_ids = self._ids()
        names_to_ids["Validate PR Title"] = 999
        runs = {100: [listed_run(1, 1), listed_run(2, 2), listed_run(3, 1)], 999: [listed_run(4, 3)]}
        status, api, record = self._sweep(self._workflows(names_to_ids), runs)
        self.assertEqual(status, 0)
        # The fake answers every slice with the same runs, so a candidate
        # lists once per slice and is still read once.
        self.assertEqual([(call.args[2], call.args[3]) for call in record.call_args_list], [(2, 2)])
        self.assertNotIn(999, [call.args[0] for call in api.successful_runs_between.call_args_list])
        slices_per_workflow = reporter.SWEEP_WINDOW_DAYS // reporter.SLICE_DAYS
        self.assertEqual(len(api.successful_runs_between.call_args_list), len(reporter.WATCHED_WORKFLOWS) * slices_per_workflow)

    def test_the_window_is_three_days_of_run_creation_listed_a_day_at_a_time(self):
        """A re-run keeps its run's `created_at`: the green behind #2870 came
        twenty hours after the run was created. The list endpoint filters on
        creation and nothing else, so the window is days wide -- and it is
        asked for a day at a time, because one query lists a thousand runs
        at most and the busiest workflow's three days were past half that."""
        status, api, _ = self._sweep(self._workflows(self._ids()), {})
        self.assertEqual(status, 0)
        asked = {(call.args[1], call.args[2]) for call in api.successful_runs_between.call_args_list}
        stamp = lambda days: (self.NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        self.assertEqual(asked, {(stamp(1), stamp(0)), (stamp(2), stamp(1)), (stamp(3), stamp(2))})
        self.assertEqual(reporter.SWEEP_WINDOW_DAYS, 3)

    def test_a_run_an_issue_already_records_is_skipped_without_reading_it(self):
        """Every sweep sees the same re-runs for three days. The issue bodies
        say which are recorded, so those cost a listing and no log reads."""
        names_to_ids = self._ids()
        runs = {100: [listed_run(2, 2), listed_run(5, 3)]}
        issues = {(100, "open"): [recording_issue(700, 100, (2, 2))]}
        status, api, record = self._sweep(self._workflows(names_to_ids), runs, issues=issues)
        self.assertEqual(status, 0)
        self.assertEqual([call.args[2] for call in record.call_args_list], [5])
        # `record` lists again before it writes rather than reusing the
        # sweep's listing: see the shared-fingerprint test below.
        self.assertEqual(len(record.call_args_list[0].args), 5)

    def test_a_green_the_hidden_keys_name_is_skipped_once_its_row_is_trimmed(self):
        issue = {"number": 700, "body": f"{reporter.issue_marker(100, 'abc')}\n{reporter.SEEN_PREFIX}2:1:2{reporter.OCCURRENCE_SUFFIX}"}
        status, _, record = self._sweep(self._workflows(self._ids()), {100: [listed_run(2, 2)]}, issues={(100, "open"): [issue]})
        self.assertEqual(status, 0)
        self.assertEqual(record.call_args_list, [])

    def test_candidates_are_recorded_oldest_first(self):
        """The list comes newest first; the rows should land in the order the
        runs happened."""
        status, _, record = self._sweep(self._workflows(self._ids()), {100: [listed_run(9, 2), listed_run(4, 2), listed_run(6, 3)]})
        self.assertEqual(status, 0)
        self.assertEqual([call.args[2] for call in record.call_args_list], [4, 6, 9])

    def test_a_re_run_storm_past_the_row_cap_settles_after_one_sweep(self):
        """More runs than the table keeps, all one flake, re-run green in one
        window. The first sweep records them; the second finds every one on
        the issue's hidden keys and writes nothing, comments nothing."""
        count = reporter.MAX_OCCURRENCES + 4
        api = _StatefulSweepAPI(self._ids(), {100: [listed_run(1000 + n, 2) for n in range(count)]})
        patches = (
            mock.patch.object(reporter, "GitHubAPI", return_value=api),
            mock.patch.object(reporter, "log", lambda message: None),
            mock.patch.object(reporter, "_utcnow", return_value=self.NOW),
            mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t"}, clear=True),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            self.assertEqual(reporter.main(["--sweep"]), 0)
            after_first = (len(api.created), len(api.updated), len(api.comments), api.reads)
            self.assertEqual(reporter.main(["--sweep"]), 0)
        self.assertEqual(len(api.created), 2)
        for issue in api.issues["open"]:
            self.assertEqual(len(reporter.occurrences_in(issue["body"])), reporter.MAX_OCCURRENCES)
            self.assertEqual(len(reporter.seen_in(issue["body"])), count)
        self.assertEqual((len(api.created), len(api.updated), len(api.comments)), after_first[:3])
        self.assertEqual(api.reads, after_first[3], "the second sweep read no run")

    def test_a_closed_issue_counts_as_a_record_too(self):
        runs = {100: [listed_run(2, 2)]}
        issues = {(100, "closed"): [recording_issue(600, 100, (2, 2))]}
        status, _, record = self._sweep(self._workflows(self._ids()), runs, issues=issues)
        self.assertEqual(status, 0)
        self.assertEqual(record.call_args_list, [])

    def test_a_recorded_run_re_run_again_is_read_for_its_new_attempt(self):
        """Attempt 1 red, 2 green, recorded; then 3 red and 4 green. The run
        id is on the issue, the green attempt is not, so the sweep hands it to
        `record` again (which then adds attempt 3's row and no other: the
        merge dedupes per failed attempt, tested with `reconcile` above)."""
        runs = {100: [listed_run(2, 4)]}
        issues = {(100, "open"): [recording_issue(700, 100, (2, 2))]}
        status, _, record = self._sweep(self._workflows(self._ids()), runs, issues=issues)
        self.assertEqual(status, 0)
        self.assertEqual([(call.args[2], call.args[3]) for call in record.call_args_list], [(2, 4)])

    def test_two_candidates_sharing_a_fingerprint_open_one_issue(self):
        """Two green re-runs of one flake inside one sweep -- a shared fixture
        hitting two pull requests, or the re-run storm after an outage. With
        the real `record` and `reconcile`, and an API whose issue list grows
        as they write, the second candidate finds the first one's issue."""
        api = _StatefulSweepAPI(self._ids(), {100: [listed_run(2, 2), listed_run(5, 2)]})
        with mock.patch.object(reporter, "GitHubAPI", return_value=api), mock.patch.object(
            reporter, "log", lambda message: None
        ), mock.patch.object(reporter, "_utcnow", return_value=self.NOW), mock.patch.dict(
            "os.environ", {"GITHUB_TOKEN": "t"}, clear=True
        ):
            status = reporter.main(["--sweep"])
        self.assertEqual(status, 0)
        # The log names two classes, so two issues: one per class, each
        # created once and then updated with the second run's row.
        self.assertEqual(len(api.created), 2, [issue["title"] for issue in api.created])
        self.assertEqual(len({issue["title"] for issue in api.created}), 2)
        self.assertEqual(sorted(number for number, _ in api.updated), sorted(issue["number"] for issue in api.created))
        for issue in api.issues["open"]:
            self.assertEqual(sorted(record["run_id"] for record in reporter.occurrences_in(issue["body"])), [2, 5])

    def test_a_defect_on_one_runs_shape_raises_out_of_the_sweep(self):
        """A read that failed is left to the next sweep; a bug in this script
        is not. Here the listing raises a `TypeError`, standing in for any
        defect on the path: it leaves `main` and reds the run, where a warning
        four times an hour on a green job would hide it."""
        with self.assertRaises(TypeError):
            self._sweep(self._workflows(self._ids()), TypeError("run has no attempt"))

    def test_no_issue_is_listed_for_a_workflow_with_no_candidate(self):
        status, api, _ = self._sweep(self._workflows(self._ids()), {100: [listed_run(1, 1)]})
        self.assertEqual(status, 0)
        self.assertEqual(api.issues_for_workflow.call_args_list, [])

    def test_a_watched_name_no_workflow_carries_reds_the_sweep_after_doing_the_rest(self):
        """A rename silently stops a name matching. The sweep is the only thing
        positioned to notice, and says so with its exit status -- after
        sweeping what it could find."""
        names_to_ids = self._ids(reporter.WATCHED_WORKFLOWS[1:])
        first_present = names_to_ids[reporter.WATCHED_WORKFLOWS[1]]
        status, _, record = self._sweep(self._workflows(names_to_ids), {first_present: [listed_run(2, 2)]})
        self.assertEqual(status, 1)
        self.assertEqual([call.args[2] for call in record.call_args_list], [2])
        self.assertTrue(any(reporter.WATCHED_WORKFLOWS[0] in line and "named" in line for line in self.logged))

    def test_a_ghost_carrying_a_watched_name_is_listed_as_well(self):
        """The workflow list keeps an entry for a file that no longer exists.
        Listing its runs is one request and finds nothing inside the window,
        so both carriers are listed rather than guessed between."""
        workflows = self._workflows(self._ids()) + [{"id": 555, "name": reporter.WATCHED_WORKFLOWS[0], "path": ".github/workflows/old.yml"}]
        status, api, _ = self._sweep(workflows, {})
        self.assertEqual(status, 0)
        self.assertIn(555, [call.args[0] for call in api.successful_runs_between.call_args_list])

    def test_the_workflow_list_failing_past_retries_leaves_the_sweep_to_the_next_one(self):
        status, _, record = self._sweep(urllib.error.URLError("down"), {})
        self.assertEqual(status, 0)
        self.assertEqual(record.call_args_list, [])
        self.assertTrue(any("next" in line for line in self.logged))

    def test_a_refused_workflow_list_reds_the_sweep(self):
        refused = urllib.error.HTTPError("/workflows", 403, "forbidden", {}, None)
        status, _, _ = self._sweep(refused, {})
        self.assertEqual(status, 1)

    def test_one_workflows_failed_read_does_not_stop_the_rest(self):
        names_to_ids = self._ids()

        def runs(workflow_id, start, end):
            if workflow_id == 100:
                raise urllib.error.URLError("reset")
            return [listed_run(workflow_id, 2)] if workflow_id == 101 else []

        status, _, record = self._sweep(self._workflows(names_to_ids), runs)
        self.assertEqual(status, 0)
        self.assertEqual([call.args[2] for call in record.call_args_list], [101])
        self.assertTrue(any("next sweep" in line for line in self.logged))

    def test_a_request_github_refused_reds_the_sweep_after_the_rest_ran(self):
        def runs(workflow_id, start, end):
            if workflow_id == 100:
                raise urllib.error.HTTPError("/runs", 404, "gone", {}, None)
            return [listed_run(workflow_id, 2)] if workflow_id == 101 else []

        status, _, record = self._sweep(self._workflows(self._ids()), runs)
        self.assertEqual(status, 1)
        self.assertEqual([call.args[2] for call in record.call_args_list], [101])

    def test_a_record_that_could_not_be_written_reds_the_sweep(self):
        status, _, _ = self._sweep(self._workflows(self._ids()), {100: [listed_run(2, 2)]}, record=lambda *a, **k: 1)
        self.assertEqual(status, 1)

    def test_a_dry_run_sweep_passes_dry_run_through(self):
        status, _, record = self._sweep(self._workflows(self._ids()), {100: [listed_run(2, 2)]}, argv=("--sweep", "--dry-run"))
        self.assertEqual(status, 0)
        self.assertIs(record.call_args_list[0].args[4], True)


class HiddenLineParsingTest(unittest.TestCase):
    """A hand edit can leave either hidden line in a shape the script never
    writes. The parsers refuse those, because a key of the wrong length or a
    bare value would otherwise raise inside the sweep and red every pass."""

    def test_a_key_without_three_integer_fields_is_dropped(self):
        body = f"{reporter.SEEN_PREFIX}5,7:1:2,x:1:2,8:1,9:1:2:3,,10:2:3{reporter.OCCURRENCE_SUFFIX}"
        self.assertEqual(reporter.seen_in(body), [(7, 1, 2), (10, 2, 3)])

    def test_an_occurrence_that_is_not_an_object_with_its_keys_is_dropped(self):
        lines = [
            f"{reporter.OCCURRENCE_PREFIX}5{reporter.OCCURRENCE_SUFFIX}",
            f'{reporter.OCCURRENCE_PREFIX}{{"foo": 1}}{reporter.OCCURRENCE_SUFFIX}',
            f"{reporter.OCCURRENCE_PREFIX}[1, 2]{reporter.OCCURRENCE_SUFFIX}",
            f'{reporter.OCCURRENCE_PREFIX}{{"run_id": 3, "failed_attempt": 1, "passed_attempt": 2}}{reporter.OCCURRENCE_SUFFIX}',
        ]
        self.assertEqual(reporter.occurrences_in("\n".join(lines)), [{"run_id": 3, "failed_attempt": 1, "passed_attempt": 2}])

    def test_a_malformed_body_neither_raises_in_the_dedupe_nor_in_the_merge(self):
        body = f"{reporter.SEEN_PREFIX}5{reporter.OCCURRENCE_SUFFIX}\n{reporter.OCCURRENCE_PREFIX}5{reporter.OCCURRENCE_SUFFIX}"
        self.assertEqual(reporter.recorded_greens(([{"body": body}], [])), set())
        merged, added = reporter.merge_occurrences(reporter.occurrences_in(body), [], reporter.seen_in(body))
        self.assertEqual((merged, added), ([], []))


class SuccessfulRunsBetweenTest(unittest.TestCase):
    """The list read behind the sweep: every page of one workflow's successful
    runs created inside one slice, newest first, as the endpoint orders them."""

    def _api(self, pages, total):
        calls = []

        class API(reporter.GitHubAPI):
            def get(self, path, tolerate=()):
                calls.append(path)
                page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
                return {"total_count": total, "workflow_runs": pages[page - 1]}

        return API(REPO, "tok"), calls

    def test_every_page_is_read_until_a_short_one(self):
        pages = [[listed_run(i, 1) for i in range(reporter.PAGE_SIZE)], [listed_run(500, 2)]]
        api, calls = self._api(pages, reporter.PAGE_SIZE + 1)
        runs = api.successful_runs_between(WORKFLOW_ID, "2026-10-11T12:00:00Z", "2026-10-12T12:00:00Z")
        self.assertEqual(len(runs), reporter.PAGE_SIZE + 1)
        self.assertEqual(len(calls), 2)
        query = dict(urllib.parse.parse_qsl(calls[0].split("?", 1)[1]))
        self.assertEqual(query["status"], "success")
        self.assertEqual(query["created"], "2026-10-11T12:00:00Z..2026-10-12T12:00:00Z")
        self.assertTrue(calls[0].startswith(f"/repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs?"))

    def test_the_endpoints_own_cap_ends_the_read_with_a_warning(self):
        """The endpoint lists a thousand runs at most. A window that holds
        more is not read past that, and the log says the oldest were not."""
        full = [listed_run(i, 1) for i in range(reporter.PAGE_SIZE)]
        pages = [full] * (reporter.RUNS_LIST_CAP // reporter.PAGE_SIZE + 2)
        api, calls = self._api(pages, reporter.RUNS_LIST_CAP + 300)
        logged = []
        with mock.patch.object(reporter, "log", logged.append):
            runs = api.successful_runs_between(WORKFLOW_ID, "2026-10-11T12:00:00Z", "2026-10-12T12:00:00Z")
        self.assertEqual(len(runs), reporter.RUNS_LIST_CAP)
        self.assertEqual(len(calls), reporter.RUNS_LIST_CAP // reporter.PAGE_SIZE)
        self.assertTrue(any("not swept" in line for line in logged))

    def test_exactly_the_cap_is_read_whole_and_warns_about_nothing(self):
        full = [listed_run(i, 1) for i in range(reporter.PAGE_SIZE)]
        pages = [full] * (reporter.RUNS_LIST_CAP // reporter.PAGE_SIZE + 1)
        api, calls = self._api(pages, reporter.RUNS_LIST_CAP)
        logged = []
        with mock.patch.object(reporter, "log", logged.append):
            runs = api.successful_runs_between(WORKFLOW_ID, "2026-10-11T12:00:00Z", "2026-10-12T12:00:00Z")
        self.assertEqual(len(runs), reporter.RUNS_LIST_CAP)
        self.assertEqual(len(calls), reporter.RUNS_LIST_CAP // reporter.PAGE_SIZE)
        self.assertEqual(logged, [])


class WorkflowShapeTest(unittest.TestCase):
    """The workflow is a schedule and a dispatch, and nothing event-driven: a
    `workflow_run` trigger put back here would start a run on every completion
    of every watched workflow again (#2943)."""

    def setUp(self):
        self.document, self.triggers = workflow_document()

    def test_it_starts_on_a_schedule_and_a_dispatch_only(self):
        self.assertEqual(sorted(self.triggers), ["schedule", "workflow_dispatch"])
        self.assertEqual(self.triggers["schedule"], [{"cron": "*/15 * * * *"}])

    def test_the_dispatch_takes_a_dry_run_that_defaults_on_and_an_optional_run_id(self):
        inputs = self.triggers["workflow_dispatch"]["inputs"]
        self.assertIs(inputs["dry_run"]["default"], True)
        self.assertIn("run_id", inputs)
        self.assertFalse(inputs["run_id"].get("required", False))

    def test_one_sweep_at_a_time_and_a_dispatched_run_in_a_group_of_its_own(self):
        """GitHub holds one pending run per group and cancels it when another
        arrives. Two sweeps read the same window, so that costs nothing; a
        `run_id` dispatch is the one record no sweep will redo, so it must not
        wait in the sweeps' group."""
        self.assertEqual(
            self.document["concurrency"],
            {"group": "${{ github.workflow }}-${{ inputs.run_id || 'sweep' }}", "cancel-in-progress": False},
        )

    def test_the_job_is_guarded_and_each_entry_point_has_its_step(self):
        job = self.document["jobs"]["notify"]
        self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"])
        steps = {step["name"]: step for step in job["steps"] if "notify_flaky_check.py" in step.get("run", "")}
        self.assertEqual(sorted(steps), ["Record one run", "Sweep the watched workflows"])
        one = steps["Record one run"]
        self.assertEqual(one["if"], "github.event_name == 'workflow_dispatch' && inputs.run_id != ''")
        self.assertIn('--run-id "$RUN_ID"', one["run"])
        self.assertEqual(one["env"]["RUN_ID"], "${{ inputs.run_id }}")
        sweep = steps["Sweep the watched workflows"]
        self.assertEqual(sweep["if"], "github.event_name == 'schedule' || inputs.run_id == ''")
        self.assertEqual(sweep["env"]["DRY_RUN"], "${{ inputs.dry_run }}")
        branches = [line.strip() for line in sweep["run"].splitlines() if "notify_flaky_check.py" in line]
        self.assertEqual(
            branches,
            ["python3 scripts/notify_flaky_check.py --sweep --dry-run", "python3 scripts/notify_flaky_check.py --sweep"],
            "a dry-run branch and a writing branch, in that order",
        )
        # Only the sweep warns; the one-run path has nothing to annotate.
        self.assertEqual(sweep["env"]["FLAKY_CHECK_ANNOTATE"], "true")
        self.assertNotIn("FLAKY_CHECK_ANNOTATE", one["env"])

    def test_the_token_can_read_runs_and_write_issues_and_nothing_else(self):
        job = self.document["jobs"]["notify"]
        self.assertEqual(job["permissions"], {"contents": "read", "actions": "read", "issues": "write"})


class WatchListTest(unittest.TestCase):
    def test_the_watched_workflows_all_exist_under_that_name(self):
        """The list matches on each workflow's `name:`; a rename silently
        drops one. The sweep reds on a missing name at run time, and this
        catches the same thing before the merge."""
        names = set()
        for path in (_HERE.parent / ".github" / "workflows").glob("*.yml"):
            document = yaml.safe_load(path.read_text()) or {}
            names.add(document.get("name"))
        missing = sorted(set(reporter.WATCHED_WORKFLOWS) - names)
        self.assertEqual([], missing, "no workflow is named: " + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
