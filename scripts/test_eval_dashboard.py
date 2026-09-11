"""Contract tests for the eval dashboard renderer's brief.json and the publisher.

The fixtures here are built against schema_version 1 of the collector's
data.json -- including the optional additive ``tasks[].reps``,
``runs[].pr_merged``, ``runs[].tier``, ``pending_builds`` and ``releases[]``
fields (SCHEMA.md) -- deliberately in this file rather than shared with the
collector: the renderer must keep working from the written contract alone,
so these tests are the contract's teeth on the reading side. Both directions
are covered: reps present (rep-level rates, strips, last failures) and reps
absent (single-result tasks), which must fall back to each task's single
result.

What the pages *show* from brief.json is `test_eval_dashboard_pages.py`'s
job, in headless Chrome; this file pins what render.py *computes*.

The publish tests never touch a bucket. The gsutil argv is asserted as a
value (``gsutil_command``), and ``publish`` is only ever *executed* against a
local directory -- a gs:// target in these tests gets a recording fake for a
runner, and the local-path test uses a runner that fails the test if called.
"""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import unittest.mock

from eval_dashboard import publish, render

HERE = pathlib.Path(__file__).resolve().parent
REPO_NOTES = HERE / "eval_dashboard" / "case-notes.yaml"
REPO_EVENTS = HERE / "eval_dashboard" / "events.yaml"
REPO_ROSTER_DOC = HERE.parent / "docs" / "eval-gate-roster.md"
TEMPLATES = [HERE / "eval_dashboard" / "template" / "page.html.tmpl", HERE / "eval_dashboard" / "template" / "pages.js"]

# The fixture's roster: case-b is active but off it (and dated as demoted),
# so the status pills have every state to show.
ADMITTED = frozenset({"case-a", "case-c", "case-d", "case-e"})
DEMOTED = {"case-b": "2026-08-30"}

# A reason long enough to exercise truncation on the page side; carries an
# agent-classed keyword ("false finding").
LONG_REASON = (
    "false finding on a healthy workload: the agent invented a PDB violation "
    "that does not exist"
)

# The real post-kube-agents-eval-rc build the collector fixture is built from
# (scripts/eval_dashboard/testdata_rc/), reduced to the releases[] record.
RC_TASKS = [{"name": f"case-{n}", "result": "pass" if n < 15 else "fail"} for n in range(25)]
RC_TASKS.append({"name": "case-infra", "result": "infra"})
RC_RELEASE = {
    "build_id": "2097891568546484224",
    "rc_tag": "staging_2609092307_5b5ad10",
    "commit": "5b5ad10",
    "tier": "nightly",
    "verdict": "GREEN",
    "result": "SUCCESS",
    "started": "2026-09-10T03:35:01+00:00",
    "finished": "2026-09-10T07:51:10+00:00",
    "duration_s": 15006,
    "project": "kube-agents-evals-10",
    "artifacts_url": "https://oss.gprow.dev/view/gs/kube-agents-prow/logs/"
                     "post-kube-agents-eval-rc/2097891568546484224",
    "pass_rate": 0.9,
    "baseline_rate": None,
    "margin": None,
    "tasks": RC_TASKS,
}


def rep(result, reason=None):
    return {"n": 1, "result": result, "reason": reason}


