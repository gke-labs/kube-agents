"""Golden and contract tests for the eval dashboard renderer and publisher.

The fixtures here are built against schema_version 1 of the collector's
data.json -- including the optional additive ``tasks[].reps`` and
``runs[].pr_merged`` fields (SCHEMA.md, "Optional run and task fields"),
which no collector version emits yet -- deliberately in this file rather
than shared with the collector: the renderer must keep working from the
written contract alone, so these tests are the contract's teeth on the
reading side. Both directions are covered: reps present (rep-level cells,
cohorts, Pareto) and reps absent (today's production data), which must fall
back to each task's single result.

The publish tests never touch a bucket. The gsutil argv is asserted as a
value (``gsutil_command``), and ``publish`` is only ever *executed* against a
local directory -- a gs:// target in these tests gets a recording fake for a
runner, and the local-path test uses a runner that fails the test if called.
"""

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from eval_dashboard import publish, render

REPO_NOTES = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "case-notes.yaml"
REPO_EVENTS = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "events.yaml"

# A reason long enough to exercise the 60-char snippet fallback, carrying an
# agent-classed keyword ("false finding").
LONG_REASON = (
    "false finding on a healthy workload: the agent invented a PDB violation "
    "that does not exist"
)


def rep(result, reason=None):
    return {"n": 1, "result": result, "reason": reason}


def fixture_data():
    """Six runs against five active cases, telling the whole story:

    - run A (#900, merged, 08-20): prior-week cohort anchor, 4 pass / 1 fail.
    - run B (#950, merged, 08-30): superseded by run C of the same PR, so it
      must not appear in the merged-PR cohort at all.
    - run C (#950, merged, 08-31): final run of PR 950 -- 6 pass / 1 fail,
      with a partial cell and an all-infra cell.
    - run D (#951, NOT merged, 08-31): run-level event (4 of 5 graded tasks
      failed); renders as a column but its failures charge the run.
    - run E (#952, pr_merged absent, 09-01): the Pareto's raw material --
      429 reps, not-a-real-run reps, an exact-check miss, a reason-less
      fail, and a long agent-classed reason.
    - run F (#953, merged, 09-01, SUCCESS): the latest green full run.
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


def fixture_events_yaml():
    return (
        "events:\n"
        '  - date: "2026-08-31"\n'
        '    label: "#1063 + 360m timeout"\n'
        "catches:\n"
        "  product_bugs: 4\n"
        "  prs_blocked: 2\n"
        '  ledger: "#1054"\n'
        "false_reds_7d: 1\n"
    )


def render_fixture(data, notes_path=None, events_path=None, events_yaml=None):
    """Run the real CLI against a temp dir; returns (html, out_dir, tmp).
    Notes and events default to *absent* files so the repo's own annotation
    files never leak into a test; pass events_yaml to write one inline."""
    tmp = tempfile.TemporaryDirectory()
    out_dir = pathlib.Path(tmp.name) / "out"
    data_path = pathlib.Path(tmp.name) / "data.json"
    data_path.write_text(json.dumps(data))
    if events_yaml is not None:
        events_path = pathlib.Path(tmp.name) / "events.yaml"
        events_path.write_text(events_yaml)
    argv = ["--data", str(data_path), "--out-dir", str(out_dir)]
    argv += ["--notes", str(notes_path or pathlib.Path(tmp.name) / "no-notes.yaml")]
    argv += ["--events", str(events_path or pathlib.Path(tmp.name) / "no-events.yaml")]
    with contextlib.redirect_stdout(io.StringIO()):
        render.main(argv)
    # The two-band page these golden tests pin is published as legacy.html;
    # index.html is the Brief (test_eval_dashboard_pages.py).
    return (out_dir / "legacy.html").read_text(), out_dir, tmp


def baked_app(html):
    """The server-rendered fragment only: everything render.py substituted
    for __APP__, and none of the template's own JS source. Assertions
    against the whole page are toothless for any string the JS mirror
    carries as a literal ("The gate", the legend labels, the tile markup...),
    because the template ships that source verbatim in every page."""
    return html.split('<div id="app">', 1)[1].split("<script>", 1)[0]


def script_source(html):
    """The template's inline JS, for pinning the live-side contract."""
    return "".join(part.split("</script>", 1)[0] for part in html.split("<script>")[1:])


def daytrend_of(app):
    if 'id="daytrend"' not in app:
        raise AssertionError("no daytrend fragment rendered")
    return app.split('id="daytrend"', 1)[1].split("</div>", 1)[0]


def mx_row_for(app, case_name):
    rows = [r for r in app.split('<div class="mx-row">') if f">{case_name}</span>" in r]
    if not rows:
        raise AssertionError(f"no matrix row for {case_name}")
    # The last row's split segment runs on into the event footnote, the
    # legend and the Pareto; cut it back to the row itself.
    return rows[0].split('<div class="mx-ev">')[0].split('<div class="legend')[0]


class RenderGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html, cls.out_dir, cls._tmp = render_fixture(
            fixture_data(), events_yaml=fixture_events_yaml()
        )
        cls.app = baked_app(cls.html)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_two_bands_with_cohort_captions(self):
        self.assertIn('<h2 id="agent">The agent</h2>', self.app)
        self.assertIn("cohort: final run of each merged PR", self.app)
        self.assertIn('<h2 id="gate">The gate</h2>', self.app)
        # The run-level rule is stated verbatim on the gate band, in the
        # matrix caption, on the Pareto and on the evidence table.
        self.assertEqual(
            self.app.count("run-level events excluded from per-case stats"), 4
        )

    def test_week_pass_rate_tile_with_delta_vs_prior_week(self):
        # This week (window ends at generated_at 09-01T12:00): run C
        # (6 pass / 1 fail) + run F (7 pass) = 13/14 -> 93%. Run B is the
        # same PR as C and superseded; run D is unmerged; run E has no
        # pr_merged. Prior week: run A, 4/5 -> 80%. Delta +13pt.
        self.assertIn("93<small>%</small>", self.app)
        self.assertIn("▲ +13pt vs prior week", self.app)
        self.assertIn("merged-PR cohort · 2 runs this week", self.app)

    def test_product_bugs_tile_from_events_yaml(self):
        self.assertIn("4<small> + 2 PRs blocked</small>", self.app)
        self.assertIn("catch ledger #1054", self.app)

    def test_domains_tile(self):
        self.assertIn("11<small>/ 11</small>", self.app)
        self.assertIn("all covered", self.app)
        self.assertIn("6 scenarios · 5 blocking", self.app)

    def test_false_reds_tile_from_events_yaml(self):
        self.assertIn(
            '<div class="k">False reds · 7d</div><div class="v">1</div>', self.app
        )

    def test_infra_rep_rate_computed_from_reps(self):
        # 45 reps across the 6-run window, 5 infra -> 11.1%.
        self.assertIn("11.1<small>%</small>", self.app)
        self.assertIn("5 of 45 reps · last 6 runs", self.app)

    def test_wall_clock_of_latest_green_full_run(self):
        # Run F: SUCCESS, 9000s -> 150 min.
        self.assertIn("150<small>min</small>", self.app)
        self.assertIn("latest green full run · #953", self.app)

    def test_queue_wait_never_faked(self):
        self.assertIn(
            '<div class="k">Queue wait · median</div><div class="v">—</div>', self.app
        )
        self.assertIn("not reported in data.json", self.app)

    def test_matrix_cell_states_from_reps(self):
        # case-a: pass in A/B/C/F, fail-all in D, partial in E.
        row = mx_row_for(self.app, "case-a")
        self.assertEqual(row.count("c-g"), 4)
        self.assertEqual(row.count("c-r"), 1)
        self.assertEqual(row.count("c-a"), 1)
        # case-c: absent from A/B (not-run), infra-excluded in E.
        row = mx_row_for(self.app, "case-c")
        self.assertEqual(row.count("c-n"), 2)
        self.assertEqual(row.count("c-i"), 1)
        # case-d: all-infra reps in C render hollow, not failed.
        row = mx_row_for(self.app, "case-d")
        self.assertEqual(row.count("c-i"), 1)

    def test_reps_absent_task_falls_back_to_single_result(self):
        # case-d has no reps in D and E (bare result: fail) -> two red cells.
        row = mx_row_for(self.app, "case-d")
        self.assertEqual(row.count("c-r"), 2)

    def test_inactive_case_has_no_matrix_row(self):
        with self.assertRaises(AssertionError):
            mx_row_for(self.app, "case-f")

    def test_run_level_event_column_renders_but_is_charged_to_the_run(self):
        # Run D (4 of 5 graded tasks failed) still renders as a column...
        self.assertIn("run-level event", self.app)
        row = mx_row_for(self.app, "case-e")
        self.assertEqual(row.count("c-g"), 1)  # its pass in run D still shows
        # ...but none of its failure reasons reach the Pareto.
        self.assertNotIn("EVENT-ONLY-REASON", self.app)
        # The one reason-less fail counted there is run E's case-d, not the
        # three reason-less fails of run D.
        self.assertIn(
            '<div class="pa-count">1</div><div class="pa-name">(no reason recorded)</div>',
            self.app,
        )

    def test_pareto_normalization_and_classes(self):
        # HTTP 429 reps (one fail + one infra in run E) -> one infra-classed
        # group.
        self.assertIn("endpoint saturation (infra)", self.app)
        self.assertIn('pa-infra" style="width:', self.app)
        # "required phrases absent" with an extractable check name, seen in
        # runs B, C and E -> 3, check-classed, and the top bar (100%).
        self.assertIn(
            '<div class="pa-count">3</div><div class="pa-name">exact-check: kanban-columns</div>',
            self.app,
        )
        self.assertIn('pa-check" style="width:100.0%', self.app)
        # NOT_A_REAL_RUN wording gets its own group, neutral-classed.
        self.assertIn("not a real agent run", self.app)
        self.assertIn("pa-unknown", self.app)
        # Anything else groups by its first 60 chars; "false finding" is
        # agent-classed.
        self.assertIn(render.esc(LONG_REASON[:60]) + "…", self.app)
        self.assertIn("pa-agent", self.app)
        # Reason-less infra reps (run C's case-d) get their own group and
        # keep the infra class -- the result itself says what they were.
        self.assertIn(
            '<div class="pa-count">3</div><div class="pa-name">(infra, no reason recorded)</div>',
            self.app,
        )

    def test_day_trend_uses_final_run_per_merged_pr(self):
        trend = daytrend_of(self.app)
        # Run B (08-30) is superseded by run C of the same PR: no point.
        self.assertNotIn("08-30", trend)
        # Day fractions: C alone on 08-31 (6/7 -> 86%), F alone on 09-01.
        self.assertIn('data-l="2026-08-31 · 86%"', trend)
        self.assertIn('data-l="2026-09-01 · 100%"', trend)
        # Unmerged (D) and unknown (E) runs never chart.
        self.assertNotIn('data-l="2026-08-31 · 5', trend)

    def test_event_markers_from_events_yaml(self):
        # Chart labels are clipped at 18 chars so clustered events stay
        # readable; the matrix footnote carries the full text.
        self.assertIn("#1063 + 360m timeo…", daytrend_of(self.app))
        # The matrix column for 08-31 carries the marker and the footnote
        # names it.
        self.assertIn("▲ 08-31 #1063 + 360m timeout", self.app)

    def test_evidence_table_slimmed_but_present(self):
        self.assertIn("Evidence on record", self.app)
        self.assertIn("6 of 20", self.app)
        self.assertIn(">IN PRESUBMIT</span>", self.app)
        self.assertIn(">NOT IN PRESUBMIT</span>", self.app)  # case-f
        # Per-tier rate columns, presubmit and nightly, 7 and 30 days.
        for head in ("<th>presubmit · 7d</th>", "<th>presubmit · 30d</th>", "<th>nightly · 7d</th>", "<th>nightly · 30d</th>"):
            self.assertIn(head, self.app)
        # case-a, presubmit (windows end at generated_at 09-01T12:00; run D
        # is a run-level event and never counts): 7d = B 3/3, C 3/3, E 2/3,
        # F 3/3 -> 11 of 12; 30d adds run A's 3/3 -> 14 of 15. No nightly on
        # record: two dashes.
        row = self.app.split('<div class="tname">case-a</div>', 1)[1].split("</tr>", 1)[0]
        cells = row.split("<td>")[2:6]
        self.assertIn('<span class="num">92%</span> <span class="cap">(12)</span>', cells[0])
        self.assertIn('<span class="num">93%</span> <span class="cap">(15)</span>', cells[1])
        self.assertEqual(row.count('<span class="cap">—</span>'), 2)
        self.assertIn("0 nightly", row)

    def test_superseded_sections_are_gone(self):
        for marker in (
            "Latest run",  # hero tile
            "judge score",  # single-case judge chart
            "Suite pass fraction, per run",  # per-PR-number x-axis chart
            "Median case cost",
            "Test suite",  # per-case table, superseded by the matrix
        ):
            self.assertNotIn(marker, self.app)

    def test_releases_empty_state_rendered(self):
        self.assertIn("No RC in the gate window", self.app)

    def test_freshness_timestamp_from_generated_at(self):
        self.assertIn("updated 12:00 UTC", self.html)

    def test_head_sha_of_latest_run_in_header(self):
        self.assertIn("head f6e5d4c", self.html)

    def test_data_json_copied_next_to_index(self):
        copied = json.loads((self.out_dir / "data.json").read_text())
        self.assertEqual(copied["generated_at"], "2026-09-01T12:00:00Z")


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


