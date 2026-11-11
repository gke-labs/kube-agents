"""The sized fleet run's decisions: which audits, when to mark them, when the burst ends, and the verdict."""

import unittest
from unittest import mock

from kube_agents_bench.scale import sized_run as sr


class AuditsTest(unittest.TestCase):
    def test_the_audits_are_the_fleet_audit_jobs(self):
        audits = sr.audits()
        self.assertEqual(len(audits), 10)
        self.assertIn("compliance-audit", audits)
        self.assertNotIn("stall-watch", audits)

    def test_the_first_run_audits_come_from_oobe(self):
        first = sr.first_run_audits()
        self.assertEqual(first[0], "fleet-wide-cost-analysis")
        self.assertTrue(set(first) <= set(sr.audits()))

    def test_the_other_five_are_marked_every_five_minutes(self):
        others = tuple(j for j in sr.audits() if j not in sr.first_run_audits())
        self.assertEqual(len(others), 5)
        plan = sr.stagger(others)
        self.assertEqual([t for t, _ in plan], [300, 600, 900, 1200, 1500])
        self.assertEqual([j for _, j in plan], list(others))


class BurstTest(unittest.TestCase):
    wanted = ("a", "b")

    def test_not_before_an_hour_even_when_every_audit_ended(self):
        self.assertFalse(sr.burst_over(1800, {"a": "completed", "b": "completed"}, self.wanted))

    def test_after_an_hour_once_every_audit_ended(self):
        self.assertFalse(sr.burst_over(3700, {"a": "completed", "b": "running"}, self.wanted))
        self.assertTrue(sr.burst_over(3700, {"a": "completed", "b": "failed"}, self.wanted))

    def test_at_three_hours_whatever_is_running(self):
        self.assertTrue(sr.burst_over(3 * 3600, {"a": "running"}, self.wanted))


class VerdictTest(unittest.TestCase):
    wanted = ("a", "b", "c")
    first = ("a", "b")

    def green_record(self):
        record = sr.RunRecord(started=0.0, burst_seconds=3900)
        record.statuses = {"a": "completed", "b": "completed", "c": "completed"}
        record.pods_before = {"gw": sr.PodState(0, False)}
        record.pods_after = {"gw": sr.PodState(0, False)}
        record.fleet_check = {"findings": []}
        return record

    def test_a_clean_run_is_green(self):
        result = sr.verdict(self.green_record(), self.wanted, self.first)
        self.assertTrue(result["green"], result)

    def test_a_restart_an_oom_kill_and_a_sandbox_change_are_crashes(self):
        record = self.green_record()
        record.pods_after = {"gw": sr.PodState(2, True)}
        record.sandbox_changed = ["2026-11-10T22:00:00Z shell"]
        result = sr.verdict(record, self.wanted, self.first)
        failures = result["checks"]["no_crash"]["failures"]
        self.assertFalse(result["green"])
        self.assertIn("gw: 2 restart(s)", failures)
        self.assertIn("gw: OOMKilled", failures)
        self.assertTrue(any("SandboxChanged" in f for f in failures))

    def test_a_pod_that_was_already_restarted_before_the_run_is_not_a_crash(self):
        record = self.green_record()
        record.pods_before = {"gw": sr.PodState(3, True)}
        record.pods_after = {"gw": sr.PodState(3, True)}
        self.assertTrue(sr.verdict(record, self.wanted, self.first)["checks"]["no_crash"]["passed"])

    def test_an_audit_that_did_not_complete_fails_its_check(self):
        record = self.green_record()
        record.statuses["c"] = "running"
        del record.statuses["b"]
        checks = sr.verdict(record, self.wanted, self.first)["checks"]
        self.assertEqual(checks["every_audit_finishes"]["failures"], ["b: not run", "c: running"])
        self.assertEqual(checks["first_run_flow_ends"]["failures"], ["b: not run"])

    def test_an_unchecked_or_unsound_fleet_fails_the_run(self):
        record = self.green_record()
        record.fleet_check = {}
        self.assertFalse(sr.verdict(record, self.wanted, self.first)["checks"]["run_sound"]["passed"])
        record.fleet_check = {"findings": ["sized-m-01: 30 ready nodes, designed 32"]}
        self.assertFalse(sr.verdict(record, self.wanted, self.first)["green"])