def fixture_data():
    """Six runs against five active cases, telling the whole story:

    - run A (#900, merged, 08-20): twelve days old -- inside the 14-day
      brief window and the 30-day rate window, outside the 7-day one;
      4 pass / 1 fail.
    - run B (#950, 08-30): superseded by run C of the same PR.
    - run C (#950, 08-31): 6 pass / 1 fail, with a partial cell and an
      all-infra cell.
    - run D (#951, 08-31): run-level event (4 of 5 graded tasks failed);
      it keeps its strip bar and its grid cell but its failures do not
      count in the rates.
    - run E (#952, 09-01): 429 reps, not-a-real-run reps, an exact-check
      miss, a reason-less fail, and a long agent-classed reason.
    - run F (#953, 09-01, SUCCESS): the latest green full run.
    """
    return {
        "schema_version": 1,
        "generated_at": "2026-09-01T12:00:00Z",
        "source": "logs",
        "runs": [
            {
                "build_id": "bA", "pr": 900, "pr_merged": True,
                "started": "2026-08-20T10:00:00Z", "finished": "2026-08-20T11:30:00Z",
                "result": "FAILURE", "duration_s": 5400,
                "tasks": [
                    {"name": "case-a", "result": "pass",
                     "reps": [rep("pass"), rep("pass"), rep("pass")]},
                    {"name": "case-b", "result": "pass",
                     "reps": [rep("pass"), rep("fail", "old flake before fix"), rep("infra")]},
                ],
            },
            {
                "build_id": "bB", "pr": 950, "pr_merged": True,
                "started": "2026-08-30T09:00:00Z", "finished": "2026-08-30T10:40:00Z",
                "result": "FAILURE", "duration_s": 6000,
                "tasks": [
                    {"name": "case-a", "result": "pass",
                     "reps": [rep("pass"), rep("pass"), rep("pass")]},
                    {"name": "case-b", "result": "pass",
                     "reps": [rep("pass"), rep("pass"),
                              rep("fail", "check kanban-columns: required phrases absent")]},
                ],
            },
            {
                "build_id": "bC", "pr": 950, "pr_merged": True,
                "started": "2026-08-31T09:00:00Z", "finished": "2026-08-31T10:40:00Z",
                "result": "FAILURE", "duration_s": 6000,
                "tasks": [
                    {"name": "case-a", "result": "pass",
                     "reps": [rep("pass"), rep("pass"), rep("pass")]},
                    {"name": "case-b", "result": "pass",
                     "reps": [rep("fail", "check kanban-columns: required phrases absent"),
                              rep("pass"), rep("pass")]},
                    {"name": "case-c", "result": "pass"},
                    {"name": "case-d", "result": "infra",
                     "reps": [rep("infra"), rep("infra"), rep("infra")]},
                ],
            },
            {
                "build_id": "bD", "pr": 951, "pr_merged": False,
                "started": "2026-08-31T11:00:00Z", "finished": "2026-08-31T12:10:00Z",
                "result": "FAILURE", "duration_s": 4200,
                "tasks": [
                    {"name": "case-a", "result": "fail",
                     "reps": [rep("fail", "EVENT-ONLY-REASON breakage"),
                              rep("fail", "EVENT-ONLY-REASON breakage"),
                              rep("fail", "EVENT-ONLY-REASON breakage")]},
                    {"name": "case-b", "result": "fail"},
                    {"name": "case-c", "result": "fail"},
                    {"name": "case-d", "result": "fail"},
                    {"name": "case-e", "result": "pass"},
                ],
            },
            {
                "build_id": "bE", "pr": 952,
                "started": "2026-09-01T08:00:00Z", "finished": "2026-09-01T09:30:00Z",
                "result": "FAILURE", "duration_s": 5100,
                "tasks": [
                    {"name": "case-a", "result": "pass",
                     "reps": [rep("pass"), rep("pass"),
                              rep("fail", "HTTP 429 Too Many Requests from litellm endpoint")]},
                    {"name": "case-b", "result": "fail",
                     "reps": [rep("fail", "transcript is not evidence of a real agent run"),
                              rep("fail", "transcript is not evidence of a real agent run"),
                              rep("fail", "check kanban-columns: required phrases absent")]},
                    {"name": "case-c", "result": "infra",
                     "reps": [rep("infra", "HTTP 429 Too Many Requests")]},
                    {"name": "case-d", "result": "fail"},
                    {"name": "case-e", "result": "fail", "reps": [rep("fail", LONG_REASON)]},
                ],
            },
            {
                "build_id": "bF", "pr": 953, "pr_merged": True, "head_sha": "f6e5d4c00",
                "started": "2026-09-01T09:00:00Z", "finished": "2026-09-01T11:30:00Z",
                "result": "SUCCESS", "duration_s": 9000,
                "tasks": [
                    {"name": "case-a", "result": "pass",
                     "reps": [rep("pass"), rep("pass"), rep("pass")]},
                    {"name": "case-b", "result": "pass",
                     "reps": [rep("pass"), rep("pass"), rep("pass")]},
                    {"name": "case-c", "result": "pass"},
                ],
            },
        ],
        "cases": [
            {"name": "case-a", "domain": "reliability", "active": True, "runs_on_record": 6},
            {"name": "case-b", "domain": "chat-and-routing", "active": True, "runs_on_record": 6},
            {"name": "case-c", "domain": "capacity", "active": True, "runs_on_record": 4},
            {"name": "case-d", "domain": "security", "active": True, "runs_on_record": 3},
            {"name": "case-e", "domain": "gpu", "active": True, "runs_on_record": 2},
            {"name": "case-f", "domain": "cost", "active": False, "runs_on_record": 0},
        ],
        "coverage": {"domains_total": 11, "domains_covered": 11, "uncovered": []},
    }


def nightly_run():
    """A nightly build the day before generated_at: red on case-a and on a
    nightly-only case-g, green on case-b. Nobody's pull request."""
    return {
        "build_id": "bN", "tier": "nightly", "job": "ci-kube-agents-eval-nightly", "pr": None,
        "head_sha": "7a32267", "started": "2026-08-31T20:00:00Z", "finished": "2026-09-01T00:30:00Z",
        "result": "FAILURE", "duration_s": 16200,
        "tasks": [
            {"name": "case-a", "result": "fail",
             "reps": [rep("fail", "NIGHTLY-ONLY-REASON drift"), rep("fail", "NIGHTLY-ONLY-REASON drift"), rep("fail", "NIGHTLY-ONLY-REASON drift")]},
            {"name": "case-b", "result": "pass", "reps": [rep("pass"), rep("pass"), rep("pass")]},
            {"name": "case-g", "result": "fail", "reps": [rep("pass"), rep("fail", "NIGHTLY-ONLY-REASON audit"), rep("fail", "NIGHTLY-ONLY-REASON audit")]},
        ],
    }


