#!/usr/bin/env python3
"""Unit tests for the flaky-check issue reporter.

Run: cd scripts && python3 -m unittest test_notify_flaky_check

The reporter cannot be exercised end to end before it is on main -- a
`workflow_run` workflow only runs from the default branch's copy of itself --
so everything that can be decided without a runner is decided here. The
failure modes worth the most: recording a flake that is not one (a re-run of a
cancelled attempt, a re-run that still failed), which teaches everyone to
ignore the label; splitting one flake across several issues because the
fingerprint drifted; and doubling a row when the same event is handled twice.
"""

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import notify_flaky_check as reporter

REPO = "gke-labs/kube-agents"
WORKFLOW_ID = 327115835
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


def existing_issue(number, lines, records, workflow_id=WORKFLOW_ID):
    marker = reporter.issue_marker(workflow_id, reporter.digest(workflow_id, lines))
    return {"number": number, "body": reporter.render_body(REPO, "Python Unit Tests", lines, records, marker)}


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


if __name__ == "__main__":
    unittest.main()