class NightlyTierRenderTest(unittest.TestCase):
    """A nightly run in data.json (runs[].tier) shows up where the nightly is
    meant to -- the evidence table's nightly columns and count, the footer
    -- and nowhere the gate is judged: no matrix column, no Pareto bar, no
    infra-rep tile, no wall-clock tile, no Brief run."""

    @classmethod
    def setUpClass(cls):
        data = fixture_data()
        data["runs"].append(nightly_run())
        data["cases"].append({"name": "case-g", "domain": "obtainability", "active": False, "nightly_active": True,
                              "runs_on_record": 0, "nightly": {"runs_on_record": 1, "pass_rate": 0.0, "last3": ["fail"]}})
        data["cases"][0]["nightly"] = {"runs_on_record": 1, "pass_rate": 0.0, "last3": ["fail"]}
        cls.data = data
        cls.html, cls.out_dir, cls._tmp = render_fixture(data, events_yaml=fixture_events_yaml())
        cls.app = baked_app(cls.html)
        cls.control = baked_app(render_fixture(fixture_data(), events_yaml=fixture_events_yaml())[0])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_gate_band_and_the_agent_band_are_unchanged(self):
        gate = lambda app: app.split('<h2 id="gate">', 1)[1].split('<h2 id="nightly">', 1)[0]
        agent = lambda app: app.split('<h2 id="agent">', 1)[1].split('<h2 id="gate">', 1)[0]
        self.assertEqual(gate(self.app), gate(self.control), "matrix, tiles and Pareto read presubmit runs only")
        # The agent band differs only by the scenario count, which is the
        # cases[] list (case-g was added), not the runs.
        self.assertEqual(agent(self.app).replace("7 scenarios", "6 scenarios"), agent(self.control))
        self.assertEqual(self.app.count("mx-col"), 6)
        self.assertNotIn("NIGHTLY-ONLY-REASON", self.app)
        self.assertNotIn("bN", gate(self.app))

    def test_the_evidence_table_carries_the_nightly_record_apart(self):
        row = self.app.split('<div class="tname">case-a</div>', 1)[1].split("</tr>", 1)[0]
        cells = row.split("<td>")[2:6]  # presubmit 7d, 30d, nightly 7d, 30d
        self.assertIn("92%", cells[0])
        self.assertIn("93%", cells[1])
        self.assertIn('<span class="num">0%</span> <span class="cap">(3)</span>', cells[2])
        self.assertIn('<span class="num">0%</span> <span class="cap">(3)</span>', cells[3])
        self.assertIn("1 nightly", row)
        # case-g runs in the nightly only: its row exists, presubmit side empty.
        row_g = self.app.split('<div class="tname">case-g</div>', 1)[1].split("</tr>", 1)[0]
        self.assertIn(">NIGHTLY ONLY</span>", row_g)
        self.assertIn("0 of 20", row_g)
        self.assertIn('<span class="num">33%</span> <span class="cap">(3)</span>', row_g)
        self.assertNotIn("NIGHTLY ONLY", self.control)

    def test_the_footer_counts_the_tiers(self):
        self.assertIn("7 runs on record, 1 of them nightly; the agent and gate bands read presubmit runs only", self.app)
        self.assertIn("6 runs on record, 0 of them nightly", self.control)

    def test_brief_json_lists_presubmit_runs_only(self):
        brief = json.loads((self.out_dir / "brief.json").read_text())
        self.assertNotIn("bN", [r["build"] for r in brief["runs"]])
        self.assertEqual(len(brief["runs"]), 6)
        self.assertTrue(brief["cases"]["case-g"]["nightly_active"])
        self.assertFalse(brief["cases"]["case-a"]["nightly_active"], "absent in the fixture reads false")
        # The nightly still informs each case's note on the PR view.
        run_e = next(r for r in brief["runs"] if r["build"] == "bE")
        case_a = next(c for c in run_e["cases"] if c["case"] == "case-a")
        self.assertIs(case_a["nightly_failed_recent"], True)

    def test_the_js_mirror_carries_the_tier_filter(self):
        js = script_source(self.html)
        for token in ('tiers: ["presubmit", "nightly"]', "tierRateWindowsDays: [7, 30]", "const runTier =", "const gateRuns =",
                      "function tierPassRates(", "NIGHTLY ONLY", "const measuredRuns = (data) => gateRuns(data)",
                      f"evidenceFixedColumns: {render.EVIDENCE_FIXED_COLUMNS}"):
            self.assertIn(token, js)

    def test_an_unknown_tier_is_neither_the_gates_nor_the_nightlys(self):
        data = fixture_data()
        data["runs"][-1]["tier"] = "rc"  # run F, the latest green full run
        html, _, tmp = render_fixture(data, events_yaml=fixture_events_yaml())
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertEqual(app.count("mx-col"), 5)
        self.assertNotIn("latest green full run · #953", app)
        self.assertIn("6 runs on record, 0 of them nightly", app)


