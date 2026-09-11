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

from eval_dashboard import nightly, post_health, render
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
        # Concluded but short of the matrix: incomplete, not truncated.
        data["runs"][-1].update(result="FAILURE", duration_s=20000)
        last = nightly.night_reports(data)[0]
        self.assertFalse(last["truncated"])
        self.assertFalse(last["complete"])

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
        self.assertEqual(nightly.nightly_document({"runs": [], "cases": []}), {"job": JOB, "nights": []})


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
        data["runs"][-1].update(result="FAILURE", duration_s=20000)
        self.assertEqual(self.line(data), "🌙 Nightly: 1 cases · 0 passed all reps · 1 partial · 0 failed · nothing newly failing · incomplete: 1 of 3 cases recorded · 5h 33m")

    def test_a_missing_night_and_no_night_at_all_say_so(self):
        data = two_nights()
        two_days_on = DIGEST_AT + datetime.timedelta(days=2)
        self.assertEqual(self.line(data, two_days_on), "🌙 Nightly: no run last night (the newest on record started Mon 8:00 PM ET)")
        self.assertEqual(self.line({"runs": [], "cases": []}), "🌙 Nightly: no run on record yet")
        self.assertEqual(self.line({}), "🌙 Nightly: no data.json to read a night from")
        self.assertEqual(self.line(None), "🌙 Nightly: no data.json to read a night from")

    def test_many_newly_failing_cases_are_counted_past_three(self):
        data = two_nights()
        data["cases"] += [{"name": f"case-{i}", "domain": "cost", "active": True, "nightly_active": True} for i in range(4)]
        data["runs"][-1]["tasks"] += [task(f"case-{i}", "fail", "fail", "fail") for i in range(4)]
        self.assertIn("newly failing: case-0, case-1, case-2 and 2 more", self.line(data))


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
        self.assertIn("<b>Newly failing</b> against <a href=\"nightly.html?build=3000000000000000001\">Sun, Sep 6</a>: <code>case-b</code>", app)
        self.assertIn("<b>Passing again:</b> <code>case-c</code>", app)
        self.assertIn('<span class="now">Mon, Sep 7<span class="pill p-fail">1 failed</span></span>', app)
        # The grader's reason reaches the DOM escaped.
        self.assertNotIn("<b>absent</b>", app)
        self.assertIn("check x: &lt;b&gt;absent&lt;/b&gt;", app)

    def test_an_older_night_and_an_unknown_build(self):
        app = dom_text(self.page, query=f"build={NIGHT_1}")
        self.assertIn("<h1>Night of Sun, Sep 6</h1>", app)
        self.assertIn("First night on record: nothing to compare with yet.", app)
        self.assertIn(f'<a href="nightly.html?build={NIGHT_2}">Mon, Sep 7<span class="pill p-fail">1 failed</span></a>', app)
        app = dom_text(self.page, query="build=4242")
        self.assertIn("<h1>No night with build 4242 on record</h1>", app)

    def test_the_brief_links_last_night(self):
        app = dom_text(self.out / "index.html")
        self.assertIn("<h2>Last night's run</h2>", app)
        self.assertIn("<b>Mon, Sep 7</b> — 3 cases · 1 passed all reps · 1 partial · 1 failed · newly failing: <code>case-b</code> · 6h 40m. "
                      f'<a href="nightly.html?build={NIGHT_2}">Read the report →</a>', app)
        self.assertIn('Last night\'s run: <a href="nightly.html">nightly</a>', app)

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


if __name__ == "__main__":
    unittest.main()