class RunTest(unittest.TestCase):
    def test_marks_each_other_audit_on_the_stagger_and_ends_the_burst_when_every_audit_ends(self):
        now = [0.0]
        marked = []
        statuses = {j: "completed" for j in sr.audits()}

        def sleep(seconds):
            now[0] += seconds

        with mock.patch.object(sr.operations, "reset"), \
                mock.patch.object(sr.operations, "trigger_jobs", side_effect=lambda c, n, h, jobs: marked.append((now[0], jobs[0])) or []), \
                mock.patch.object(sr.operations, "job_statuses", return_value=statuses), \
                mock.patch.object(sr.operations, "job_seconds", return_value={}), \
                mock.patch.object(sr, "pod_states", return_value={}), \
                mock.patch.object(sr, "log_lines", return_value=[]):
            record, result = sr.run(project="p", host_cluster="h", host_context="ctx", namespace="ns",
                                    check_fleet=lambda: {"findings": []}, soak_seconds=3600,
                                    clock=lambda: now[0], sleep=sleep)
        self.assertEqual([t for t, _ in marked], [300, 600, 900, 1200, 1500])
        self.assertEqual(record.burst_seconds, 3600)
        self.assertTrue(result["green"], result)


if __name__ == "__main__":
    unittest.main()


class RemarkTest(unittest.TestCase):
    def test_an_unclaimed_mark_is_made_again_after_ten_minutes(self):
        marks = {"a": (0.0, 1), "b": (0.0, 1)}
        self.assertEqual(sr.to_remark(marks, {"a": "unclaimed", "b": "running"}, 599), [])
        self.assertEqual(sr.to_remark(marks, {"a": "unclaimed", "b": "running"}, 600), ["a"])

    def test_a_job_with_no_row_yet_counts_as_unclaimed(self):
        self.assertEqual(sr.to_remark({"a": (0.0, 1)}, {}, 700), ["a"])

    def test_no_more_than_five_attempts(self):
        self.assertEqual(sr.to_remark({"a": (0.0, 5)}, {}, 7000), [])


class VerdictTimingTest(unittest.TestCase):
    wanted = ("a", "b")
    first = ("a",)

    def record(self):
        record = sr.RunRecord(started=0.0)
        record.pods_before = record.pods_after = {"gw": sr.PodState()}
        record.fleet_check = {"findings": []}
        return record

    def test_an_audit_that_ends_only_in_the_soak_window_still_fails(self):
        record = self.record()
        record.burst_statuses = {"a": "completed", "b": "running"}
        record.statuses = {"a": "completed", "b": "completed"}
        checks = sr.verdict(record, self.wanted, self.first)["checks"]
        self.assertEqual(checks["every_audit_finishes"]["failures"], ["b: running"])

    def test_a_failed_first_run_audit_still_ends_the_flow(self):
        record = self.record()
        record.burst_statuses = {"a": "failed", "b": "completed"}
        checks = sr.verdict(record, self.wanted, self.first)["checks"]
        self.assertTrue(checks["first_run_flow_ends"]["passed"])
        self.assertFalse(checks["every_audit_finishes"]["passed"])

    def test_a_fault_the_run_recovered_from_is_a_note_not_a_failure(self):
        record = self.record()
        record.burst_statuses = {"a": "completed", "b": "completed"}
        record.errors = ["could not mark every job due: transient"]
        result = sr.verdict(record, self.wanted, self.first)
        self.assertTrue(result["green"])
        self.assertEqual(result["errors"], record.errors)
        self.assertIsNot(result["errors"], record.errors)
