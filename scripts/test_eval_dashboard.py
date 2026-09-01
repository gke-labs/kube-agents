"""Golden and contract tests for the eval dashboard renderer and publisher.

The fixtures here are built against schema_version 1 of the collector's
data.json, deliberately in this file rather than shared with the collector:
the renderer must keep working from the written contract alone, so these
tests are the contract's teeth on the reading side.

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

from eval_dashboard import publish, render

REPO_NOTES = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "case-notes.yaml"


def fixture_data():
    """Two runs, four cases: one pass, one fail, one INFRA in the latest
    run, and one active case that has never run (IN BUILD)."""
    return {
        "schema_version": 1,
        "generated_at": "2026-08-28T14:02:11Z",
        "source": "logs",
        "runs": [
            {
                "build_id": "2093054394793725952",
                "pr": 998,
                "head_sha": "a28f0b3f00",
                "project": "kube-agents-evals-2",
                "started": "2026-08-27T09:00:00Z",
                "finished": "2026-08-27T10:36:33Z",
                "result": "FAILURE",
                "duration_s": 5793,
                "tasks": [
                    {"name": "reliability-pdb-probe", "result": "pass", "duration_s": 165, "outcome_validity": 1.0},
                    {"name": "capacity-pinned-pool-probe", "result": "pass", "duration_s": 300, "outcome_validity": 1.0},
                    {"name": "compliance-rbac-overgrant", "result": "fail", "duration_s": 1870, "outcome_validity": 0.5},
                ],
            },
            {
                "build_id": "2093414500000000000",
                "pr": 1001,
                "head_sha": "b31c2d40aa",
                "project": "kube-agents-evals-2",
                "started": "2026-08-28T09:00:00Z",
                "finished": "2026-08-28T10:12:00Z",
                "result": "FAILURE",
                "duration_s": 4320,
                "tasks": [
                    {"name": "reliability-pdb-probe", "result": "pass", "duration_s": 182, "outcome_validity": 1.0},
                    {"name": "capacity-pinned-pool-probe", "result": "fail", "duration_s": 129, "outcome_validity": 0.0},
                    {"name": "compliance-rbac-overgrant", "result": "infra", "duration_s": 240},
                ],
            },
        ],
        "cases": [
            {
                "name": "reliability-pdb-probe",
                "domain": "reliability",
                "active": True,
                "runs_on_record": 4,
                "pass_rate": 1.0,
                "last3": ["pass", "pass", "pass"],
                "durations": {"min": 145, "med": 165, "max": 182},
                "ov_history": [
                    {"build_id": "2092000000000000000", "value": 0.5},
                    {"build_id": "2093054394793725952", "value": 1.0},
                    {"build_id": "2093414500000000000", "value": 1.0},
                ],
            },
            {
                "name": "capacity-pinned-pool-probe",
                "domain": "capacity",
                "active": True,
                "runs_on_record": 4,
                "pass_rate": 0.75,
                "last3": ["pass", "pass", "fail"],
                "durations": {"min": 129, "med": 214, "max": 300},
                "ov_history": [
                    {"build_id": "2093054394793725952", "value": 1.0},
                    {"build_id": "2093414500000000000", "value": 0.0},
                ],
            },
            {
                "name": "compliance-rbac-overgrant",
                "domain": "fleet-audits",
                "active": True,
                "runs_on_record": 4,
                "pass_rate": 0.5,
                "last3": ["fail", "fail", "infra"],
                "durations": {"min": 240, "med": 1055, "max": 1870},
            },
            {
                "name": "autoops-warning-event-triage",
                "domain": "incident-triage",
                "active": True,
                "runs_on_record": 0,
                "last3": [],
            },
        ],
        "coverage": {
            "domains_total": 11,
            "domains_covered": 10,
            "uncovered": ["incident-triage"],
        },
    }


def render_fixture(data, notes_path=None):
    """Run the real CLI against a temp dir; returns (html, out_dir)."""
    tmp = tempfile.TemporaryDirectory()
    out_dir = pathlib.Path(tmp.name) / "out"
    data_path = pathlib.Path(tmp.name) / "data.json"
    data_path.write_text(json.dumps(data))
    argv = ["--data", str(data_path), "--out-dir", str(out_dir)]
    argv += ["--notes", str(notes_path or pathlib.Path(tmp.name) / "no-notes.yaml")]
    with contextlib.redirect_stdout(io.StringIO()):
        render.main(argv)
    return (out_dir / "index.html").read_text(), out_dir, tmp


def baked_app(html):
    """The server-rendered fragment only: everything render.py substituted
    for __APP__, and none of the template's own JS source. Assertions
    against the whole page are toothless for any string the JS mirror
    carries as a literal ("Test suite", "not enough runs yet", the pill
    markup...), because the template ships that source verbatim in every
    page."""
    return html.split('<div id="app">', 1)[1].split("<script>", 1)[0]


def script_source(html):
    """The template's inline JS, for pinning the live-side contract."""
    return "".join(part.split("</script>", 1)[0] for part in html.split("<script>")[1:])