class ReasonSignatureTest(unittest.TestCase):
    def test_the_never_ran_reason_groups_and_classes_as_infra(self):
        # The wording classify_rep() writes for #1184's empty-success record
        # (bench/kube_agents_bench/scoring.py); a #1184 wave must group under
        # one named infra bar, not scatter into first-60-chars groups.
        reason = (
            "the record shows no agent ever ran: the trajectory is empty and "
            "tokens.total is 0, so no model call was billed. There is no "
            "answer in it to grade, whatever produced it -- infrastructure, "
            "not the pull request (#1184)"
        )
        self.assertEqual(
            render.reason_signature(reason),
            "never ran: empty trajectory, zero tokens (infra)",
        )
        self.assertEqual(render.reason_class(reason), "infra")

    def test_the_js_mirror_carries_the_never_ran_signature(self):
        # The page re-renders the Pareto client-side from the template's own
        # JS mirror of these maps, so a signature added to render.py alone is
        # invisible on the live surface — the server-rendered bar is replaced
        # on load. Caught by a headless-Chrome capture during #1184's review.
        tmpl = (
            pathlib.Path(__file__).resolve().parent
            / "eval_dashboard" / "template" / "index.html.tmpl"
        ).read_text()
        self.assertIn(f'sigNeverRan: "{render.SIG_NEVER_RAN}"', tmpl)
        self.assertIn('return "never ran: empty trajectory, zero tokens (infra)"', tmpl)
        # ...and the class map: the keyword must sit in the JS infra list too.
        self.assertIn(f'"{render.SIG_NEVER_RAN}"]', tmpl)


RC_TASKS = [{"name": f"case-{n}", "result": "pass" if n < 15 else "fail"} for n in range(25)]
RC_TASKS.append({"name": "case-infra", "result": "infra"})

# The real post-kube-agents-eval-rc build the collector fixture is built from
# (scripts/eval_dashboard/testdata_rc/), reduced to the releases[] record.
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


def releases_section(app):
    return app.split('<h2 id="release">', 1)[1]


# The template is a page, not a module: its mirror lives in the second
# <script> block and ends with top-level calls that touch the DOM. To call one
# function out of it, take that block, blank the placeholders render.py fills
# (they are not valid JS until then), and give the top-level tail just enough
# of a browser to run against.
_JS_BLOCK = re.compile(r"<script>\n(/\* Live read side\..*?)</script>", re.S)
_JS_PLACEHOLDER = re.compile(r"__[A-Z_]+__")
_JS_BROWSER_STUB = """
const noop = () => {};
const el = new Proxy({}, {get: (t, k) => (k === "style" || k === "dataset"
  ? new Proxy({}, {get: noop, set: () => true}) : (k === "textContent"
  || k === "innerHTML" ? "" : noop)), set: () => true});
globalThis.document = new Proxy({}, {get: () => (() => el)});
globalThis.location = {protocol: "file:"};
globalThis.setInterval = noop;
globalThis.fetch = () => Promise.reject(new Error("no network in the parity harness"));
"""