def with_nightly(data=None):
    data = data or fixture_data()
    data["runs"].append(nightly_run())
    data["cases"].append({"name": "case-g", "domain": "obtainability", "active": False, "nightly_active": True,
                          "runs_on_record": 0, "nightly": {"runs_on_record": 1, "pass_rate": 0.0, "last3": ["fail"]}})
    data["cases"][0]["nightly"] = {"runs_on_record": 1, "pass_rate": 0.0, "last3": ["fail"]}
    return data


def fixture_events_yaml():
    return (
        "catches:\n"
        "  product_bugs: 4\n"
        "  prs_blocked: 2\n"
        '  ledger: "#1054"\n'
    )


def render_fixture(data, notes_path=None, events_path=None, events_yaml=None, admitted=ADMITTED, demoted=DEMOTED):
    """Run the real CLI against a temp dir with the fixture's roster;
    returns (out_dir, brief, tmp). Notes and events default to *absent*
    files so the repo's own annotation files never leak into a test; pass
    events_yaml to write one inline."""
    tmp = tempfile.TemporaryDirectory()
    out_dir = pathlib.Path(tmp.name) / "out"
    data_path = pathlib.Path(tmp.name) / "data.json"
    data_path.write_text(json.dumps(data))
    if events_yaml is not None:
        events_path = pathlib.Path(tmp.name) / "events.yaml"
        events_path.write_text(events_yaml)
    argv = ["--data", str(data_path), "--out-dir", str(out_dir), "--repo-root", tmp.name]
    argv += ["--notes", str(notes_path or pathlib.Path(tmp.name) / "no-notes.yaml")]
    argv += ["--events", str(events_path or pathlib.Path(tmp.name) / "no-events.yaml")]
    with contextlib.redirect_stdout(io.StringIO()), \
            unittest.mock.patch.object(render.classify, "admitted_cases", return_value=admitted), \
            unittest.mock.patch.object(render, "demotion_dates", return_value=demoted):
        render.main(argv)
    brief = json.loads((out_dir / render.BRIEF_JSON).read_text())
    return out_dir, brief, tmp


