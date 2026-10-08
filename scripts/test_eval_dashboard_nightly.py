"""nightly.py: one night of the nightly tier as a report, the digest line
about it, and -- when headless Chrome is present -- nightly.html and the
Brief's "Last night's run" block as a browser renders them.

The nights are synthetic (no real night exists yet: the periodic is not
merged); once one is on record, replaying it here is the follow-up #1491
names. Every asserted time is America/Toronto.
"""

import copy
import datetime
import json
import tempfile
import unittest
import unittest.mock

from eval_dashboard import nightly, post_health, render, trend
from test_eval_dashboard_pages import (
    chrome,
    dom_text,
    health_doc,
    load_fixture,
    render_to,
)

UTC = datetime.timezone.utc
NOW = "2026-09-08T14:30:00+00:00"  # Tue 10:30 AM ET
# Sun 8 PM ET is 00:00Z Monday; Mon 8 PM ET is 00:00Z Tuesday.
NIGHT_1 = "3000000000000000001"
NIGHT_2 = "3000000000000000002"
JOB = "ci-kube-agents-eval-nightly"
DIGEST_AT = datetime.datetime(2026, 9, 8, 13, 0, tzinfo=UTC)  # Tue 9 AM ET


def rep(n, result, reason=None):
    return {"n": n, "result": result, "reason": reason}


def task(name, *results, reason="check absent: required phrases absent"):
    reps = [rep(i + 1, r, reason if r != "pass" else None) for i, r in enumerate(results)]
    result = "pass" if all(r == "pass" for r in results) else ("infra" if all(r == "infra" for r in results) else "fail")
    return {"name": name, "result": result, "reps": reps}


def night(build, started, finished, tasks, result="FAILURE", duration_s=24000, **extra):
    run = {"build_id": build, "tier": "nightly", "job": JOB, "pr": None, "head_sha": build[-7:],
           "started": started, "finished": finished, "result": result, "duration_s": duration_s, "tasks": tasks}
    run.update(extra)
    return run


CASES = [
    {"name": "case-a", "domain": "cost", "active": True, "nightly_active": True},
    {"name": "case-b", "domain": "reliability", "active": True, "nightly_active": True},
    {"name": "case-c", "domain": "reliability", "active": False, "nightly_active": True},
    {"name": "old-only", "domain": "cost", "active": False, "nightly_active": False},
]
FIRST = night(NIGHT_1, "2026-09-07T00:00:00+00:00", "2026-09-07T06:40:00+00:00", [
    task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "fail", "fail", "fail", reason="old reason"),
])
SECOND = night(NIGHT_2, "2026-09-08T00:00:00+00:00", "2026-09-08T06:40:00+00:00", [
    task("case-a", "pass", "fail", "pass", reason="check x: <b>absent</b>"),
    task("case-b", "fail", "fail", "fail", reason="check y: required phrases absent"),
    task("case-c", "pass", "pass", "pass"),
], project="kube-agents-evals-3")


def two_nights(presubmit_runs=()):
    return {"schema_version": 1, "generated_at": NOW, "cases": copy.deepcopy(CASES), "runs": [*presubmit_runs, copy.deepcopy(FIRST), copy.deepcopy(SECOND)]}