def row_for(html, case_name):
    rows = [r for r in html.split("<tr>") if case_name in r]
    if not rows:
        raise AssertionError(f"no table row for {case_name}")
    return rows[0].split("</tr>")[0]


class RenderGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html, cls.out_dir, cls._tmp = render_fixture(fixture_data())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_passed_pill_rendered(self):
        row = row_for(self.html, "reliability-pdb-probe")
        self.assertIn('class="pill p-pass">PASSED</span>', row)

    def test_failed_pill_rendered(self):
        row = row_for(self.html, "capacity-pinned-pool-probe")
        self.assertIn('class="pill p-fail">FAILED</span>', row)

    def test_infra_is_never_rendered_as_failure(self):
        # compliance-rbac-overgrant's latest result is infra: its pill says
        # INFRA, its newest history dot is the infra class, and nothing in
        # the row claims FAILED.
        row = row_for(self.html, "compliance-rbac-overgrant")
        self.assertIn('class="pill p-infra">INFRA</span>', row)
        self.assertIn('class="h-infra"', row)
        self.assertNotIn("FAILED", row)

    def test_never_run_case_is_in_build(self):
        row = row_for(self.html, "autoops-warning-event-triage")
        self.assertIn('class="pill p-pend">IN BUILD</span>', row)
        self.assertEqual(row.count('class="h-na"'), 3)

    def test_freshness_timestamp_from_generated_at(self):
        self.assertIn("updated 14:02 UTC", self.html)

    def test_judge_bar_carries_threshold_tick(self):
        self.assertIn('class="thr" style="left:80%"', self.html)

    def test_uncovered_domain_rendered(self):
        self.assertIn("uncovered: incident-triage", self.html)
        self.assertIn("10<small>/ 11</small>", self.html)

    def test_latest_run_tile_excludes_infra_from_denominator(self):
        # Latest run: 1 pass, 1 fail, 1 infra -> "1 / 2 passed".
        self.assertIn("1<small>/ 2 passed</small>", self.html)

    def test_pass_fraction_chart_excludes_infra(self):
        # Run #998: 2/3 pass -> 0.67. Run #1001: 1 pass, 1 fail, infra
        # excluded -> 0.50, not 0.33.
        self.assertIn("#1001 · 0.50", self.html)
        self.assertIn("#998 · 0.67", self.html)

    def test_judge_trend_picks_deepest_history(self):
        self.assertIn("reliability-pdb-probe · judge score", self.html)

    def test_evidence_depth_against_screening_yardstick(self):
        app = baked_app(self.html)
        self.assertIn("4 of 20", app)
        self.assertIn("0 of 20", app)
        self.assertIn(">IN PRESUBMIT</span>", app)

    def test_releases_empty_state_rendered(self):
        self.assertIn("No RC in the gate window", baked_app(self.html))

    def test_data_json_copied_next_to_index(self):
        copied = json.loads((self.out_dir / "data.json").read_text())
        self.assertEqual(copied["generated_at"], "2026-08-28T14:02:11Z")

    def test_head_sha_of_latest_run_in_header(self):
        self.assertIn("head b31c2d4", self.html)


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

    def test_optional_fields_absent(self):
        # The bare minimum the schema allows: no coverage, no durations,
        # no ov_history, tasks without judge scores, run without pr.
        data = {
            "schema_version": 1,
            "generated_at": "2026-08-28T14:02:11Z",
            "source": "logs",
            "runs": [{"build_id": "b1", "tasks": [{"name": "x", "result": "pass"}]}],
            "cases": [{"name": "x"}],
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn('class="pill p-pass">PASSED</span>', app)
        self.assertIn("not enough runs yet", app)
        self.assertIn("no judge history yet", app)

    def test_null_project_renders_placeholder_not_none(self):
        # SCHEMA.md: project is null when the log never reached the lease
        # line (aborted / deadline-truncated builds). The key is present,
        # so a plain .get(key, "?") would leak the literal string "None".
        data = fixture_data()
        data["runs"][-1]["project"] = None
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("· None", html)
        self.assertIn("· ?", html)

    def test_unknown_additive_fields_ignored(self):
        data = fixture_data()
        data["a_future_field"] = {"x": 1}
        data["runs"][0]["novel"] = True
        data["cases"][0]["novel"] = "yes"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn("Test suite", baked_app(html))

    def test_html_in_data_is_escaped(self):
        data = fixture_data()
        data["cases"][0]["name"] = "<script>alert(1)</script>"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("<script>alert(1)</script>", html)

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
        # The payload never appears raw: not in the document body, and the
        # <script> bootstrap emits every '<' as the JSON escape \\u003c.
        self.assertNotIn("<img src=x", html)
        self.assertIn(
            '<div class="k">Domain coverage</div><div class="v">—</div>'
            '<div class="d2">not reported</div>',
            html,
        )

    def test_prior_run_without_metric_is_not_called_first_run(self):
        # Two runs on record, but the earlier one reported no durations at
        # all: the cost/wall-clock chips stay empty rather than claiming
        # "first run" on run two.
        data = fixture_data()
        for task in data["runs"][0]["tasks"]:
            task.pop("duration_s", None)
        data["runs"][0].pop("duration_s", None)
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        # The rendered chip, not the template's JS source (which contains
        # the words "first run" as a string literal either way).
        self.assertNotIn('<span class="delta flat">first run</span>', html)

    def test_zero_task_newest_run_does_not_anchor_the_tiles(self):
        # A build aborted before any task parses to zero task rows. The
        # tiles and the suite table anchor to the newest *measured* run
        # instead of rendering "0 / 0 passed · all passed", and the wall
        # clock does not inherit the aborted run's short duration.
        data = fixture_data()
        data["runs"].append({
            "build_id": "2094501900000000000",
            "project": None,
            "started": "2026-08-29T09:00:00Z",
            "finished": "2026-08-29T09:09:00Z",
            "result": "ABORTED",
            "duration_s": 540,
            "tasks": [],
        })
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertNotIn("0<small>/ 0 passed</small>", app)
        self.assertNotIn("all passed", app)
        self.assertIn("1<small>/ 2 passed</small>", app)  # newest measured run
        self.assertIn("72<small>min</small>", app)  # 4320 s, not the 9-min abort
        self.assertNotIn("9<small>min</small>", app)

    def test_all_runs_unmeasured_says_so(self):
        data = {
            "schema_version": 1,
            "generated_at": "2026-08-28T14:02:11Z",
            "source": "logs",
            "runs": [{"build_id": "b1", "result": "ABORTED", "tasks": []}],
            "cases": [{"name": "x"}],
        }
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn("no measured runs yet", app)
        self.assertNotIn("all passed", app)

    def test_rounding_matches_the_js_rerender(self):
        # Python rounds half to even, JS Math.round/toFixed round half up;
        # the baked HTML must agree with what the on-load re-render shows.
        data = fixture_data()
        data["cases"][1]["pass_rate"] = 0.125  # Math.round(12.5) === 13
        data["runs"][-1]["tasks"][0]["outcome_validity"] = 0.25  # toFixed(1) === "0.3"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertIn(">13%<", html)
        self.assertIn('<span class="val">0.3</span>', html)


class CaseNotesTest(unittest.TestCase):
    def test_absent_notes_file_means_no_notes(self):
        self.assertEqual(render.load_notes(pathlib.Path("/nonexistent/notes.yaml")), {})

    def test_note_and_issue_links_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes_path = pathlib.Path(tmp) / "notes.yaml"
            notes_path.write_text(
                "notes:\n"
                "  reliability-pdb-probe:\n"
                "    note: hardened 08-27\n"
                '    issues: ["#1010"]\n'
            )
            html, _, tmp_render = render_fixture(fixture_data(), notes_path=notes_path)
            self.addCleanup(tmp_render.cleanup)
        row = row_for(html, "reliability-pdb-probe")
        self.assertIn("hardened 08-27", row)
        self.assertIn('href="https://github.com/gke-labs/kube-agents/issues/1010"', row)
        # A case with no entry renders without a note, not with an error.
        self.assertNotIn('class="tnote"', row_for(html, "capacity-pinned-pool-probe"))

    def test_repo_notes_file_parses_and_carries_seed_annotations(self):
        notes = render.load_notes(REPO_NOTES)
        for name in (
            "agent-kanban-smoke",
            "capacity-pinned-pool-probe",
            "compliance-rbac-overgrant",
            "gpu-stress-test-diagnosis",
        ):
            self.assertIn(name, notes)
        self.assertEqual(notes["compliance-rbac-overgrant"]["issues"], ["#998", "#985"])


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
        cls.html, _, cls._tmp = render_fixture(data)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_bootstrap_data_is_embedded_and_script_safe(self):
        self.assertIn('"generated_at":"2026-08-28T14:02:11Z"', self.html)
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
                'notes:\n  reliability-pdb-probe:\n    issues: ["#1010"]\n'
            )
            html, _, tmp_render = render_fixture(fixture_data(), notes_path=notes_path)
            self.addCleanup(tmp_render.cleanup)
        self.assertIn('"reliability-pdb-probe":{"note":null,"issues":["#1010"]}', html)


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