class BriefCasesTest(unittest.TestCase):
    """brief.json's per-case record: what the Cases page and the Grid read."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir, cls.brief, cls._tmp = render_fixture(fixture_data(), events_yaml=fixture_events_yaml())
        cls.cases = cls.brief["cases"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_rates_per_tier_at_7_and_30_days(self):
        # Windows end at generated_at 09-01T12:00; run D is a run-level
        # event and never counts. case-a, 7d: B 3/3, C 3/3, E 2/3, F 3/3 ->
        # 11 of 12; 30d adds run A's 3/3 -> 14 of 15. No nightly on record.
        self.assertEqual(self.brief["rate_windows_days"], [7, 30])
        self.assertEqual(self.cases["case-a"]["rates"], {"presubmit": [[11, 1], [14, 1]], "nightly": [None, None]})
        # case-d: run C all infra (uncounted), run D an event, run E one bare fail.
        self.assertEqual(self.cases["case-d"]["rates"]["presubmit"], [[0, 1], [0, 1]])
        self.assertEqual(self.cases["case-f"]["rates"]["presubmit"], [None, None], "never ran: no rate, not zero")

    def test_status_pills_from_the_roster_and_the_roster_page(self):
        self.assertEqual((self.cases["case-a"]["status"], self.cases["case-a"]["demoted_on"]), ("blocking", None))
        self.assertEqual((self.cases["case-b"]["status"], self.cases["case-b"]["demoted_on"]), ("demoted", "2026-08-30"))
        self.assertEqual(self.cases["case-f"]["status"], "retired")
        self.assertTrue(self.cases["case-a"]["admitted"])
        self.assertFalse(self.cases["case-b"]["admitted"])
        self.assertEqual(self.cases["case-a"]["domain"], "reliability")

    def test_an_active_case_off_the_roster_without_a_date_is_held_out(self):
        _, brief, tmp = render_fixture(fixture_data(), demoted={})
        self.addCleanup(tmp.cleanup)
        self.assertEqual(brief["cases"]["case-b"]["status"], "held_out")

    def test_an_unreadable_roster_reads_every_active_case_as_blocking(self):
        _, brief, tmp = render_fixture(fixture_data(), admitted=None)
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(brief["admitted"])
        self.assertEqual({c["status"] for n, c in brief["cases"].items() if n != "case-f"}, {"blocking"})

    def test_the_strip_is_the_case_history_oldest_first(self):
        strip = self.cases["case-a"]["strip"]
        self.assertEqual([s["build"] for s in strip], ["bA", "bB", "bC", "bD", "bE", "bF"])
        self.assertEqual([s["state"] for s in strip], ["pass", "pass", "pass", "fail", "partial", "pass"])
        self.assertEqual([s["event"] for s in strip], [False, False, False, True, False, False], "run D is a run-level event")
        self.assertEqual(strip[0]["pr"], 900)
        self.assertEqual(strip[0]["at"], "2026-08-20T11:30:00Z", "placed by the run's finish")
        # case-c: absent from A and B, infra in E -- infra is history, so it stays on the strip.
        self.assertEqual([s["state"] for s in self.cases["case-c"]["strip"]], ["pass", "fail", "infra", "pass"])
        self.assertEqual(self.cases["case-f"]["strip"], [])

    def test_the_strip_is_capped_at_the_last_strip_runs(self):
        data = fixture_data()
        for n in range(40):
            data["runs"].append({"build_id": f"bX{n:02d}", "pr": 1000 + n, "started": f"2026-09-01T11:{n:02d}:30Z",
                                 "result": "SUCCESS", "tasks": [{"name": "case-c", "result": "pass"}]})
        _, brief, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        strip = brief["cases"]["case-c"]["strip"]
        self.assertEqual(len(strip), render.STRIP_RUNS)
        self.assertEqual(brief["strip_runs"], render.STRIP_RUNS)
        self.assertNotIn("bC", [s["build"] for s in strip], "the oldest appearances fall off")
        self.assertEqual(strip[-1]["build"], "bX39", "newest last")

    def test_the_last_failure_is_the_newest_non_pass_and_carries_the_reason(self):
        failure = self.cases["case-b"]["last_failure"]
        self.assertEqual((failure["tier"], failure["build"], failure["pr"], failure["state"]), ("presubmit", "bE", 952, "fail"))
        self.assertEqual(failure["reps"], {"pass": 0, "fail": 3, "infra": 0}, "the collector's rep results, as the strip counts them")
        self.assertEqual(failure["reason"], "check kanban-columns: required phrases absent", "the first graded failure's reason, not a never-ran phrasing")
        self.assertIn("cls", failure)
        self.assertIn("also_failing_prs", failure)
        self.assertIsNone(failure["excerpt"], "data.json carries no excerpt; nothing is invented")
        # case-a's newest non-pass is the partial in run E, not the older full failure in D.
        partial = self.cases["case-a"]["last_failure"]
        self.assertEqual((partial["build"], partial["state"]), ("bE", "partial"))
        self.assertEqual(partial["reps"], {"pass": 2, "fail": 1, "infra": 0})
        self.assertIsNone(self.cases["case-f"]["last_failure"])

    def test_notes_and_issues_travel_and_a_badge_key_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = pathlib.Path(tmp) / "notes.yaml"
            notes_path.write_text('notes:\n  case-a:\n    note: hardened 08-27\n    issues: ["#1010"]\n    badge: held-out\n')
            _, brief, tmp_render = render_fixture(fixture_data(), notes_path=notes_path)
            self.addCleanup(tmp_render.cleanup)
        self.assertEqual((brief["cases"]["case-a"]["note"], brief["cases"]["case-a"]["issues"]), ("hardened 08-27", ["#1010"]))
        self.assertEqual((brief["cases"]["case-b"]["note"], brief["cases"]["case-b"]["issues"]), (None, []))
        self.assertNotIn("badge", brief["cases"]["case-a"])

    def test_catches_come_from_events_yaml(self):
        self.assertEqual(self.brief["catches"], {"product_bugs": 4, "prs_blocked": 2, "ledger": "#1054"})
        _, brief, tmp = render_fixture(fixture_data())
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(brief["catches"])

    def test_brief_runs_are_the_last_14_days_and_data_json_is_copied(self):
        self.assertEqual([r["build"] for r in self.brief["runs"]], ["bA", "bB", "bC", "bD", "bE", "bF"], "run A is 12 days old: inside the window")
        old = dict(fixture_data(), generated_at="2026-09-05T12:00:00Z")
        _, brief, tmp = render_fixture(old)
        self.addCleanup(tmp.cleanup)
        self.assertEqual([r["build"] for r in brief["runs"]], ["bB", "bC", "bD", "bE", "bF"], "four days later run A has aged out")
        copied = json.loads((self.out_dir / "data.json").read_text())
        self.assertEqual(copied["generated_at"], "2026-09-01T12:00:00Z")
        self.assertEqual(self.brief["pending"], [])
        self.assertEqual(self.brief["releases"], [])


class NightlyTierTest(unittest.TestCase):
    """A nightly run in data.json (runs[].tier) shows up where the nightly is
    meant to -- the nightly rate columns, a nightly-only case's status and
    last failure -- and nowhere the gate is judged: not in the Brief's runs,
    not in a presubmit strip, not in a presubmit rate."""

    @classmethod
    def setUpClass(cls):
        _, cls.brief, cls._tmp = render_fixture(with_nightly())
        _, cls.control, cls._tmp2 = render_fixture(fixture_data())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        cls._tmp2.cleanup()

    def test_the_nightly_record_sits_beside_the_presubmits_never_pooled(self):
        case_a = self.brief["cases"]["case-a"]
        self.assertEqual(case_a["rates"]["presubmit"], self.control["cases"]["case-a"]["rates"]["presubmit"])
        self.assertEqual(case_a["rates"]["nightly"], [[0, 3], [0, 3]])
        self.assertEqual([s["build"] for s in case_a["strip"]], [s["build"] for s in self.control["cases"]["case-a"]["strip"]], "the strip is the presubmit's")
        self.assertEqual(case_a["last_failure"]["tier"], "presubmit", "the presubmit's failure wins while it has one")

    def test_a_nightly_only_case_has_its_status_rates_and_failure_from_the_nightly(self):
        case_g = self.brief["cases"]["case-g"]
        self.assertEqual(case_g["status"], "nightly_only")
        self.assertEqual(case_g["rates"], {"presubmit": [None, None], "nightly": [[1, 2], [1, 2]]})
        self.assertEqual(case_g["strip"], [])
        failure = case_g["last_failure"]
        self.assertEqual((failure["tier"], failure["build"], failure["pr"], failure["state"]), ("nightly", "bN", None, "partial"))
        self.assertEqual(failure["reason"], "NIGHTLY-ONLY-REASON audit")
        self.assertIsNone(failure["cls"], "a nightly run is never classified as anyone's PR")

    def test_brief_runs_list_presubmit_runs_only(self):
        self.assertNotIn("bN", [r["build"] for r in self.brief["runs"]])
        self.assertEqual(len(self.brief["runs"]), 6)
        self.assertTrue(self.brief["cases"]["case-g"]["nightly_active"])
        self.assertFalse(self.brief["cases"]["case-a"]["nightly_active"], "absent in the fixture reads false")
        # The nightly still informs each case's note on the PR view.
        run_e = next(r for r in self.brief["runs"] if r["build"] == "bE")
        case_a = next(c for c in run_e["cases"] if c["case"] == "case-a")
        self.assertIs(case_a["nightly_failed_recent"], True)

    def test_an_unknown_tier_is_neither_the_gates_nor_the_nightlys(self):
        data = fixture_data()
        data["runs"][-1]["tier"] = "rc"  # run F
        _, brief, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("bF", [r["build"] for r in brief["runs"]])
        self.assertNotIn("bF", [s["build"] for s in brief["cases"]["case-a"]["strip"]])
        self.assertEqual(brief["cases"]["case-a"]["rates"], {"presubmit": [[8, 1], [11, 1]], "nightly": [None, None]})


class RosterPageTest(unittest.TestCase):
    def test_demotion_dates_are_read_from_the_hold_out_entries(self):
        text = (
            "## The admission bar\n\n"
            "- **case-x** —\n"
            "  [#1](https://example/1): demoted 2026-09-02 after collapses on unrelated\n"
            "  pull requests. Enters when the bar holds.\n"
            "- **case-y** —\n"
            "  [#2](https://example/2): the receipt is graded as the answer. Never\n"
            "  admitted, so never demoted.\n"
            "- **case-z** — demoted 2026-09-02 evening after six collapses.\n\n"
            "Prose after the list that says demoted 2026-01-01 belongs to no entry.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "roster.md"
            path.write_text(text)
            self.assertEqual(render.demotion_dates(path), {"case-x": "2026-09-02", "case-z": "2026-09-02"})
            self.assertEqual(render.demotion_dates(pathlib.Path(tmp) / "missing.md"), {})

    def test_the_repos_roster_page_dates_the_two_demotions_on_record(self):
        dates = render.demotion_dates(REPO_ROSTER_DOC)
        self.assertEqual(dates.get("compliance-rbac-overgrant"), "2026-09-02")
        self.assertEqual(dates.get("rca-remediation-pr"), "2026-09-02")

    def test_case_status_rules(self):
        status = render.case_status
        self.assertEqual(status({"name": "a", "active": True}, frozenset({"a"}), {}), ("blocking", None))
        self.assertEqual(status({"name": "a", "active": True}, frozenset(), {"a": "2026-09-02"}), ("demoted", "2026-09-02"))
        self.assertEqual(status({"name": "a", "active": True}, frozenset(), {}), ("held_out", None))
        self.assertEqual(status({"name": "a", "active": False, "nightly_active": True}, frozenset({"a"}), {}), ("nightly_only", None))
        self.assertEqual(status({"name": "a"}, frozenset({"a"}), {}), ("retired", None))
        self.assertEqual(status({"name": "a", "active": True}, None, {}), ("blocking", None), "no roster reads as blocking")


class ReleasesAndPendingTest(unittest.TestCase):
    """releases[] and pending_builds are optional and additive, so every
    state a real data.json can reach has to travel -- absent, populated,
    and the half-parsed record a driver that died before its banner leaves."""

    def brief_with(self, **extra):
        data = fixture_data()
        data.update(extra)
        _, brief, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        return brief

    def test_a_real_release_is_compacted_and_its_cases_counted(self):
        release = self.brief_with(releases=[RC_RELEASE])["releases"][0]
        self.assertEqual(release["build"], "2097891568546484224")
        self.assertEqual(release["rc_tag"], "staging_2609092307_5b5ad10")
        self.assertEqual(release["artifacts_url"], RC_RELEASE["artifacts_url"])
        self.assertEqual((release["tier"], release["verdict"], release["result"]), ("nightly", "GREEN", "SUCCESS"))
        self.assertEqual(release["pass_rate"], 0.9)
        self.assertIsNone(release["baseline_rate"])
        self.assertEqual(release["cases"], {"passed": 15, "graded": 25, "infra": 1})
        self.assertEqual(release["duration_s"], 15006)
        self.assertNotIn("tasks", release)

    def test_a_non_https_artifacts_url_never_reaches_the_page(self):
        release = self.brief_with(releases=[dict(RC_RELEASE, artifacts_url="javascript:alert(1)")])["releases"][0]
        self.assertIsNone(release["artifacts_url"])

    def test_a_run_with_no_banner_keeps_its_nulls(self):
        blank = {"build_id": "42", "rc_tag": None, "commit": None, "tier": None, "verdict": None, "result": "FAILURE",
                 "started": "2026-09-10T03:35:01+00:00", "finished": None, "duration_s": None, "project": None,
                 "artifacts_url": None, "pass_rate": None, "baseline_rate": None, "margin": None, "tasks": []}
        release = self.brief_with(releases=[blank])["releases"][0]
        self.assertEqual((release["build"], release["verdict"], release["result"], release["cases"]), ("42", None, "FAILURE", None))

    def test_the_list_is_newest_first_and_capped(self):
        many = [dict(RC_RELEASE, build_id=str(2000 + n), started=f"2026-09-{n + 1:02d}T03:35:01+00:00")
                for n in range(render.RELEASES_MAX_ROWS + 4)]
        builds = [r["build"] for r in self.brief_with(releases=many)["releases"]]
        self.assertEqual(len(builds), render.RELEASES_MAX_ROWS)
        self.assertEqual(builds[0], many[-1]["build_id"])
        self.assertNotIn(many[0]["build_id"], builds)

    def test_malformed_or_retyped_releases_degrade(self):
        self.assertEqual([r["build"] for r in self.brief_with(releases=["nonsense", 7, None, {"build_id": "9"}])["releases"]], ["9"])
        self.assertEqual(self.brief_with(releases="soon")["releases"], [])

    def test_pending_builds_become_the_grids_running_columns(self):
        # generated_at is 09-01T12:00; a build first seen the day before is
        # a pod that died without uploading, not a run still in flight.
        brief = self.brief_with(pending_builds=[
            {"build_id": "2098409186789429248", "first_seen": "2026-09-01T11:58:44+00:00"},
            {"build_id": "2098076561386246144", "first_seen": "2026-09-01T10:12:39+00:00"},
            {"build_id": "2097000000000000000", "first_seen": "2026-08-31T12:00:00+00:00"},
            {"build_id": "not-a-build", "first_seen": "2026-09-01T10:12:39+00:00"},
            {"build_id": "2098000000000000000", "first_seen": "yesterday"},
            "junk",
        ])
        self.assertEqual(brief["pending"], [
            {"build": "2098076561386246144", "first_seen": "2026-09-01T10:12:39+00:00"},
            {"build": "2098409186789429248", "first_seen": "2026-09-01T11:58:44+00:00"},
        ])
        self.assertEqual(self.brief_with(pending_builds="soon")["pending"], [])


class RenderedPagesTest(unittest.TestCase):
    def test_four_pages_are_written_and_none_names_the_legacy_page(self):
        out_dir, _, tmp = render_fixture(fixture_data())
        self.addCleanup(tmp.cleanup)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["brief.json", "cases.html", "data.json", "grid.html", "index.html", "run.html"])
        for page in ("index.html", "run.html", "grid.html", "cases.html"):
            text = (out_dir / page).read_text()
            self.assertNotIn("legacy", text.lower(), page)
            self.assertIn('href="grid.html"', text, page)
            self.assertIn('href="cases.html"', text, page)
        for template in TEMPLATES:
            self.assertNotIn("legacy", template.read_text().lower(), template.name)

    def test_the_nav_marks_the_page_and_shows_the_pr_view_only_when_opened(self):
        out_dir, _, tmp = render_fixture(fixture_data())
        self.addCleanup(tmp.cleanup)
        index = (out_dir / "index.html").read_text()
        self.assertIn('<a href="index.html" class="on">Brief</a>', index)
        self.assertIn('data-page="brief"', index)
        self.assertNotIn(">PR view</a>", index)
        grid = (out_dir / "grid.html").read_text()
        self.assertIn('<a href="grid.html" class="on">Grid</a>', grid)
        self.assertIn('data-page="grid"', grid)
        cases = (out_dir / "cases.html").read_text()
        self.assertIn('<a href="cases.html" class="on">Cases</a>', cases)
        run = (out_dir / "run.html").read_text()
        self.assertIn('<a href="run.html" class="on">PR view</a>', run)
        self.assertIn("head f6e5d4c", index)
        self.assertIn("updated 12:00 UTC", index, "the baked badge; the page rewrites it in ET on load")

    def test_empty_data_still_renders_every_page(self):
        data = {"schema_version": 1, "generated_at": "2026-08-28T14:02:11Z", "source": "logs", "runs": [], "cases": []}
        out_dir, brief, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertEqual((brief["runs"], brief["cases"], brief["releases"]), ([], {}, []))
        self.assertTrue((out_dir / "grid.html").exists())

    def test_todays_production_shape_without_reps_falls_back_to_single_results(self):
        data = {
            "schema_version": 1, "generated_at": "2026-08-28T14:02:11Z", "source": "logs",
            "runs": [{"build_id": "b1", "pr": 998, "started": "2026-08-27T09:00:00Z", "result": "FAILURE", "duration_s": 5793,
                      "tasks": [{"name": "case-x", "result": "pass"}, {"name": "case-y", "result": "fail"}, {"name": "case-z", "result": "infra"}]}],
            "cases": [{"name": "case-x", "active": True}, {"name": "case-y", "active": True}, {"name": "case-z", "active": True}],
        }
        _, brief, tmp = render_fixture(data, admitted=None)
        self.addCleanup(tmp.cleanup)
        self.assertEqual([c["strip"][0]["state"] for c in (brief["cases"][n] for n in ("case-x", "case-y", "case-z"))], ["pass", "fail", "infra"])
        self.assertEqual(brief["cases"]["case-y"]["rates"]["presubmit"], [[0, 1], [0, 1]])
        self.assertEqual(brief["cases"]["case-z"]["rates"]["presubmit"], [None, None], "infra is never in a denominator")
        self.assertEqual(brief["cases"]["case-x"]["domain"], "unknown")

    def test_malformed_entries_degrade_instead_of_aborting_the_render(self):
        # One off-shape entry from a collector must never abort the whole
        # render (the publish hook would then skip every cycle and the
        # dashboard would silently go stale).
        data = fixture_data()
        data["cases"].append("stray-string")
        data["cases"].append({"name": "case-bad-depth", "active": True, "runs_on_record": "6"})
        data["cases"].append({"domain": "no-name"})
        data["coverage"] = ["oops"]
        data["runs"].append({"build_id": "bBroken", "tasks": "not-a-list"})
        data["runs"].append("not-a-run")
        _, brief, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn("case-bad-depth", brief["cases"])
        self.assertNotIn("None", brief["cases"])

    def test_unknown_additive_fields_are_ignored(self):
        data = fixture_data()
        data["a_future_field"] = {"x": 1}
        data["runs"][0]["novel"] = True
        data["cases"][0]["novel"] = "yes"
        _, brief, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn("case-a", brief["cases"])

    def test_hostile_data_never_escapes_the_script_block(self):
        data = fixture_data()
        data["cases"][0]["name"] = "</script><script>alert(1)</script>"
        data["cases"][0]["domain"] = "<!--<script>"
        data["runs"][-2]["tasks"][3]["reps"] = [rep("fail", "<img src=x onerror=alert(2)> boom")]
        out_dir, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        for page in ("index.html", "cases.html", "grid.html"):
            text = (out_dir / page).read_text()
            self.assertNotIn("</script><script>alert", text)
            self.assertNotIn("<!--<script>", text)
            self.assertNotIn("<img src=x", text)
            self.assertIn("\\u003c/script>\\u003cscript>alert(1)\\u003c/script>", text)
        self.assertEqual(json.loads(render.bootstrap_json("<!--<script></script>")), "<!--<script></script>")

    def test_token_shaped_data_does_not_expand_template_markers(self):
        data = fixture_data()
        data["cases"][0]["name"] = "__PAGES_JS__"
        out_dir, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        text = (out_dir / "index.html").read_text()
        self.assertEqual(text.count("__PAGES_JS__"), 1, "once, inside the JSON bootstrap")
        self.assertNotIn("__META__", text)


class CaseNotesTest(unittest.TestCase):
    def test_absent_notes_file_means_no_notes(self):
        self.assertEqual(render.load_notes(pathlib.Path("/nonexistent/notes.yaml")), {})

    def test_malformed_notes_shapes_degrade_to_no_notes(self):
        # The docstring's promise: absent, empty, or malformed all mean
        # "no note", never a crash (a bad case-notes.yaml edit must cost a
        # note, not the dashboard).
        shapes = (
            "- a-top-level-list\n",
            "notes:\n  - a-list-not-a-mapping\n",
            "notes:\n  case-a:\n    issues: 123\n",
            'notes:\n  case-a:\n    issues: "#123"\n',  # scalar, not list
            "notes:\n  case-a: {\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            for text in shapes:
                path = pathlib.Path(tmp) / "notes.yaml"
                path.write_text(text)
                notes = render.load_notes(path)
                for entry in notes.values():
                    self.assertEqual(entry["issues"], [])

    def test_a_bare_string_entry_is_a_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "notes.yaml"
            path.write_text("notes:\n  case-a: redesigned 08-27\n")
            self.assertEqual(render.load_notes(path), {"case-a": {"note": "redesigned 08-27", "issues": []}})

    def test_repo_notes_file_parses_and_carries_seed_annotations(self):
        notes = render.load_notes(REPO_NOTES)
        for name in ("agent-kanban-smoke", "capacity-pinned-pool-probe", "compliance-rbac-overgrant", "gpu-stress-test-diagnosis"):
            self.assertIn(name, notes)
        self.assertEqual(notes["compliance-rbac-overgrant"]["issues"], ["#998", "#985", "#1171"])
        self.assertEqual(notes["capacity-pinned-pool-probe"]["issues"], ["#1010"])
        for entry in notes.values():
            self.assertEqual(set(entry), {"note", "issues"})


class EventsTest(unittest.TestCase):
    def test_absent_events_file_degrades(self):
        self.assertEqual(render.load_events(pathlib.Path("/nonexistent/events.yaml")), {"catches": None})

    def test_malformed_entries_are_dropped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "events.yaml"
            path.write_text("catches: 7\n")
            self.assertIsNone(render.load_events(path)["catches"])
            path.write_text("catches:\n  product_bugs: four\n  prs_blocked: 2\n")
            self.assertEqual(render.load_events(path)["catches"], {"product_bugs": None, "prs_blocked": 2, "ledger": None})
            path.write_text("- a list\n")
            self.assertIsNone(render.load_events(path)["catches"])

    def test_repo_events_file_parses_and_carries_seed_annotations(self):
        events = render.load_events(REPO_EVENTS)
        self.assertEqual(events["catches"], {"product_bugs": 4, "prs_blocked": 2, "ledger": "#1054"})


class PublishTest(unittest.TestCase):
    def _rendered_out_dir(self):
        out_dir, _, tmp = render_fixture(fixture_data())
        self.addCleanup(tmp.cleanup)
        return out_dir

    def test_gsutil_command_construction(self):
        files = [pathlib.Path("/o/data.json"), pathlib.Path("/o/index.html")]
        self.assertEqual(
            publish.gsutil_command(files, "gs://bucket/dash"),
            ["gsutil", "-h", "Cache-Control: no-cache", "cp",
             "/o/data.json", "/o/index.html", "gs://bucket/dash/"],
        )

    def test_gs_target_would_run_gsutil_but_is_never_executed_here(self):
        out_dir = self._rendered_out_dir()
        calls = []

        def recording_runner(argv, check):
            calls.append((argv, check))

        publish.publish(str(out_dir), "gs://bucket/dash", runner=recording_runner)
        (argv, check), = calls
        self.assertTrue(check)
        self.assertEqual(argv[:4], ["gsutil", "-h", "Cache-Control: no-cache", "cp"])
        self.assertEqual(argv[-1], "gs://bucket/dash/")
        for name in ("index.html", "grid.html", "cases.html", "run.html", "brief.json", "data.json"):
            self.assertIn(str(out_dir / name), argv)

    def test_local_target_copies_without_any_subprocess(self):
        out_dir = self._rendered_out_dir()

        def forbidden_runner(*args, **kwargs):
            raise AssertionError("local publish must not shell out")

        with tempfile.TemporaryDirectory() as target:
            dest = pathlib.Path(target) / "serve"
            publish.publish(str(out_dir), str(dest), runner=forbidden_runner)
            self.assertTrue((dest / "index.html").exists())
            self.assertTrue((dest / "cases.html").exists())
            self.assertEqual(
                (dest / "data.json").read_text(), (out_dir / "data.json").read_text()
            )

    def test_empty_out_dir_refuses(self):
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(SystemExit):
            publish.publish(empty, "gs://bucket/dash", runner=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
