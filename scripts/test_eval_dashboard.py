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

    def test_evidence_progress_toward_screening_window(self):
        self.assertIn("4 of 20", self.html)
        self.assertIn("0 of 20", self.html)
        self.assertIn(">HAND-PICKED</span>", self.html)

    def test_releases_empty_state_rendered(self):
        self.assertIn("No RC in the gate window", self.html)

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
        self.assertIn('id="empty-state"', html)
        self.assertIn("No evaluation data yet", html)
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
        self.assertIn('class="pill p-pass">PASSED</span>', html)
        self.assertIn("not enough runs yet", html)
        self.assertIn("no judge history yet", html)

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
        self.assertIn("Test suite", html)

    def test_html_in_data_is_escaped(self):
        data = fixture_data()
        data["cases"][0]["name"] = "<script>alert(1)</script>"
        html, _, tmp = render_fixture(data)
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("<script>alert(1)</script>", html)


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
        # A hostile string proves the inline-JSON escaping: without it this
        # would close the <script> block early.
        data["cases"][0]["novel_field"] = "</script><b>boom</b>"
        data["stale_after_s"] = 600
        cls.html, _, cls._tmp = render_fixture(data)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_bootstrap_data_is_embedded_and_script_safe(self):
        self.assertIn('"generated_at":"2026-08-28T14:02:11Z"', self.html)
        self.assertNotIn("</script><b>boom</b>", self.html)
        self.assertIn("<\\/script><b>boom<\\/b>", self.html)

    def test_polls_data_json_every_60_seconds(self):
        self.assertIn("refreshMs: 60000", self.html)
        self.assertIn('fetch("data.json", { cache: "no-store" })', self.html)
        self.assertIn("setInterval(refresh, DASH.refreshMs)", self.html)

    def test_stale_threshold_read_from_data_with_7200_default(self):
        self.assertIn("stale_after_s", self.html)
        self.assertIn("staleDefaultS: 7200", self.html)
        self.assertIn('"stale_after_s":600', self.html)

    def test_stale_and_unreachable_states_carry_text_labels(self):
        # Not color alone: the amber badge always carries a written label.
        self.assertIn("STALE · ", self.html)
        self.assertIn("UNREACHABLE · ", self.html)
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