class NightDocumentTest(unittest.TestCase):
    def test_states_counts_and_the_comparison_with_the_night_before(self):
        nights = nightly.night_reports(two_nights())
        self.assertEqual([n["build"] for n in nights], [NIGHT_2, NIGHT_1], "newest first")
        last = nights[0]
        self.assertEqual(last["counts"], {"expected": 3, "recorded": 3, "passed": 1, "partial": 1, "failed": 1, "infra": 0, "missing": 0})
        self.assertEqual([(c["case"], c["domain"], c["state"]) for c in last["cases"]],
                         [("case-a", "cost", "partial"), ("case-b", "reliability", "fail"), ("case-c", "reliability", "pass")], "by domain, then name")
        self.assertEqual(last["newly_failing"], ["case-b"])
        self.assertEqual(last["fixed"], ["case-c"])
        self.assertEqual(last["previous_build"], NIGHT_1)
        self.assertTrue(last["complete"])
        self.assertFalse(last["truncated"])
        self.assertEqual(last["log_url"], f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_2}")
        by_name = {c["case"]: c for c in last["cases"]}
        self.assertEqual(by_name["case-a"]["reps"], {"pass": 2, "fail": 1, "infra": 0})
        self.assertEqual(by_name["case-a"]["reason"], "check x: <b>absent</b>", "the first failing rep's reason, unescaped in the data")
        self.assertIsNone(by_name["case-c"]["reason"], "a pass carries no reason")
        self.assertEqual(by_name["case-b"]["transcript_url"], f"{last['log_url']}/artifacts/eval_case-b_rep1.log")
        first = nights[1]
        self.assertIsNone(first["previous_build"])
        self.assertEqual(first["newly_failing"], [], "the first night has nothing to compare with")
        self.assertEqual(first["counts"]["failed"], 1)

    def test_truncated_incomplete_and_infra_nights(self):
        data = two_nights()
        data["runs"].append(night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T08:00:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "infra", "infra", "infra", reason="KUBE_AGENTS_INFRA_FAILURE 429")],
                                  result="ABORTED", duration_s=None))
        last = nightly.night_reports(data)[0]
        self.assertTrue(last["truncated"])
        self.assertFalse(last["complete"])
        self.assertEqual(last["missing"], ["case-c"])
        self.assertEqual(last["counts"]["infra"], 1)
        self.assertEqual(last["counts"]["missing"], 1)
        self.assertEqual(last["duration_s"], 8 * 3600, "no verdict line: finished - started")
        self.assertEqual(last["newly_failing"], [], "case-b lost every rep to infra: not a failure")
        # Concluded but short of the matrix: incomplete, not truncated. (No
        # eval_verdict key at all: a record from before the collector wrote
        # it, unknown rather than truncated.)
        data["runs"][-1].update(result="FAILURE", duration_s=20000)
        last = nightly.night_reports(data)[0]
        self.assertFalse(last["truncated"])
        self.assertFalse(last["complete"])
        # Prow's deadline delivers SIGTERM and records FAILURE, not ABORTED
        # (collect.py's fixture 2092688354838581248): a night that is not a
        # SUCCESS and whose log has no verdict line is truncated too.
        data["runs"][-1].update(eval_verdict=None)
        self.assertTrue(nightly.night_reports(data)[0]["truncated"])
        data["runs"][-1].update(eval_verdict="RED")
        self.assertFalse(nightly.night_reports(data)[0]["truncated"], "graded to the end, short of the matrix: incomplete")
        data["runs"][-1].update(result="SUCCESS", eval_verdict=None)
        self.assertFalse(nightly.night_reports(data)[0]["truncated"], "a SUCCESS without a verdict line is step 0's revalidation, not a kill")

    def test_reps_absent_means_the_task_result_is_one_rep_and_bad_rows_are_skipped(self):
        data = two_nights()
        data["runs"][-1]["tasks"] = [{"name": "case-a", "result": "fail"}, {"name": "case-b", "result": "pass"}, {"name": "case-c", "result": "bogus"}, "junk", {"result": "pass"}]
        last = nightly.night_reports(data)[0]
        self.assertEqual([(c["case"], c["state"], c["reps"]) for c in last["cases"]],
                         [("case-a", "fail", {"pass": 0, "fail": 1, "infra": 0}), ("case-b", "pass", {"pass": 1, "fail": 0, "infra": 0})])
        self.assertEqual(last["missing"], ["case-c"])

    def test_expected_cases_read_nightly_active_and_fall_back_to_active(self):
        self.assertEqual(nightly.expected_cases(two_nights()), ["case-a", "case-b", "case-c"])
        legacy = {"cases": [{"name": "x", "active": True}, {"name": "y", "active": False}]}
        self.assertEqual(nightly.expected_cases(legacy), ["x"])

    def test_only_nightly_runs_are_nights_and_the_window_is_bounded(self):
        data = two_nights(presubmit_runs=[{"build_id": "1", "pr": 5, "started": "2026-09-08T01:00:00+00:00", "finished": "2026-09-08T02:00:00+00:00", "result": "FAILURE", "tasks": [task("case-a", "fail", "fail", "fail")]}])
        self.assertEqual([n["build"] for n in nightly.night_reports(data)], [NIGHT_2, NIGHT_1])
        self.assertEqual([n["build"] for n in nightly.night_reports(data, limit=1)], [NIGHT_2])
        self.assertEqual(nightly.night_reports(data, limit=1)[0]["previous_build"], NIGHT_1, "the night outside the window still serves as the comparison")
        self.assertEqual(nightly.nightly_document({"runs": [], "cases": []}), {"job": JOB, "nights": [], "running": []})

    def test_a_nightly_build_in_flight_is_a_running_night_not_a_grid_column(self):
        data = two_nights()
        data["pending_builds"] = [
            {"build_id": "3000000000000000003", "first_seen": "2026-09-08T14:05:00+00:00", "tier": "nightly"},
            {"build_id": "3000000000000000004", "first_seen": "2026-09-08T14:10:00+00:00"},  # the presubmit's
            {"build_id": "2999999999999999999", "first_seen": "2026-09-08T01:00:00+00:00", "tier": "nightly"},  # died 13 h ago
            {"build_id": "bogus", "first_seen": "2026-09-08T14:05:00+00:00", "tier": "nightly"},
        ]
        now = datetime.datetime.fromisoformat(NOW)
        self.assertEqual(nightly.running_nights(data, now), [
            {"build": "3000000000000000003", "first_seen": "2026-09-08T14:05:00+00:00",
             "log_url": f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/3000000000000000003"},
        ])
        self.assertEqual([r["build"] for r in nightly.running_nights(data, None)], ["2999999999999999999", "3000000000000000003"], "no clock: no age judged")
        self.assertEqual(nightly.running_nights({"pending_builds": "soon"}, now), [])
        brief = render.brief_document(data, None, None, None, admitted=frozenset(), demoted={})
        self.assertEqual([r["build"] for r in brief["nightly"]["running"]], ["3000000000000000003"], "judged against generated_at")
        self.assertEqual([p["build"] for p in brief["pending"]], ["3000000000000000004"], "the Grid's running columns are the presubmit's")


class BuildUrlTest(unittest.TestCase):
    """The links follow the bucket the collector read the build from
    (``runs[].log_url``); a record without the field is from the bucket the
    nightly used before 2026-09-15 and is linked there."""

    URL = "https://oss.gprow.dev/view/gs/kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly/3000000000000000005"

    def test_a_recorded_log_url_wins_and_the_transcript_hangs_off_it(self):
        run = {"build_id": "3000000000000000005", "job": JOB, "log_url": self.URL}
        self.assertEqual(nightly.build_url(run), self.URL)
        self.assertEqual(nightly.transcript_url(run, "case-b"), self.URL + "/artifacts/eval_case-b_rep1.log")
        # Not a Spyglass page: fall back rather than link it.
        run["log_url"] = "gs://kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly/3000000000000000005/"
        self.assertEqual(nightly.build_url(run), f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/3000000000000000005")

    def test_a_record_without_the_field_links_the_legacy_bucket(self):
        self.assertEqual(nightly.build_url({"build_id": NIGHT_1, "job": JOB}), f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_1}")
        self.assertEqual(nightly.build_url({"build_id": NIGHT_1}), f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_1}", "the default job")
        self.assertIsNone(nightly.build_url({"build_id": "bogus", "log_url": self.URL}))

    def test_the_night_report_and_a_running_night_carry_the_recorded_url(self):
        data = two_nights()
        for run in data["runs"]:
            if run["build_id"] == NIGHT_2:
                run["log_url"] = self.URL
        reports = nightly.night_reports(data)
        self.assertEqual([n["log_url"] for n in reports], [self.URL, f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_1}"])
        self.assertEqual(reports[0]["cases"][0]["transcript_url"], self.URL + "/artifacts/eval_case-a_rep1.log")
        data["pending_builds"] = [{"build_id": "3000000000000000005", "first_seen": "2026-09-08T14:05:00+00:00", "tier": "nightly", "log_url": self.URL}]
        now = datetime.datetime.fromisoformat(NOW)
        self.assertEqual(nightly.running_nights(data, now)[0]["log_url"], self.URL)


class DigestLineTest(unittest.TestCase):
    def line(self, data, at=DIGEST_AT):
        return nightly.digest_line(data, at, clock=lambda value: post_health.clock(value, weekday=True))

    def test_the_line_names_the_counts_the_newly_failing_case_and_the_wall_clock(self):
        self.assertEqual(self.line(two_nights()), "🌙 Nightly: 3 cases · 1 passed all reps · 1 partial · 1 failed · newly failing: case-b · 6h 40m")

    def test_a_first_night_a_quiet_night_and_an_infra_loss_read_differently(self):
        data = two_nights()
        data["runs"] = [r for r in data["runs"] if r["build_id"] != NIGHT_1]
        self.assertEqual(self.line(data), "🌙 Nightly: 3 cases · 1 passed all reps · 1 partial · 1 failed · first night on record · 6h 40m")
        data = two_nights()
        data["runs"][-1]["tasks"] = [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "infra", "infra", "infra", reason="429")]
        self.assertEqual(self.line(data), "🌙 Nightly: 3 cases · 2 passed all reps · 0 partial · 0 failed · 1 infra · nothing newly failing · 6h 40m")

    def test_a_truncated_or_incomplete_night_says_so_instead_of_numbers(self):
        data = two_nights()
        data["runs"][-1].update(result="ABORTED", duration_s=None, tasks=data["runs"][-1]["tasks"][:1])
        self.assertEqual(self.line(data), "🌙 Nightly: truncated after 6h 40m · 1 of 3 cases recorded · the night's numbers are not comparable")
        # The deadline's real shape: FAILURE with no verdict line.
        data["runs"][-1].update(result="FAILURE", eval_verdict=None)
        self.assertEqual(self.line(data), "🌙 Nightly: truncated after 6h 40m · 1 of 3 cases recorded · the night's numbers are not comparable")
        data["runs"][-1].update(eval_verdict="RED", duration_s=20000)
        self.assertEqual(self.line(data), "🌙 Nightly: 1 cases · 0 passed all reps · 1 partial · 0 failed · nothing newly failing · incomplete: 1 of 3 cases recorded · 5h 33m")

    def test_a_missing_night_and_no_night_at_all_say_so(self):
        data = two_nights()
        two_days_on = DIGEST_AT + datetime.timedelta(days=2)
        self.assertEqual(self.line(data, two_days_on), "🌙 Nightly: no run last night (the newest on record started Mon 8:00 PM ET)")
        # Unless a night is still running: a late start, or one at its budget.
        data["pending_builds"] = [{"build_id": "3000000000000000003", "first_seen": (two_days_on - datetime.timedelta(hours=8)).isoformat(), "tier": "nightly"}]
        self.assertEqual(self.line(data, two_days_on), "🌙 Nightly: still running (first seen Thu 1:00 AM ET) · the report follows when it finishes")
        self.assertEqual(self.line({"runs": [], "cases": [], "pending_builds": data["pending_builds"]}, two_days_on), "🌙 Nightly: still running (first seen Thu 1:00 AM ET) · the report follows when it finishes")
        # A last night on record outranks a build in flight: the numbers are there.
        self.assertIn("newly failing: case-b", self.line(data))
        data["pending_builds"][0]["first_seen"] = (two_days_on - datetime.timedelta(hours=10)).isoformat()
        self.assertEqual(self.line(data, two_days_on), "🌙 Nightly: no run last night (the newest on record started Mon 8:00 PM ET)", "past the budget: not running")
        self.assertEqual(self.line({"runs": [], "cases": []}), "🌙 Nightly: no run on record yet")
        self.assertEqual(self.line({}), "🌙 Nightly: no data.json to read a night from")
        self.assertEqual(self.line(None), "🌙 Nightly: no data.json to read a night from")

    def test_many_newly_failing_cases_are_counted_past_three(self):
        data = two_nights()
        data["cases"] += [{"name": f"case-{i}", "domain": "cost", "active": True, "nightly_active": True} for i in range(4)]
        data["runs"][-1]["tasks"] += [task(f"case-{i}", "fail", "fail", "fail") for i in range(4)]
        self.assertIn("newly failing: case-0, case-1, case-2 and 2 more", self.line(data))


# A night split across two jobs (nightly.py's module docstring): the main
# part beside the writers part, both started at 00:00 UTC on Tuesday.
WRITERS_JOB = "ci-kube-agents-eval-nightly-writers"
WRITERS_2 = "3000000000000000012"
SPLIT_CASES = [*CASES, {"name": "pr-a", "domain": "gitops", "active": False, "nightly_active": True},
               {"name": "pr-b", "domain": "gitops", "active": False, "nightly_active": True}]
RERUN_MAIN = "3000000000000000004"
WRITERS_URL = f"https://oss.gprow.dev/view/gs/kube-agents-evals-nightly-logs/logs/{WRITERS_JOB}/{WRITERS_2}"


def writers(build, started, finished, tasks, result="SUCCESS", duration_s=7800, **extra):
    return night(build, started, finished, tasks, result=result, duration_s=duration_s, job=WRITERS_JOB,
                 log_url=f"https://oss.gprow.dev/view/gs/kube-agents-evals-nightly-logs/logs/{WRITERS_JOB}/{build}", **extra)


WRITERS_SECOND = writers(WRITERS_2, "2026-09-08T00:00:05+00:00", "2026-09-08T02:10:05+00:00", [
    task("pr-a", "pass", "pass", "pass"), task("pr-b", "fail", "fail", "fail", reason="no pull request opened"),
])


def split_nights():
    """Monday: the main job alone, before the split. Tuesday: both parts."""
    data = two_nights()
    data["cases"] = copy.deepcopy(SPLIT_CASES)
    data["runs"].append(copy.deepcopy(WRITERS_SECOND))
    return data


class SplitNightTest(unittest.TestCase):
    def line(self, data, at=DIGEST_AT):
        return nightly.digest_line(data, at, clock=lambda value: post_health.clock(value, weekday=True))

    def test_one_job_alone_is_one_part_per_night_and_expects_no_other(self):
        for report in nightly.night_reports(two_nights()):
            self.assertEqual([p["part"] for p in report["parts"]], ["main"])
            self.assertEqual((report["missing_parts"], report["running_parts"]), ([], []))
        self.assertEqual(nightly.night_reports(two_nights())[0]["parts"][0], {
            "part": "main", "build": NIGHT_2, "job": JOB, "result": "FAILURE", "truncated": False,
            "started": "2026-09-08T00:00:00+00:00", "finished": "2026-09-08T06:40:00+00:00", "duration_s": 24000,
            "log_url": f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_2}", "recorded": 3,
            "from_night": None,
        })

    def test_two_builds_on_one_date_are_one_night(self):
        nights = nightly.night_reports(split_nights())
        self.assertEqual([n["build"] for n in nights], [NIGHT_2, NIGHT_1], "two nights, filed by the main part's build")
        last = nights[0]
        self.assertEqual([(p["part"], p["build"], p["job"]) for p in last["parts"]], [("main", NIGHT_2, JOB), ("writers", WRITERS_2, WRITERS_JOB)])
        self.assertEqual(last["counts"], {"expected": 5, "recorded": 5, "passed": 2, "partial": 1, "failed": 2, "infra": 0, "missing": 0})
        self.assertTrue(last["complete"])
        self.assertFalse(last["truncated"])
        self.assertEqual((last["missing_parts"], last["running_parts"]), ([], []))
        self.assertEqual(last["newly_failing"], ["case-b", "pr-b"])
        self.assertEqual((last["job"], last["result"], last["project"]), (JOB, "FAILURE", "kube-agents-evals-3"), "the main part's")
        self.assertEqual((last["started"], last["finished"], last["duration_s"]), ("2026-09-08T00:00:00+00:00", "2026-09-08T06:40:00+00:00", 24000))
        by_name = {c["case"]: c for c in last["cases"]}
        self.assertEqual(by_name["pr-b"]["transcript_url"], f"{WRITERS_URL}/artifacts/eval_pr-b_rep1.log", "each case links its own build")
        self.assertEqual([c["case"] for c in last["cases"]], ["case-a", "pr-a", "pr-b", "case-b", "case-c"], "by domain, then name, across the parts")
        # Monday, before any writers build: incomplete against today's
        # matrix, but no part is missing.
        self.assertEqual((nights[1]["missing_parts"], nights[1]["counts"]["missing"]), ([], 2))
        self.assertEqual(self.line(split_nights()), "🌙 Nightly: 5 cases · 2 passed all reps · 1 partial · 2 failed · newly failing: case-b, pr-b · 6h 40m")
        self.assertEqual(nightly.nightly_job(split_nights()), JOB, "the main part's job names the periodic")

    def test_a_writers_build_that_starts_first_opens_the_night_the_main_build_joins(self):
        data = split_nights()
        next(r for r in data["runs"] if r["build_id"] == NIGHT_2)["started"] = "2026-09-08T00:10:00+00:00"
        nights = nightly.night_reports(data)
        parts = [[(p["part"], p["build"]) for p in n["parts"]] for n in nights]
        self.assertEqual(parts, [[("main", NIGHT_2), ("writers", WRITERS_2)], [("main", NIGHT_1)]])
        self.assertEqual((nights[0]["build"], nights[0]["started"]), (NIGHT_2, "2026-09-08T00:00:05+00:00"), "filed by the main part, started with the writers part")
        self.assertEqual((nights[0]["missing_parts"], nights[0]["running_parts"]), ([], []))

    def test_a_night_is_filed_by_its_utc_start_date_with_a_grace(self):
        """8 PM ET is 00:00 UTC: a writers build that starts a second early
        still opens the night the main build a second later joins; one that
        starts at 22:00 UTC belongs to its own date, the night before."""
        data = split_nights()
        next(r for r in data["runs"] if r["build_id"] == WRITERS_2)["started"] = "2026-09-07T23:59:59+00:00"
        next(r for r in data["runs"] if r["build_id"] == NIGHT_2)["started"] = "2026-09-08T00:00:01+00:00"
        nights = nightly.night_reports(data)
        parts = [[(p["part"], p["build"]) for p in n["parts"]] for n in nights]
        self.assertEqual(parts, [[("main", NIGHT_2), ("writers", WRITERS_2)], [("main", NIGHT_1)]])
        self.assertEqual((nights[0]["missing_parts"], nights[1]["missing_parts"]), ([], []))
        self.assertEqual(nightly.night_builds(data), {NIGHT_1: NIGHT_1, WRITERS_2: NIGHT_2, NIGHT_2: NIGHT_2})
        next(r for r in data["runs"] if r["build_id"] == WRITERS_2)["started"] = "2026-09-07T22:00:00+00:00"
        nights = nightly.night_reports(data)
        parts = [[(p["part"], p["build"]) for p in n["parts"]] for n in nights]
        self.assertEqual(parts, [[("main", NIGHT_2)], [("main", NIGHT_1), ("writers", WRITERS_2)]])
        self.assertEqual((nights[0]["missing_parts"], nights[1]["missing_parts"]), (["writers"], []))
        self.assertEqual(nightly.night_builds(data), {NIGHT_1: NIGHT_1, WRITERS_2: NIGHT_1, NIGHT_2: NIGHT_2})

    def test_a_main_rerun_late_in_the_evening_stays_on_its_own_date(self):
        """A manual main re-run at 23:30 UTC is its own date's night; one at
        23:50 UTC is dated to the next day by the grace and is a night of its
        own there. Neither takes the cron night's writers part; each reports
        the writers part of its own date."""
        for rerun_at, lent, lender in (("2026-09-08T23:30:00+00:00", WRITERS_2, NIGHT_2),
                                       ("2026-09-08T23:50:00+00:00", "3000000000000000013", "3000000000000000003")):
            with self.subTest(rerun_at=rerun_at):
                data = split_nights()
                data["runs"].append(night("3000000000000000007", rerun_at, "2026-09-09T06:10:00+00:00",
                                          [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")]))
                data["runs"].append(night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T06:40:00+00:00",
                                          [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")]))
                data["runs"].append(writers("3000000000000000013", "2026-09-09T00:00:05+00:00", "2026-09-09T02:10:05+00:00",
                                            [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")]))
                newest, rerun = nightly.night_reports(data)[:2]
                self.assertEqual([(p["part"], p["build"]) for p in newest["parts"]], [("main", "3000000000000000003"), ("writers", "3000000000000000013")])
                self.assertEqual(newest["missing_parts"], [])
                self.assertEqual([(p["part"], p["build"], p["from_night"]) for p in rerun["parts"]], [("main", "3000000000000000007", None), ("writers", lent, lender)])
                self.assertEqual(nightly.night_builds(data)["3000000000000000013"], "3000000000000000003")

    def test_a_case_both_parts_recorded_the_night_before_reads_as_the_main_part_did(self):
        """Tonight a case both parts recorded counts as the main part
        recorded it; so does the night before, for "newly failing"."""
        data = split_nights()
        data["runs"][-1]["tasks"].append(task("case-b", "pass", "pass", "pass"))
        tuesday = nightly.night_reports(data)[0]
        self.assertEqual({c["case"]: c["state"] for c in tuesday["cases"]}["case-b"], "fail", "the main part's")
        data["runs"].append(night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T06:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "fail", "fail", "fail"), task("case-c", "pass", "pass", "pass")]))
        data["runs"].append(writers("3000000000000000013", "2026-09-09T00:00:05+00:00", "2026-09-09T02:10:05+00:00",
                                    [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")]))
        wednesday = nightly.night_reports(data)[0]
        self.assertEqual(wednesday["newly_failing"], [], "case-b failed in Tuesday's main part")
        self.assertEqual(wednesday["fixed"], ["pr-b"])

    def test_a_night_after_one_without_its_main_part_compares_with_the_last_main_part(self):
        """Monday: case-c fails in the main part. Tuesday: the writers part
        alone. Wednesday: case-c fails again; it is not newly failing,
        because the comparison skips back to Monday's main part."""
        data = two_nights()
        data["cases"] = copy.deepcopy(SPLIT_CASES)
        data["runs"] = [copy.deepcopy(FIRST), copy.deepcopy(WRITERS_SECOND)]
        data["runs"].append(night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T06:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "fail", "fail", "fail"), task("case-c", "fail", "fail", "fail")]))
        data["runs"].append(writers("3000000000000000013", "2026-09-09T00:00:05+00:00", "2026-09-09T02:10:05+00:00",
                                    [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")]))
        wednesday = nightly.night_reports(data)[0]
        self.assertEqual(wednesday["newly_failing"], ["case-b"], "case-c failed in Monday's main part")
        self.assertEqual(wednesday["previous_build"], NIGHT_1)

    def test_each_part_is_compared_with_its_own_night_before(self):
        """Monday: the main job alone. Tuesday: the writers part alone, pr-b
        failing. Wednesday: both parts. The main cases' night before is
        Monday's main part, the writers cases' Tuesday's writers part."""
        data = two_nights()
        data["cases"] = copy.deepcopy(SPLIT_CASES)
        data["runs"] = [copy.deepcopy(FIRST), copy.deepcopy(WRITERS_SECOND)]
        data["runs"].append(night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T06:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "fail", "fail", "fail"), task("case-c", "fail", "fail", "fail")]))
        data["runs"].append(writers("3000000000000000013", "2026-09-09T00:00:05+00:00", "2026-09-09T02:10:05+00:00",
                                    [task("pr-a", "pass", "pass", "pass"), task("pr-b", "fail", "fail", "fail")]))
        wednesday = nightly.night_reports(data)[0]
        self.assertEqual(wednesday["newly_failing"], ["case-b"], "pr-b failed in Tuesday's writers part")
        self.assertEqual(wednesday["previous_build"], NIGHT_1, "the main part's night before")
        data["runs"][-1]["tasks"][1] = task("pr-b", "pass", "pass", "pass")
        self.assertEqual(nightly.night_reports(data)[0]["fixed"], ["pr-b"])
        # Monday's main part recorded pr-b passing, before the split:
        # Tuesday's writers part is the newer record of it, and stands.
        data["runs"][0]["tasks"].append(task("pr-b", "pass", "pass", "pass"))
        data["runs"][-1]["tasks"][1] = task("pr-b", "fail", "fail", "fail")
        self.assertEqual(nightly.night_reports(data)[0]["newly_failing"], ["case-b"])

    def test_a_writers_part_cut_short_leaves_the_main_results_standing(self):
        data = split_nights()
        data["runs"][-1].update(result="FAILURE", eval_verdict=None, tasks=data["runs"][-1]["tasks"][:1])
        last = nightly.night_reports(data)[0]
        self.assertFalse(last["truncated"], "the main part ran to the end")
        self.assertFalse(last["complete"])
        self.assertEqual([p["truncated"] for p in last["parts"]], [False, True])
        self.assertEqual([p["recorded"] for p in last["parts"]], [3, 1])
        self.assertEqual(last["missing"], ["pr-b"])
        self.assertEqual(self.line(data), "🌙 Nightly: 4 cases · 2 passed all reps · 1 partial · 1 failed · newly failing: case-b · writers part truncated after 2h 10m · incomplete: 4 of 5 cases recorded · 6h 40m")
        # The main part cut short too: the whole night is.
        data["runs"][-2].update(result="ABORTED")
        last = nightly.night_reports(data)[0]
        self.assertTrue(last["truncated"])
        self.assertEqual(self.line(data), "🌙 Nightly: truncated after 6h 40m · 4 of 5 cases recorded · the night's numbers are not comparable")

    def test_a_main_part_cut_short_truncates_the_night(self):
        """The main part holds nearly every case: its deadline is the night's,
        whatever the writers part did."""
        data = split_nights()
        data["runs"][-2].update(result="FAILURE", eval_verdict=None)
        last = nightly.night_reports(data)[0]
        self.assertEqual([p["truncated"] for p in last["parts"]], [True, False])
        self.assertTrue(last["truncated"])
        self.assertFalse(last["complete"])
        self.assertEqual(self.line(data), "🌙 Nightly: truncated after 6h 40m · 5 of 5 cases recorded · the night's numbers are not comparable")

    def test_a_truncated_night_gives_its_main_parts_wall_clock(self):
        """The night lasted as long as its longest part, but it was cut
        short when its main part was."""
        data = split_nights()
        data["runs"][-2].update(result="ABORTED", finished="2026-09-08T01:05:00+00:00", duration_s=3900)
        self.assertEqual(nightly.night_reports(data)[0]["duration_s"], 7800, "the writers part's")
        self.assertEqual(self.line(data), "🌙 Nightly: truncated after 1h 05m · 5 of 5 cases recorded · the night's numbers are not comparable")

    def test_a_part_cut_short_with_every_case_recorded_is_incomplete_without_a_count(self):
        data = split_nights()
        data["runs"][-1].update(result="FAILURE", eval_verdict=None)
        last = nightly.night_reports(data)[0]
        self.assertEqual((last["truncated"], last["complete"], last["counts"]["missing"]), (False, False, 0))
        self.assertEqual(self.line(data), "🌙 Nightly: 5 cases · 2 passed all reps · 1 partial · 2 failed · newly failing: case-b, pr-b · writers part truncated after 2h 10m · incomplete · 6h 40m")

    def test_a_missing_writers_part_is_named_once_writers_parts_appear(self):
        data = split_nights()
        wednesday = night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T06:40:00+00:00",
                          [task(name, "pass", "pass", "pass") for name in ("case-a", "case-b", "case-c")])
        data["runs"].append(wednesday)
        last = nightly.night_reports(data)[0]
        self.assertEqual([p["part"] for p in last["parts"]], ["main"])
        self.assertEqual((last["missing_parts"], last["running_parts"]), (["writers"], []))
        self.assertFalse(last["complete"])
        self.assertEqual(self.line(data, DIGEST_AT + datetime.timedelta(days=1)),
                         "🌙 Nightly: 3 cases · 3 passed all reps · 0 partial · 0 failed · nothing newly failing · writers part missing · incomplete: 3 of 5 cases recorded · 6h 40m")
        # Still in flight on that date: running, not missing.
        data["pending_builds"] = [{"build_id": "3000000000000000013", "first_seen": "2026-09-09T00:05:00+00:00", "tier": "nightly",
                                   "log_url": f"https://oss.gprow.dev/view/gs/kube-agents-evals-nightly-logs/logs/{WRITERS_JOB}/3000000000000000013"}]
        last = nightly.night_reports(data, running=nightly.running_nights(data, None))[0]
        self.assertEqual((last["missing_parts"], last["running_parts"]), ([], ["writers"]))
        # In flight from the next date: that is the next night's writers
        # part, and this night's is still missing.
        data["pending_builds"][0]["first_seen"] = "2026-09-10T00:05:00+00:00"
        last = nightly.night_reports(data, running=nightly.running_nights(data, None))[0]
        self.assertEqual((last["missing_parts"], last["running_parts"]), (["writers"], []))
        # First seen a moment before midnight: this night's, by the grace.
        data["pending_builds"][0]["first_seen"] = "2026-09-08T23:58:00+00:00"
        last = nightly.night_reports(data, running=nightly.running_nights(data, None))[0]
        self.assertEqual((last["missing_parts"], last["running_parts"]), ([], ["writers"]))
        # The main job running the whole matrix again (the split undone):
        # nothing is missing, so no part is.
        del data["pending_builds"]
        data["runs"][-1]["tasks"] += [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")]
        last = nightly.night_reports(data)[0]
        self.assertEqual((last["missing_parts"], last["complete"]), ([], True))
        # Short of a main case: incomplete, and no part is named missing.
        full = data["runs"][-1]["tasks"]
        data["runs"][-1]["tasks"] = [t for t in full if t["name"] != "case-c"]
        last = nightly.night_reports(data)[0]
        self.assertEqual((last["missing"], last["missing_parts"], last["complete"]), (["case-c"], [], False))
        self.assertEqual(self.line(data, DIGEST_AT + datetime.timedelta(days=1)),
                         "🌙 Nightly: 4 cases · 4 passed all reps · 0 partial · 0 failed · nothing newly failing · incomplete: 4 of 5 cases recorded · 6h 40m")
        # Short of a case a writers build recorded: the writers part is.
        data["runs"][-1]["tasks"] = [t for t in full if t["name"] != "pr-b"]
        last = nightly.night_reports(data)[0]
        self.assertEqual((last["missing"], last["missing_parts"]), (["pr-b"], ["writers"]))

    def test_a_writers_part_alone_names_the_main_part_missing_or_running(self):
        data = split_nights()
        data["runs"] = [r for r in data["runs"] if r["build_id"] != NIGHT_2]
        last = nightly.night_reports(data)[0]
        self.assertEqual((last["build"], last["job"]), (WRITERS_2, WRITERS_JOB), "filed by the only part it has")
        self.assertEqual((last["missing_parts"], last["running_parts"]), (["main"], []))
        self.assertEqual(last["newly_failing"], ["pr-b"], "in the data; the digest gives no verdict on it")
        self.assertEqual(self.line(data), "🌙 Nightly: 2 cases · 1 passed all reps · 0 partial · 1 failed · main part missing · incomplete: 2 of 5 cases recorded · 2h 10m")
        # Nothing failing in the writers part: still no "nothing newly failing".
        quiet = copy.deepcopy(data)
        quiet["runs"][-1]["tasks"] = [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")]
        self.assertEqual(self.line(quiet), "🌙 Nightly: 2 cases · 2 passed all reps · 0 partial · 0 failed · main part missing · incomplete: 2 of 5 cases recorded · 2h 10m")
        # The main part still in flight: the night's numbers follow with it.
        data["pending_builds"] = [{"build_id": "3000000000000000002", "first_seen": "2026-09-08T00:03:00+00:00", "tier": "nightly"}]
        self.assertEqual(self.line(data, datetime.datetime(2026, 9, 8, 3, 0, tzinfo=UTC)),
                         "🌙 Nightly: still running (first seen Mon 8:03 PM ET) · the report follows when it finishes")
        brief = render.brief_document(dict(data, generated_at="2026-09-08T03:00:00+00:00"), None, None, None, admitted=frozenset(), demoted={})
        self.assertEqual(brief["nightly"]["nights"][0]["running_parts"], ["main"])

    def test_a_rerun_of_the_main_part_on_the_same_date_is_a_night_of_its_own(self):
        """A full passing re-run of the main job at 09:00 is filed as a night
        of its own; its report carries the date's writers part as its own
        (pr-b failing), so it is complete, and says where the part is filed."""
        data = split_nights()
        data["runs"].append(night(RERUN_MAIN, "2026-09-08T09:00:00+00:00", "2026-09-08T15:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")], result="SUCCESS"))
        nights = nightly.group_nights(nightly.sorted_nightly_runs(data))
        self.assertEqual([sorted(n) for n in nights], [["main"], ["main", "writers"], ["main"]])
        self.assertEqual(nightly.night_builds(data), {NIGHT_1: NIGHT_1, NIGHT_2: NIGHT_2, WRITERS_2: NIGHT_2, RERUN_MAIN: RERUN_MAIN})
        last, cron = nightly.night_reports(data)[:2]
        self.assertEqual((last["build"], last["missing_parts"], last["running_parts"]), (RERUN_MAIN, [], []))
        self.assertEqual([(p["part"], p["build"], p["from_night"]) for p in last["parts"]], [("main", RERUN_MAIN, None), ("writers", WRITERS_2, NIGHT_2)])
        self.assertEqual([(p["part"], p["build"], p["from_night"]) for p in cron["parts"]], [("main", NIGHT_2, None), ("writers", WRITERS_2, None)])
        self.assertEqual(last["counts"], {"expected": 5, "recorded": 5, "passed": 4, "partial": 0, "failed": 1, "infra": 0, "missing": 0})
        self.assertEqual((last["missing"], last["complete"], last["truncated"]), ([], True, False))
        self.assertEqual((last["newly_failing"], last["previous_build"]), (["pr-b"], NIGHT_2))
        self.assertEqual((last["started"], last["finished"], last["duration_s"]), ("2026-09-08T09:00:00+00:00", "2026-09-08T15:40:00+00:00", 24000), "its own build's wall clock")
        self.assertEqual(self.line(data, datetime.datetime(2026, 9, 8, 18, 0, tzinfo=UTC)),
                         "🌙 Nightly: 5 cases · 4 passed all reps · 0 partial · 1 failed · newly failing: pr-b · 6h 40m")

    def test_a_borrowed_writers_part_is_compared_with_the_writers_part_before_it(self):
        """Wednesday: both parts, then a main re-run. The re-run's borrowed
        writers part is compared with Tuesday's, not with itself; Thursday's
        writers part is compared with Wednesday's, which it borrowed."""
        data = split_nights()
        data["runs"].append(night("3000000000000000003", "2026-09-09T00:00:00+00:00", "2026-09-09T06:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "fail", "fail", "fail"), task("case-c", "pass", "pass", "pass")]))
        data["runs"].append(writers("3000000000000000013", "2026-09-09T00:00:05+00:00", "2026-09-09T02:10:05+00:00",
                                    [task("pr-a", "fail", "fail", "fail"), task("pr-b", "pass", "pass", "pass")]))
        data["runs"].append(night(RERUN_MAIN, "2026-09-09T09:00:00+00:00", "2026-09-09T15:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "fail", "fail", "fail"), task("case-c", "pass", "pass", "pass")]))
        rerun = nightly.night_reports(data)[0]
        self.assertEqual((rerun["build"], rerun["newly_failing"], rerun["fixed"]), (RERUN_MAIN, ["pr-a"], ["pr-b"]))
        data["runs"].append(night("3000000000000000005", "2026-09-10T00:00:00+00:00", "2026-09-10T06:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")]))
        data["runs"].append(writers("3000000000000000015", "2026-09-10T00:00:05+00:00", "2026-09-10T02:10:05+00:00",
                                    [task("pr-a", "fail", "fail", "fail"), task("pr-b", "pass", "pass", "pass")]))
        thursday = nightly.night_reports(data)[0]
        self.assertEqual((thursday["newly_failing"], thursday["fixed"]), ([], ["case-b"]))

    def test_a_borrowed_writers_build_counts_once_where_runs_are_counted(self):
        """The report borrows the writers part; the Cases page's rates and
        last failure, the run-level event, the classifier and the Trend
        page count its build once, under the night it is filed in."""
        data = split_nights()
        data["runs"].append(night(RERUN_MAIN, "2026-09-08T09:00:00+00:00", "2026-09-08T15:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")], result="SUCCESS"))
        self.assertIn("pr-b", {c["case"] for c in nightly.night_reports(data)[0]["cases"]}, "the re-run's report borrows the writers part")
        self.assertEqual(render.tier_pass_rates(data, "nightly", 30)["pr-b"], (0, 3))
        cases = render.case_documents(data, {}, None, {}, {})
        self.assertEqual(cases["pr-b"]["rates"]["nightly"], [[0, 3], [0, 3]])
        self.assertEqual(cases["pr-b"]["last_failure"]["build"], WRITERS_2)
        nightly_runs = [r for r in data["runs"] if r.get("tier") == "nightly"]
        joined = nightly.joined_night_runs(nightly_runs)
        self.assertEqual([t["name"] for t in joined[id(nightly_runs[-1])]["tasks"]], ["case-a", "case-b", "case-c"], "the re-run is judged on its own build")
        self.assertIs(joined[id(next(r for r in nightly_runs if r["build_id"] == WRITERS_2))], joined[id(next(r for r in nightly_runs if r["build_id"] == NIGHT_2))])
        records = [
            {"case": "case-a", "build": NIGHT_2, "recorded_at": "2026-09-08T06:30:00+00:00", "key": {}},
            {"case": "pr-b", "build": WRITERS_2, "recorded_at": "2026-09-08T02:00:00+00:00", "key": {}},
            {"case": "case-a", "build": RERUN_MAIN, "recorded_at": "2026-09-08T15:30:00+00:00", "key": {}},
        ]
        nights = trend.night_documents(sorted(records, key=lambda r: r["recorded_at"]), data)
        self.assertEqual([(n["build"], n["cases"]) for n in nights], [(NIGHT_2, 2), (RERUN_MAIN, 1)])

    def test_a_rerun_of_the_writers_part_on_the_same_date_replaces_it_in_its_night(self):
        """The writers job is the short one, the one re-run after a flake:
        its newest build that date stands for the night, which stays last
        night with every case."""
        data = split_nights()
        rerun = "3000000000000000014"
        data["runs"].append(writers(rerun, "2026-09-08T05:00:00+00:00", "2026-09-08T07:10:00+00:00", [
            task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass"),
        ]))
        nights = nightly.night_reports(data)
        self.assertEqual([n["build"] for n in nights], [NIGHT_2, NIGHT_1])
        last = nights[0]
        self.assertEqual([(p["part"], p["build"]) for p in last["parts"]], [("main", NIGHT_2), ("writers", rerun)])
        self.assertEqual((last["missing_parts"], last["running_parts"], last["complete"]), ([], [], True))
        self.assertEqual(last["newly_failing"], ["case-b"])
        self.assertEqual(nightly.night_builds(data), {NIGHT_1: NIGHT_1, NIGHT_2: NIGHT_2, WRITERS_2: NIGHT_2, rerun: NIGHT_2}, "the replaced build still files under its night")
        self.assertEqual(self.line(data), "🌙 Nightly: 5 cases · 3 passed all reps · 1 partial · 1 failed · newly failing: case-b · 6h 40m")

    def test_a_writers_rerun_that_lost_every_case_to_infra_leaves_the_writers_part(self):
        """A writers re-run whose every case hit quota recorded as many cases
        but graded none: the part with real verdicts stands."""
        data = split_nights()
        rerun = "3000000000000000014"
        data["runs"].append(writers(rerun, "2026-09-08T05:00:00+00:00", "2026-09-08T05:10:00+00:00",
                                    [task("pr-a", "infra", "infra", "infra"), task("pr-b", "infra", "infra", "infra")]))
        last = nightly.night_reports(data)[0]
        self.assertEqual([(p["part"], p["build"]) for p in last["parts"]], [("main", NIGHT_2), ("writers", WRITERS_2)])
        self.assertEqual(nightly.night_builds(data)[rerun], NIGHT_2)
        self.assertEqual(self.line(data), "🌙 Nightly: 5 cases · 2 passed all reps · 1 partial · 2 failed · newly failing: case-b, pr-b · 6h 40m")

    def test_an_infra_only_writers_rerun_does_not_become_a_main_reruns_writers_part(self):
        """A main re-run at 09:00 leaves a night open for a writers part; a
        writers re-run at 10:00 that lost every case to quota does not take
        it. The re-run night borrows the date's real writers part, and the
        quota re-run stays filed under the cron night."""
        data = split_nights()
        data["runs"].append(night("3000000000000000004", "2026-09-08T09:00:00+00:00", "2026-09-08T15:40:00+00:00",
                                  [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")]))
        rerun = "3000000000000000014"
        data["runs"].append(writers(rerun, "2026-09-08T10:00:00+00:00", "2026-09-08T10:10:00+00:00",
                                    [task("pr-a", "infra", "infra", "infra"), task("pr-b", "infra", "infra", "infra")]))
        last = nightly.night_reports(data)[0]
        self.assertEqual([(p["part"], p["build"]) for p in last["parts"]], [("main", "3000000000000000004"), ("writers", WRITERS_2)])
        self.assertEqual(nightly.night_builds(data)[rerun], NIGHT_2)
        self.assertEqual(self.line(data, at=datetime.datetime(2026, 9, 8, 18, 0, tzinfo=datetime.timezone.utc)),
                         "🌙 Nightly: 5 cases · 4 passed all reps · 0 partial · 1 failed · newly failing: pr-b · 6h 40m")

    def test_a_writers_rerun_cut_short_leaves_a_clean_writers_part(self):
        """A writers re-run aborted after grading as many cases does not
        replace the part that finished; one that finished replaces a part
        cut short with as many."""
        data = split_nights()
        rerun = "3000000000000000014"
        data["runs"].append(writers(rerun, "2026-09-08T05:00:00+00:00", "2026-09-08T06:00:00+00:00",
                                    [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")], result="ABORTED", duration_s=3600))
        last = nightly.night_reports(data)[0]
        self.assertEqual([(p["part"], p["build"], p["truncated"]) for p in last["parts"]], [("main", NIGHT_2, False), ("writers", WRITERS_2, False)])
        self.assertEqual(self.line(data), self.line(split_nights()))
        self.assertNotIn("writers part truncated", self.line(data))
        cut, clean = data["runs"][-2], data["runs"][-1]
        cut.update(result="ABORTED")
        clean.update(result="SUCCESS")
        self.assertEqual([p["build"] for p in nightly.night_reports(data)[0]["parts"]], [NIGHT_2, rerun])

    def test_a_writers_rerun_that_recorded_nothing_leaves_the_writers_part(self):
        """A writers re-run that died in setup recorded no case: it does not
        replace the part that did, and still files under its night."""
        data = split_nights()
        before = nightly.night_reports(data)[0]
        rerun = "3000000000000000014"
        data["runs"].append(writers(rerun, "2026-09-08T05:00:00+00:00", "2026-09-08T05:03:00+00:00", [], result="ABORTED", duration_s=180))
        last = nightly.night_reports(data)[0]
        self.assertEqual([(p["part"], p["build"]) for p in last["parts"]], [("main", NIGHT_2), ("writers", WRITERS_2)])
        self.assertEqual((last["counts"], last["newly_failing"], last["missing_parts"]), (before["counts"], before["newly_failing"], []))
        self.assertEqual(nightly.night_builds(data), {NIGHT_1: NIGHT_1, NIGHT_2: NIGHT_2, WRITERS_2: NIGHT_2, rerun: NIGHT_2})
        self.assertEqual(self.line(data), self.line(split_nights()))

    def test_a_night_is_one_run_level_event_across_its_parts(self):
        """The pages charge a broken run's failures to the run. A writers
        part of two cases both failing is 100% failed on its own; with the
        rest of its night it is not a broken run, so its failures count
        against the cases, as they did before the split."""
        data = split_nights()
        data["runs"][-1]["tasks"] = [task("pr-a", "fail", "fail", "fail"), task("pr-b", "fail", "fail", "fail")]
        rates = render.tier_pass_rates(data, "nightly", 30)
        self.assertEqual(rates["pr-a"], (0, 3))
        self.assertTrue(render.is_run_event({"tasks": data["runs"][-1]["tasks"]}), "alone it would be dropped")
        presubmit = {"build_id": "1", "pr": 5, "started": "2026-09-08T08:00:00+00:00", "finished": "2026-09-08T09:00:00+00:00", "result": "FAILURE", "tasks": []}
        self.assertTrue(render.classify.nightly_failed_recent("pr-a", presubmit, [r for r in data["runs"] if r.get("tier") == "nightly"]))

    def test_the_trend_page_files_a_split_night_once(self):
        data = split_nights()
        records = [
            {"case": "case-a", "build": NIGHT_2, "recorded_at": "2026-09-08T06:30:00+00:00", "key": {}},
            {"case": "pr-a", "build": WRITERS_2, "recorded_at": "2026-09-08T02:00:00+00:00", "key": {}},
        ]
        nights = trend.night_documents(sorted(records, key=lambda r: r["recorded_at"]), data)
        self.assertEqual([(n["id"], n["build"], n["cases"], n["at"]) for n in nights], [(f"build:{NIGHT_2}", NIGHT_2, 2, "2026-09-08T06:30:00+00:00")])
        anchors = nightly.night_builds(data)
        self.assertEqual({p["night"] for p in trend.case_points(records[1:], None, anchors)}, {f"build:{NIGHT_2}"})


class BriefDocumentTest(unittest.TestCase):
    def test_brief_json_carries_the_nightly_block(self):
        brief = render.brief_document(two_nights(), None, None, None, admitted=frozenset(), demoted={})
        self.assertEqual(brief["nightly"]["job"], JOB)
        self.assertEqual([n["build"] for n in brief["nightly"]["nights"]], [NIGHT_2, NIGHT_1])
        self.assertEqual([r["pr"] for r in brief["runs"]], [], "a night is nobody's pull request: not in runs[]")


@unittest.skipUnless(chrome(), "headless Chrome not found")
class NightlyPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = load_fixture()
        data["generated_at"] = NOW
        data["cases"] = copy.deepcopy(CASES)
        data["runs"] += copy.deepcopy([FIRST, SECOND])
        # A third night in flight at render time (NOW is Tue 10:30 AM ET).
        data["pending_builds"] = [{"build_id": "3000000000000000003", "first_seen": "2026-09-08T14:05:00+00:00", "tier": "nightly"}]
        with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=frozenset()), \
                unittest.mock.patch.object(render, "demotion_dates", return_value={}), \
                unittest.mock.patch.object(render, "recent_merges", return_value=None):
            cls.out = render_to(cls.tmp.name, data, health=health_doc("GREEN"))
        cls.page = cls.out / "nightly.html"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_last_nights_report(self):
        app = dom_text(self.page)
        self.assertIn("<h1>Last night's run</h1>", app)
        self.assertIn('<span class="pill p-fail">1 case failed all reps</span>', app)
        self.assertIn("Mon 8:00 PM ET – Tue 2:40 AM ET", app)
        self.assertIn("The job ran to the end in 6h 40m: 3 cases recorded of 3 expected.", app)
        self.assertIn(f'href="https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_2}"', app)
        # Grouped by domain, each case with its state pill, reps and reason.
        self.assertLess(app.index('<tr class="grp"><td colspan="5">cost</td></tr>'), app.index('<tr class="grp"><td colspan="5">reliability</td></tr>'))
        self.assertIn('<tr class="newly"><td class="nm">case-b</td><td><span class="pill p-fail"', app)
        self.assertIn('<span class="pill p-partial" title="failed some reps">partial</span></td><td class="num" title="repetitions passed / graded">2/3</td>', app)
        self.assertIn("check y: required phrases absent", app)
        self.assertIn(f'href="https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/{NIGHT_2}/artifacts/eval_case-b_rep1.log"', app)
        self.assertIn('href="cases.html#case-b"', app)
        # Against the night before, and the other nights list.
        self.assertIn("<b>Newly failing</b> against <a href=\"nightly.html#build=3000000000000000001\">Sun, Sep 6</a>: <code>case-b</code>", app)
        self.assertIn("<b>Passing again:</b> <code>case-c</code>", app)
        self.assertIn('<span class="now">Mon, Sep 7<span class="pill p-fail">1 failed</span></span>', app)
        # The grader's reason reaches the DOM escaped.
        self.assertNotIn("<b>absent</b>", app)
        self.assertIn("check x: &lt;b&gt;absent&lt;/b&gt;", app)
        # The night in flight, on last night's page only.
        running = f'A night is running now: build <a href="https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB}/3000000000000000003">3000000000000000003</a>, first seen Tue 10:05 AM ET.'
        self.assertIn(running, app)
        self.assertNotIn(running, dom_text(self.page, fragment=f"#build={NIGHT_1}"))
        self.assertNotIn('class="c running"', dom_text(self.out / "grid.html"), "a night in flight is no presubmit column")

    def test_an_older_night_and_an_unknown_build(self):
        app = dom_text(self.page, fragment=f"#build={NIGHT_1}")
        self.assertIn("<h1>Night of Sun, Sep 6</h1>", app)
        self.assertIn("First night on record: nothing to compare with yet.", app)
        self.assertIn(f'<a href="nightly.html#build={NIGHT_2}">Mon, Sep 7<span class="pill p-fail">1 failed</span></a>', app)
        app = dom_text(self.page, fragment="#build=4242")
        self.assertIn("<h1>No night with build 4242 on record</h1>", app)

    def test_the_brief_links_last_night(self):
        app = dom_text(self.out / "index.html")
        self.assertIn("<h2>Last night's run</h2>", app)
        self.assertIn("<b>Mon, Sep 7</b> — 3 cases · 1 passed all reps · 1 partial · 1 failed · newly failing: <code>case-b</code> · 6h 40m. "
                      f'<a href="nightly.html#build={NIGHT_2}">Read the report →</a>', app)
        self.assertIn('Last night\'s run: <a href="nightly.html">nightly</a>', app)
        self.assertIn("A night is running now: build <a href=", app)

    def test_a_night_that_graded_nothing_or_less_than_the_matrix_is_not_clean(self):
        """A quota storm grades nothing; a pass short of the matrix is not
        every case passing. Neither earns the green pill or the "clean"
        chip (kube-agents-bot on #1503)."""
        def page_for(newest):
            data = load_fixture()
            data["generated_at"] = NOW
            data["cases"] = copy.deepcopy(CASES)
            data["runs"] += copy.deepcopy([FIRST, newest])
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=frozenset()), \
                    unittest.mock.patch.object(render, "demotion_dates", return_value={}), \
                    unittest.mock.patch.object(render, "recent_merges", return_value=None):
                out = render_to(tmp.name, data, health=health_doc("GREEN"))
            return dom_text(out / "nightly.html"), dom_text(out / "index.html")

        storm = night(NIGHT_2, "2026-09-08T00:00:00+00:00", "2026-09-08T06:40:00+00:00",
                      [task(name, "infra", "infra", "infra", reason="KUBE_AGENTS_INFRA_FAILURE 429") for name in ("case-a", "case-b", "case-c")])
        page, brief_page = page_for(storm)
        self.assertIn('<span class="pill p-infra">nothing graded · 3 cases lost to infra</span>', page)
        self.assertIn('<span class="now">Mon, Sep 7<span class="pill p-infra">nothing graded</span></span>', page)
        self.assertNotIn("every case passed", page)
        self.assertNotIn("every case passed", brief_page)
        self.assertIn("3 cases · 0 passed all reps · 0 partial · 0 failed · 3 infra", brief_page)
        short = night(NIGHT_2, "2026-09-08T00:00:00+00:00", "2026-09-08T06:40:00+00:00",
                      [task("case-a", "pass", "pass", "pass"), task("case-b", "infra", "infra", "infra", reason="429")])
        page, brief_page = page_for(short)
        self.assertIn('<span class="pill p-infra">1 passed · 1 case lost to infra · 1 not recorded</span>', page)
        self.assertIn('<span class="pill p-infra">1 lost · 1 missing</span>', page)
        self.assertNotIn("every case passed", page)
        self.assertNotIn("every case passed", brief_page)
        # Every expected case graded and passed: green, and clean in the list.
        clean = night(NIGHT_2, "2026-09-08T00:00:00+00:00", "2026-09-08T06:40:00+00:00",
                      [task(name, "pass", "pass", "pass") for name in ("case-a", "case-b", "case-c")])
        page, _ = page_for(clean)
        self.assertIn('<span class="pill p-pass">every case passed</span>', page)
        self.assertIn('<span class="now">Mon, Sep 7<span class="pill p-pass">clean</span></span>', page)

    def test_no_night_on_record(self):
        data = load_fixture()
        data["generated_at"] = NOW
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(render, "recent_merges", return_value=None):
            out = render_to(tmp, data, health=health_doc("GREEN"))
            self.assertIn("<h1>No night on record yet</h1>", dom_text(out / "nightly.html"))
            self.assertIn("No night on record yet. Once <code>ci-kube-agents-eval-nightly</code> has run", dom_text(out / "index.html"))
            brief = json.loads((out / "brief.json").read_text())
            self.assertEqual(brief["nightly"]["nights"], [])


@unittest.skipUnless(chrome(), "headless Chrome not found")
class SplitNightPageTest(unittest.TestCase):
    """A night split across two jobs, its writers part cut short: the page
    names both jobs and links both builds, says which part was cut short
    without calling the whole night truncated, and a link to the writers
    build opens the same night."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = load_fixture()
        data["generated_at"] = NOW
        data["cases"] = copy.deepcopy(SPLIT_CASES)
        cut = copy.deepcopy(WRITERS_SECOND)
        cut.update(result="FAILURE", eval_verdict=None, tasks=cut["tasks"][:1])
        data["runs"] += copy.deepcopy([FIRST, SECOND]) + [cut]
        with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=frozenset()), \
                unittest.mock.patch.object(render, "demotion_dates", return_value={}), \
                unittest.mock.patch.object(render, "recent_merges", return_value=None):
            cls.out = render_to(cls.tmp.name, data, health=health_doc("GREEN"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_report_names_both_parts_and_the_part_cut_short(self):
        app = dom_text(self.out / "nightly.html")
        self.assertIn(f"<code>{JOB}</code> and <code>{WRITERS_JOB}</code>", app)
        self.assertIn(f'<a href="{WRITERS_URL}">writers part</a>', app)
        self.assertIn("<b>Incomplete:</b> writers part cut short after 2h 10m. 4 of the 5 cases the nightly matrix on this checkout expects are recorded.", app)
        self.assertNotIn("The night was cut short", app)
        self.assertIn("<h1>Last night's run</h1>", dom_text(self.out / "nightly.html", fragment=f"#build={WRITERS_2}"))
        brief = dom_text(self.out / "index.html")
        self.assertIn("newly failing: <code>case-b</code> · writers part cut short after 2h 10m · 6h 40m", brief)

    def pages_for(self, runs):
        data = load_fixture()
        data["generated_at"] = NOW
        data["cases"] = copy.deepcopy(SPLIT_CASES)
        data["runs"] += copy.deepcopy(runs)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=frozenset()), \
                unittest.mock.patch.object(render, "demotion_dates", return_value={}), \
                unittest.mock.patch.object(render, "recent_merges", return_value=None):
            out = render_to(tmp.name, data, health=health_doc("GREEN"))
        return dom_text(out / "nightly.html"), dom_text(out / "index.html")

    def test_a_night_short_only_of_a_part_is_incomplete_in_the_list(self):
        """Every case passed, the writers part cut short after recording
        both of its cases: the only gap is the part note, and the other
        nights list says incomplete rather than an empty chip."""
        clean = night(NIGHT_2, "2026-09-08T00:00:00+00:00", "2026-09-08T06:40:00+00:00",
                      [task(name, "pass", "pass", "pass") for name in ("case-a", "case-b", "case-c")])
        cut = writers(WRITERS_2, "2026-09-08T00:00:05+00:00", "2026-09-08T02:10:05+00:00",
                      [task("pr-a", "pass", "pass", "pass"), task("pr-b", "pass", "pass", "pass")], result="FAILURE", eval_verdict=None)
        page, _ = self.pages_for([FIRST, clean, cut])
        self.assertIn('<span class="pill p-infra">5 passed · writers part cut short after 2h 10m</span>', page)
        self.assertIn('<span class="now">Mon, Sep 7<span class="pill p-infra">incomplete</span></span>', page)

    def test_a_truncated_night_gives_its_main_parts_wall_clock(self):
        cut = copy.deepcopy(SECOND)
        cut.update(result="ABORTED", finished="2026-09-08T01:05:00+00:00", duration_s=3900)
        page, brief = self.pages_for([FIRST, cut, WRITERS_SECOND])
        self.assertIn("Prow ended the job after 65 min with 5 of 5 cases recorded", page)
        self.assertIn("truncated after 65 min: 5 of 5 cases recorded", brief)

    def test_a_night_without_its_main_part_gives_no_newly_failing_verdict(self):
        page, brief = self.pages_for([FIRST, WRITERS_SECOND])
        self.assertIn("No verdict on what is newly failing: the main part of this night is missing, so only the writers part's cases are on record.", page)
        self.assertNotIn("Newly failing</b>", page)
        self.assertNotIn("Nothing newly failing", page)
        self.assertIn('<td class="nm">pr-b</td>', page)
        self.assertNotIn('class="newly"', page, "no row badged newly failing either")
        self.assertIn("no main part on record", page)
        self.assertIn('<div class="k">Newly failing</div><div class="v">—</div>', page)
        self.assertIn("2 cases · 1 passed all reps · 0 partial · 1 failed · main part missing · 2h 10m", brief)


if __name__ == "__main__":
    unittest.main()