def js_releases_html(data: dict) -> str:
    """`releasesHtml(data)` as the template's own JS computes it."""
    tmpl = (
        pathlib.Path(__file__).resolve().parent
        / "eval_dashboard" / "template" / "index.html.tmpl"
    ).read_text()
    match = _JS_BLOCK.search(tmpl)
    assert match, "could not find the live-read <script> block in the template"
    script = _JS_PLACEHOLDER.sub("null", match.group(1))
    driver = 'process.stdout.write(releasesHtml(JSON.parse(process.env.PARITY_DATA)));'
    proc = subprocess.run(
        ["node", "-e", _JS_BROWSER_STUB + script + "\n" + driver],
        capture_output=True, text=True,
        env={**os.environ, "PARITY_DATA": json.dumps(data)},
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout


class ReleasesSectionTest(unittest.TestCase):
    """The Releases band: releases[] is optional and additive, so every state
    a real data.json can reach has to render -- absent, populated, and the
    half-parsed record a driver that died before its banner leaves behind."""

    def render_with(self, releases, runs=None):
        data = {
            "schema_version": 1, "generated_at": "2026-09-10T12:00:00Z",
            "source": "logs", "cases": [], "releases": releases,
            # One run so the page is not the global "no evaluation data yet"
            # state, which owns the whole <div id="app"> and has no bands.
            "runs": [{"build_id": "b1", "pr": 1, "started": "2026-09-01T10:00:00Z",
                      "finished": "2026-09-01T11:00:00Z", "result": "SUCCESS",
                      "duration_s": 3600, "tasks": []}] if runs is None else runs,
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        return baked_app(html)

    def test_absent_releases_keeps_the_empty_state(self):
        app = self.render_with([])
        self.assertIn("No RC in the gate window", app)
        self.assertNotIn('id="releases"', app)

    def test_a_release_alone_is_enough_to_render_the_page(self):
        """A data.json carrying only release records renders their table
        rather than "no data yet". No publish pipeline produces one -- both
        refuse an empty runs[] before render, deliberately (see app_html) --
        so this pins the renderer's behaviour for a hand-run render, not a
        path CI can take."""
        app = self.render_with([RC_RELEASE], runs=[])
        self.assertNotIn('id="empty-state"', app)
        self.assertIn("staging_2609092307_5b5ad10", releases_section(app))

    def test_neither_runs_nor_releases_is_still_the_global_empty_state(self):
        app = self.render_with([], runs=[])
        self.assertIn('id="empty-state"', app)
        self.assertNotIn('<h2 id="release">', app)

    def test_real_rc_row(self):
        section = releases_section(self.render_with([RC_RELEASE]))
        self.assertNotIn("No RC in the gate window", section)
        self.assertIn(f'href="{RC_RELEASE["artifacts_url"]}"', section)
        self.assertIn("staging_2609092307_5b5ad10", section)
        self.assertIn("5b5ad10 · build 2097891568546484224", section)
        self.assertIn(">nightly<", section)
        self.assertIn('<span class="pill p-pass">GREEN</span>', section)
        self.assertIn("90.0%", section)
        self.assertIn("baselines maturing", section)
        self.assertIn("15/25", section)  # infra case excluded from the denominator
        self.assertIn("1 infra excluded", section)
        self.assertIn("2026-09-10 03:35 UTC", section)
        self.assertIn("4h10m", section)  # the eval's own duration, not the pod's

    def test_a_measured_baseline_renders_the_margin_with_its_sign(self):
        red = dict(RC_RELEASE, verdict="RED", pass_rate=0.885,
                   baseline_rate=0.91, margin=-0.025)
        section = releases_section(self.render_with([red]))
        self.assertIn('<span class="pill p-fail">RED</span>', section)
        self.assertIn("88.5%", section)
        self.assertIn("main 91.0%", section)
        self.assertIn("margin −2.5pt", section)
        self.assertNotIn("baselines maturing", section)

    def test_a_run_with_no_banner_says_so_rather_than_implying_a_pass(self):
        blank = {"build_id": "42", "rc_tag": None, "commit": None, "tier": None,
                 "verdict": None, "result": "FAILURE",
                 "started": "2026-09-10T03:35:01+00:00", "finished": None,
                 "duration_s": None, "project": None, "artifacts_url": None,
                 "pass_rate": None, "baseline_rate": None, "margin": None, "tasks": []}
        section = releases_section(self.render_with([blank]))
        self.assertIn("unknown candidate", section)
        self.assertIn("no eval banner · job FAILURE", section)
        self.assertIn('<span class="pill p-infra">NO VERDICT</span>', section)
        self.assertNotIn("p-pass", section)

    def test_a_non_https_artifacts_url_never_becomes_a_link(self):
        """The URL is scraped out of a build log, so it is untrusted input."""
        hostile = dict(RC_RELEASE, artifacts_url="javascript:alert(1)")
        section = releases_section(self.render_with([hostile]))
        self.assertNotIn("<a ", section)
        self.assertNotIn("javascript:", section)

    def test_the_table_is_capped_at_the_shared_row_limit(self):
        many = [dict(RC_RELEASE, build_id=str(2000 + n),
                     started=f"2026-09-{n + 1:02d}T03:35:01+00:00")
                for n in range(render.RELEASES_MAX_ROWS + 4)]
        section = releases_section(self.render_with(many))
        self.assertEqual(section.count("<tr><td"), render.RELEASES_MAX_ROWS)
        # Newest first: the last-started build survives the cap, the first does not.
        self.assertIn(f"build {many[-1]['build_id']}<", section)
        self.assertNotIn(f"build {many[0]['build_id']}<", section)

    def test_malformed_releases_degrade_instead_of_aborting_the_render(self):
        app = self.render_with(["nonsense", 7, None, {"build_id": "9"}])
        self.assertIn('id="releases"', app)
        self.assertIn("unknown candidate", app)

    def test_a_retyped_releases_field_falls_back_to_the_empty_state(self):
        self.assertIn("No RC in the gate window", self.render_with("soon"))

    def test_the_js_mirror_carries_the_releases_renderer(self):
        # Same trap as the Pareto signatures above: the page re-renders from
        # data.json every 60s, so a Releases section that exists only in
        # render.py reverts to the placeholder on the first poll.
        tmpl = (
            pathlib.Path(__file__).resolve().parent
            / "eval_dashboard" / "template" / "index.html.tmpl"
        ).read_text()
        self.assertIn("function releasesHtml(", tmpl)
        self.assertIn("evidenceHtml(data) + releasesHtml(data)", tmpl)
        self.assertIn(f"releasesMaxRows: {render.RELEASES_MAX_ROWS}", tmpl)
        self.assertIn(f'releaseUrlScheme: "{render.RELEASE_URL_SCHEME}"', tmpl)
        # Not `assertIn(cls, tmpl)`: p-pass/p-fail/p-infra are CSS class names
        # the stylesheet already defines, so that assertion passes even when
        # the mirror's map is empty. Pin the pairs inside the map literal.
        mirror = [ln for ln in tmpl.splitlines() if "releaseVerdictClass:" in ln]
        self.assertEqual(len(mirror), 1, "expected one releaseVerdictClass literal")
        for verdict, cls in render.RELEASE_VERDICT_CLASS.items():
            # JS bare keys where the verdict is an identifier, quoted otherwise.
            key = verdict if verdict.isidentifier() else f'"{verdict}"'
            self.assertIn(f'{key}: "{cls}"', mirror[0],
                          f"{verdict} -> {cls} missing from the JS mirror's map")

    def test_the_brief_links_to_the_releases_section(self):
        """The table renders on the legacy page only, so without this deep
        link the landing page offers no route to it at all."""
        page = (
            pathlib.Path(__file__).resolve().parent
            / "eval_dashboard" / "template" / "page.html.tmpl"
        ).read_text()
        self.assertIn(f'<a href="{render.LEGACY_PAGE}#release">Releases</a>', page)

    @unittest.skipUnless(shutil.which("node"), "needs node to run the JS mirror")
    def test_the_js_mirror_renders_byte_identical_html(self):
        """The structural check above proves the mirror exists; this proves it
        agrees. Runs the template's own releasesHtml under node and diffs it
        against render.py's, over the shapes most likely to diverge: a real
        record, a negative margin, a missing banner, a hostile URL, entries
        that are not dicts, and more releases than the row cap.

        Skipped where node is absent, so a green run without it has not
        checked parity -- the structural test is the floor, this is the proof.
        """
        cap = render.RELEASES_MAX_ROWS
        shapes = {
            "real": [RC_RELEASE],
            "red_with_margin": [{**RC_RELEASE, "verdict": "RED", "pass_rate": 0.62,
                                 "baseline_rate": 0.81, "margin": -0.19}],
            "no_banner": [{**RC_RELEASE, "rc_tag": None, "tier": None,
                           "verdict": None, "artifacts_url": None}],
            "hostile_url": [{**RC_RELEASE, "artifacts_url": "javascript:alert(1)"}],
            "malformed": ["nonsense", 7, None, {"build_id": "3"}],
            "over_cap": [{**RC_RELEASE, "build_id": str(i), "started": f"2026-09-{i:02d}"}
                         for i in range(1, cap + 4)],
            "empty": [],
        }
        for name, releases in shapes.items():
            with self.subTest(shape=name):
                data = {"schema_version": 1, "releases": releases}
                self.assertEqual(
                    js_releases_html(data), render.releases_html(data),
                    f"render.py and the JS mirror disagree on the {name} shape",
                )


class RenderToleranceTest(unittest.TestCase):
    def test_empty_data_renders_designed_empty_state(self):
        data = {"schema_version": 1, "generated_at": "2026-08-28T14:02:11Z",
                "source": "logs", "runs": [], "cases": []}
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn('id="empty-state"', app)
        self.assertIn("No evaluation data yet", app)
        self.assertNotIn("__APP__", html)

    def test_todays_production_shape_degrades_gracefully(self):
        # No reps, no pr_merged anywhere -- exactly what the current
        # collector emits. The matrix falls back to single results, the
        # agent band says honestly that it has no cohort, and the infra
        # tile says it is counting tasks, not reps.
        data = {
            "schema_version": 1,
            "generated_at": "2026-08-28T14:02:11Z",
            "source": "logs",
            "runs": [
                {"build_id": "b1", "pr": 998, "started": "2026-08-27T09:00:00Z",
                 "result": "FAILURE", "duration_s": 5793,
                 "tasks": [
                     {"name": "case-x", "result": "pass"},
                     {"name": "case-y", "result": "fail"},
                     {"name": "case-z", "result": "infra"},
                 ]},
            ],
            "cases": [{"name": "case-x", "active": True},
                      {"name": "case-y", "active": True},
                      {"name": "case-z", "active": True}],
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn("no merged-PR runs on record", app)
        self.assertIn("not enough merged-PR days yet", app)
        self.assertIn("task-level fallback · last 1 runs", app)
        self.assertEqual(mx_row_for(app, "case-x").count("c-g"), 1)
        self.assertEqual(mx_row_for(app, "case-y").count("c-r"), 1)
        self.assertEqual(mx_row_for(app, "case-z").count("c-i"), 1)

    def test_week_boundary_is_exclusive_of_seven_days_ago(self):
        # A run started exactly 7*24h before generated_at belongs to the
        # prior week (window is (ref-7d, ref]); one second later is this
        # week.
        data = {
            "schema_version": 1,
            "generated_at": "2026-09-01T00:00:00Z",
            "source": "logs",
            "runs": [
                {"build_id": "b1", "pr": 1, "pr_merged": True,
                 "started": "2026-08-25T00:00:00Z",
                 "tasks": [{"name": "c", "reps": [rep("pass"), rep("fail")]}]},
                {"build_id": "b2", "pr": 2, "pr_merged": True,
                 "started": "2026-08-25T00:00:01Z",
                 "tasks": [{"name": "c", "reps": [rep("pass"), rep("pass"),
                                                  rep("pass"), rep("fail")]}]},
            ],
            "cases": [{"name": "c", "active": True}],
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn("75<small>%</small>", app)  # this week: b2 alone, 3/4
        self.assertIn("▲ +25pt vs prior week", app)  # prior week: b1, 1/2
        self.assertIn("merged-PR cohort · 1 run this week", app)

    def test_pass_rate_rounding_matches_the_js_rerender(self):
        # 1/8 = 12.5%: Python's banker's rounding would say 12, JS
        # Math.round says 13. The baked HTML must agree with the re-render.
        data = {
            "schema_version": 1,
            "generated_at": "2026-09-01T00:00:00Z",
            "source": "logs",
            "runs": [
                {"build_id": "b1", "pr": 1, "pr_merged": True,
                 "started": "2026-08-31T00:00:00Z",
                 "tasks": [{"name": "c", "reps": [rep("pass")] + [rep("fail")] * 7}]},
            ],
            "cases": [{"name": "c", "active": True}],
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn("13<small>%</small>", baked_app(html))

    def test_empty_events_file_means_dash_tiles(self):
        html, _, tmp = render_fixture(fixture_data(), events_yaml="")
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn(
            '<div class="k">Product bugs caught</div><div class="v">—</div>', app
        )
        self.assertIn('<div class="k">False reds · 7d</div><div class="v">—</div>', app)
        self.assertEqual(app.count("not annotated (events.yaml)"), 2)

    def test_absent_events_file_means_dash_tiles_and_no_markers(self):
        html, _, tmp = render_fixture(fixture_data())  # no --events file
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn('<div class="k">False reds · 7d</div><div class="v">—</div>', app)
        self.assertNotIn("mx-ev", app)

    def test_zero_task_run_gets_no_matrix_column(self):
        data = fixture_data()
        data["runs"].append({
            "build_id": "bAborted", "pr": 999,
            "started": "2026-09-01T10:00:00Z", "finished": "2026-09-01T10:09:00Z",
            "result": "ABORTED", "duration_s": 540, "tasks": [],
        })
        html, _, tmp = render_fixture(data, events_yaml=fixture_events_yaml())
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        # 7 runs on record, 6 measured: 6 matrix columns.
        self.assertEqual(app.count("mx-col"), 6)
        self.assertIn("last 6 measured runs", app)

    def test_all_runs_unmeasured_says_so(self):
        data = {
            "schema_version": 1,
            "generated_at": "2026-08-28T14:02:11Z",
            "source": "logs",
            "runs": [{"build_id": "b1", "result": "ABORTED", "tasks": []}],
            "cases": [{"name": "x", "active": True}],
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn("no measured runs yet", baked_app(html))

    def test_malformed_entries_degrade_instead_of_aborting_the_render(self):
        # One off-shape entry from a collector must never abort the whole
        # render (the publish hook would then skip every cycle and the
        # dashboard would silently go stale).
        data = fixture_data()
        data["cases"].append("stray-string")
        data["cases"].append({"name": "case-bad-depth", "active": True,
                              "runs_on_record": "6"})
        data["cases"].append({"name": "case-bool-depth", "active": True,
                              "runs_on_record": True})
        data["coverage"] = ["oops"]  # re-typed: tile degrades, no crash
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn('<div class="k">Domains covered</div><div class="v">—</div>', app)
        # Non-count depths render as zero evidence, not a crash or a lie.
        self.assertIn("case-bad-depth", app)
        self.assertIn("case-bool-depth", app)

    def test_retyped_uncovered_list_degrades(self):
        data = fixture_data()
        data["coverage"]["uncovered"] = "abc"  # not a list: ignored, not
        html, _, tmp = render_fixture(data)  # iterated character-wise
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertNotIn("uncovered: a, b, c", app)
        self.assertIn("6 scenarios · 5 blocking", app)

    def test_unknown_additive_fields_ignored(self):
        data = fixture_data()
        data["a_future_field"] = {"x": 1}
        data["runs"][0]["novel"] = True
        data["cases"][0]["novel"] = "yes"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn("Case × run outcome matrix", baked_app(html))

    def test_html_in_data_is_escaped(self):
        data = fixture_data()
        data["cases"][0]["name"] = "<script>alert(1)</script>"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_hostile_rep_reason_is_escaped_in_the_pareto(self):
        # reps[].reason is arbitrary log text; it reaches the page only
        # escaped.
        data = fixture_data()
        data["runs"][-2]["tasks"][3]["reps"] = [
            rep("fail", "<img src=x onerror=alert(2)> boom")
        ]
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertNotIn("<img src=x", app)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt; boom", app)

    def test_token_shaped_data_does_not_expand_template_markers(self):
        # A case *named* like a template marker must stay inert text.
        # str.replace over the whole page would re-scan the substituted
        # __APP__ fragment and expand it into the raw JSON bootstrap.
        data = fixture_data()
        data["cases"][0]["name"] = "__DATA_JSON__"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        blob = render.bootstrap_json(data)
        self.assertEqual(html.count(blob), 1)  # the <script> bootstrap only
        self.assertIn('<div class="tname">__DATA_JSON__</div>', html)

    def test_coverage_counts_must_be_whole_numbers(self):
        # The coverage tile's value_html is raw (it carries <small>), so a
        # non-integer domains_covered must fall back to "not reported"
        # rather than being interpolated into markup.
        data = fixture_data()
        data["coverage"]["domains_covered"] = '<img src=x onerror=alert(2)>'
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("<img src=x", html)
        self.assertIn(
            '<div class="k">Domains covered</div><div class="v">—</div>'
            '<div class="d2">not reported</div>',
            html,
        )


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
        )
        with tempfile.TemporaryDirectory() as tmp:
            for text in shapes:
                path = pathlib.Path(tmp) / "notes.yaml"
                path.write_text(text)
                notes = render.load_notes(path)
                for entry in notes.values():
                    self.assertEqual(entry["issues"], [])

    def test_note_and_issue_links_rendered_in_evidence_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = pathlib.Path(tmp) / "notes.yaml"
            notes_path.write_text(
                "notes:\n"
                "  case-a:\n"
                "    note: hardened 08-27\n"
                '    issues: ["#1010"]\n'
            )
            html, _, tmp_render = render_fixture(fixture_data(), notes_path=notes_path)
            self.addCleanup(tmp_render.cleanup)
        app = baked_app(html)
        self.assertIn("hardened 08-27", app)
        self.assertIn('href="https://github.com/gke-labs/kube-agents/issues/1010"', app)

    def test_badges_come_from_notes_not_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = pathlib.Path(tmp) / "notes.yaml"
            notes_path.write_text(
                "notes:\n"
                "  case-a:\n"
                "    badge: held-out\n"
                "  case-b:\n"
                "    badge: new\n"
                "  case-c:\n"
                "    badge: shiny\n"  # unknown badge renders nothing
            )
            html, _, tmp_render = render_fixture(fixture_data(), notes_path=notes_path)
            self.addCleanup(tmp_render.cleanup)
        app = baked_app(html)
        self.assertIn('<em class="b-hold">held out</em>', mx_row_for(app, "case-a"))
        self.assertIn('<em class="b-new">new</em>', mx_row_for(app, "case-b"))
        self.assertNotIn("<em", mx_row_for(app, "case-c"))
        self.assertNotIn("shiny", app)

    def test_repo_notes_file_parses_and_carries_seed_annotations(self):
        notes = render.load_notes(REPO_NOTES)
        for name in (
            "agent-kanban-smoke",
            "capacity-pinned-pool-probe",
            "compliance-rbac-overgrant",
            "gpu-stress-test-diagnosis",
        ):
            self.assertIn(name, notes)
        self.assertEqual(notes["compliance-rbac-overgrant"]["issues"], ["#998", "#985", "#1171"])
        self.assertEqual(notes["compliance-rbac-overgrant"]["badge"], "held-out")
        self.assertEqual(notes["capacity-pinned-pool-probe"]["badge"], "held-out")
        self.assertEqual(notes["security-overgrant-remediation-proposal"]["badge"], "new")


class EventsTest(unittest.TestCase):
    def test_absent_events_file_degrades(self):
        events = render.load_events(pathlib.Path("/nonexistent/events.yaml"))
        self.assertEqual(events, {"events": [], "catches": None, "false_reds_7d": None})

    def test_malformed_entries_are_dropped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "events.yaml"
            path.write_text(
                "events:\n"
                "  - not-a-mapping\n"
                "  - date: 2026-08-29\n"  # unquoted: YAML date object
                "    label: publish fix\n"
                "  - date: 2026-08-30\n"  # no label: dropped
                "catches: 7\n"  # wrong type: dropped
            )
            events = render.load_events(path)
        self.assertEqual(events["events"], [{"date": "2026-08-29", "label": "publish fix"}])
        self.assertIsNone(events["catches"])

    def test_repo_events_file_parses_and_carries_seed_annotations(self):
        events = render.load_events(REPO_EVENTS)
        self.assertEqual(len(events["events"]), 4)
        self.assertEqual(events["events"][0], {"date": "2026-08-27", "label": "kanban redesign"})
        self.assertEqual(events["catches"]["product_bugs"], 4)
        self.assertEqual(events["catches"]["prs_blocked"], 2)
        self.assertEqual(events["catches"]["ledger"], "#1054")
        self.assertEqual(events["false_reds_7d"], 1)


class LiveReadSideTest(unittest.TestCase):
    """The 60s-refresh script render.py bakes into every page."""

    @classmethod
    def setUpClass(cls):
        data = fixture_data()
        # Hostile strings prove the inline-JSON escaping: the first would
        # close the <script> block early; the second would move the HTML
        # tokenizer to the double-escaped script state, where the block's
        # own closing </script> no longer closes it.
        data["cases"][0]["novel_field"] = "</script><b>boom</b>"
        data["cases"][0]["other_field"] = "<!--<script>"
        data["stale_after_s"] = 600
        cls.html, _, cls._tmp = render_fixture(data, events_yaml=fixture_events_yaml())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_bootstrap_data_is_embedded_and_script_safe(self):
        self.assertIn('"generated_at":"2026-09-01T12:00:00Z"', self.html)
        # No '<' from data survives into the script block: neither the
        # tag-closing payload nor the comment-opener one.
        self.assertNotIn("</script><b>boom</b>", self.html)
        self.assertNotIn("<!--", self.html)
        self.assertIn("\\u003c/script>\\u003cb>boom\\u003c/b>", self.html)
        self.assertIn("\\u003c!--\\u003cscript>", self.html)
        # And the escaping is JSON-transparent: parsing it back yields the
        # original strings.
        self.assertEqual(
            json.loads(render.bootstrap_json("<!--<script></script>")),
            "<!--<script></script>",
        )

    def test_polls_data_json_every_60_seconds(self):
        self.assertIn("refreshMs: 60000", self.html)
        self.assertIn('fetch("data.json", { cache: "no-store" })', self.html)
        self.assertIn("setInterval(refresh, DASH.refreshMs)", self.html)

    def test_stale_threshold_read_from_data_with_7200_default(self):
        self.assertIn("stale_after_s", self.html)
        self.assertIn("staleDefaultS: 7200", self.html)
        self.assertIn('"stale_after_s":600', self.html)

    def test_stale_and_unreachable_states_carry_text_labels(self):
        # A template-contract tripwire, deliberately: the baked page cannot
        # reach these states server-side (they exist only after a poll), so
        # this pins the *shipped script* -- the amber badge must always
        # carry a written label, never color alone -- scoped to the script
        # source so it fails if the labels leave the template.
        js = script_source(self.html)
        self.assertIn("`STALE · ${text}`", js)
        self.assertIn("`UNREACHABLE · ${text}`", js)
        self.assertIn(".fresh.stale", self.html)

    def test_notes_travel_with_the_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = pathlib.Path(tmp) / "notes.yaml"
            notes_path.write_text(
                'notes:\n  case-a:\n    issues: ["#1010"]\n    badge: held-out\n'
            )
            html, _, tmp_render = render_fixture(fixture_data(), notes_path=notes_path)
            self.addCleanup(tmp_render.cleanup)
        self.assertIn(
            '"case-a":{"note":null,"issues":["#1010"],"badge":"held-out"}', html
        )

    def test_events_travel_with_the_bootstrap(self):
        self.assertIn(
            '"events":[{"date":"2026-08-31","label":"#1063 + 360m timeout"}]', self.html
        )
        self.assertIn('"false_reds_7d":1', self.html)

    def test_js_mirror_carries_the_shared_thresholds(self):
        js = script_source(self.html)
        self.assertIn("runEventFailFraction: 0.8", js)
        self.assertIn("matrixRuns: 30", js)
        self.assertIn("paretoWindowDays: 7", js)

    def test_js_parse_iso_normalizes_naive_timestamps_to_utc(self):
        # render.py's parse_iso assumes UTC for a timezone-naive stamp;
        # bare Date.parse reads one as local time, so the mirror appends
        # "Z" -- otherwise every day bucket and week window would shift
        # for a viewer outside UTC. Contract tripwire on the shipped
        # script, like the STALE/UNREACHABLE labels.
        self.assertIn('text += "Z"', script_source(self.html))


class PublishTest(unittest.TestCase):
    def _rendered_out_dir(self):
        _, out_dir, tmp = render_fixture(fixture_data())
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
        self.assertIn(str(out_dir / "index.html"), argv)
        self.assertIn(str(out_dir / "data.json"), argv)

    def test_local_target_copies_without_any_subprocess(self):
        out_dir = self._rendered_out_dir()

        def forbidden_runner(*args, **kwargs):
            raise AssertionError("local publish must not shell out")

        with tempfile.TemporaryDirectory() as target:
            dest = pathlib.Path(target) / "serve"
            publish.publish(str(out_dir), str(dest), runner=forbidden_runner)
            self.assertTrue((dest / "index.html").exists())
            self.assertEqual(
                (dest / "data.json").read_text(), (out_dir / "data.json").read_text()
            )

    def test_empty_out_dir_refuses(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(SystemExit):
                publish.publish(empty, "gs://bucket/dash", runner=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
