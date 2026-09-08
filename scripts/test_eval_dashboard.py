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
import functools
import html as html_lib
import http.server
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from eval_dashboard import publish, render

REPO_NOTES = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "case-notes.yaml"
REPO_EVENTS = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "events.yaml"

# A headless browser, when one is installed, runs the template's script for
# real: the parity tests compare the baked page with its own JS re-render,
# and the deep-link tests read the DOM a linked URL produces. Absent a
# browser those tests skip; the string tripwires below are the floor.
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
CHROME = next(
    (
        c
        for c in (
            (path if pathlib.Path(path).exists() else shutil.which(path))
            for path in CHROME_CANDIDATES
        )
        if c
    ),
    None,
)
# Every launch gets its own profile directory: the default one is shared
# with a developer's running Chrome and with the other test processes the
# Makefile runs in parallel. Not on macOS, where the branded build's
# updater registers itself into a fresh profile and never returns
# (chrome/updater/updater.cc in stderr); the default profile works there.
CHROME_PROFILE = tempfile.TemporaryDirectory(prefix="eval-dashboard-chrome-")
CHROME_FLAGS = (
    "--headless",
    "--disable-gpu",
    "--no-sandbox",
    *([] if sys.platform == "darwin" else [f"--user-data-dir={CHROME_PROFILE.name}"]),
)


def chrome_smoke() -> str | None:
    """Why the browser tests must skip, or None when a launch works. A
    Chrome that is installed but cannot start here (a missing library, a
    sandbox rule) is an environment fact, reported as a skip reason rather
    than as thirteen unrelated failures."""
    if not CHROME:
        return "no headless Chrome on this machine"
    try:
        proc = subprocess.run(
            [CHROME, *CHROME_FLAGS, "--dump-dom", "about:blank"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"headless Chrome did not launch: {err}"
    if proc.returncode != 0 or "<html" not in proc.stdout:
        return f"headless Chrome exited {proc.returncode}: {proc.stderr.strip()[-300:]}"
    return None


CHROME_SKIP_REASON = chrome_smoke()

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


def fixture_health(state="OUTAGE", **overrides):
    """What the CI health adjudicator writes beside data.json: an OUTAGE
    that began during run C's day, blaming two of the fixture's cases,
    adjudicated at the data's generated_at. ``evidence`` and ``metrics``
    are present because the writer emits them; the renderer ignores both."""
    health = {
        "state": state,
        "since": "2026-08-31T10:00:00Z",
        "cause": "shared fixture/environment break: case-a, case-e",
        "failing_cases": ["case-a", "case-e"],
        "evidence": ["case-a failed all graded reps on 2 runs from 2 PRs"],
        "advice": "Don't retest yet; the failing cases share a cause.",
        "metrics": {"green_share_24h": 0.4},
        "generated_at": "2026-09-01T12:00:00Z",
    }
    health.update(overrides)
    return health


def render_fixture(
    data, notes_path=None, events_path=None, events_yaml=None, health=None, health_text=None
):
    """Run the real CLI against a temp dir; returns (html, out_dir, tmp).
    Notes, events and health default to *absent* files so the repo's own
    annotation files never leak into a test; pass events_yaml to write one
    inline, health (a dict) or health_text (raw bytes of the file) to add a
    --health input."""
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
    if health is not None or health_text is not None:
        health_path = pathlib.Path(tmp.name) / "health.json"
        health_path.write_text(health_text if health_text is not None else json.dumps(health))
        argv += ["--health", str(health_path)]
    with contextlib.redirect_stdout(io.StringIO()):
        render.main(argv)
    return (out_dir / "index.html").read_text(), out_dir, tmp


def banner_of(app):
    if 'id="healthbanner"' not in app:
        raise AssertionError("no health banner rendered")
    return app.split('<div id="health">', 1)[1].split("</section>", 1)[0]


# --- the headless-browser harness ------------------------------------------

# Injected around the shipped page: the first script captures #app as the
# browser parsed the *baked* HTML; the last compares it with #app after the
# template's own renderAll() replaced it. Both sides are the browser's
# serialization of a parsed DOM, so the comparison is apples to apples. The
# banner's "checked" span is normalized out: the script adds the wall-clock
# age and a STALE/UNREACHABLE label to it by design, exactly as it does to
# the freshness badge, and a separate assertion covers that label.
PROBE_CAPTURE = '<script>window.__baked=document.getElementById("app").innerHTML;</script>'
PROBE_COMPARE = """<script>(function(){
  const strip = (s) => s.replace(/<span class="hfresh[^"]*" id="healthfresh">[^<]*<\\/span>/, "");
  const baked = strip(window.__baked), live = strip(document.getElementById("app").innerHTML);
  let at = 0;
  while (at < baked.length && baked[at] === live[at]) at += 1;
  document.title = JSON.stringify({parity: baked === live, at,
    baked: baked.slice(Math.max(0, at - 80), at + 160), live: live.slice(Math.max(0, at - 80), at + 160)});
})()</script>"""


def probe_page(html):
    """The shipped page with the parity probe spliced in: the capture goes
    in front of the first script *after* #app (so it sees the baked
    fragment and nothing else), the comparison at the end of the body."""
    before, after = html.split('<div id="app">', 1)
    after = after.replace("<script>", PROBE_CAPTURE + "<script>", 1)
    return before + '<div id="app">' + after.replace("</body>", PROBE_COMPARE + "</body>")


def chrome_dump(url):
    """The DOM after the page's script ran, as headless Chrome serializes
    it. A launch failure reports Chrome's own stderr."""
    proc = subprocess.run(
        [CHROME, *CHROME_FLAGS, "--virtual-time-budget=3000", "--dump-dom", url],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"chrome exited {proc.returncode} for {url}:\n{proc.stderr[-2000:]}")
    return proc.stdout


def browser_dom(html, out_dir, query="", fragment=""):
    """The probed page's DOM plus the parity verdict the probe wrote into
    <title>. ``query`` and ``fragment`` are appended to the file:// URL
    verbatim (the browser parses them exactly as it would on the published
    page)."""
    probe_path = out_dir / "probe.html"
    probe_path.write_text(probe_page(html))
    dom = chrome_dump(probe_path.as_uri() + query + fragment)
    title = html_lib.unescape(dom.split("<title>", 1)[1].split("</title>", 1)[0])
    if not title.startswith("{"):
        raise AssertionError(f"the probe script did not run; title is {title!r}")
    return dom, json.loads(title)


def live_app(dom):
    """#app as the script re-rendered it (the serialized DOM, not the baked
    fragment -- attribute quoting and entities are the browser's)."""
    return dom.split('<div id="app">', 1)[1].split("<script>", 1)[0]


def live_mx_row_for(app, case_name):
    rows = [r for r in re.split(r'<div class="mx-row(?: hl| dim)?">', app) if f">{case_name}</span>" in r]
    if not rows:
        raise AssertionError(f"no matrix row for {case_name}")
    return rows[0].split('<div class="mx-ev">')[0].split('<div class="legend')[0]


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
        # The run-level rule is stated verbatim on the gate band and again
        # in the matrix caption.
        self.assertEqual(
            self.app.count("run-level events excluded from per-case stats"), 3
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

    def test_section_anchors_exist_once_and_the_nav_uses_them(self):
        # The deep-link contract names #agent, #gate and #evidence; each id
        # must exist exactly once (a duplicate id makes the fragment land on
        # whichever the browser finds first) and the nav tabs must point at
        # them. #nightly was the evidence heading's old id.
        for anchor in ("agent", "gate", "evidence", "release"):
            self.assertEqual(self.app.count(f'<h2 id="{anchor}">'), 1, anchor)
            self.assertEqual(self.app.count(f'id="{anchor}"'), 1, anchor)
            self.assertIn(f'href="#{anchor}"', self.html)
        self.assertNotIn('id="nightly"', self.html)
        self.assertNotIn('href="#nightly"', self.html)

    def test_no_banner_container_is_empty_without_health(self):
        # The container always renders (the health poll re-renders it
        # alone); without a verdict it is empty and sits first in #app.
        self.assertTrue(self.app.lstrip().startswith('<div id="health"></div>'))
        self.assertNotIn("healthbanner", self.app)

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


class HealthBannerTest(unittest.TestCase):
    """The gate-health banner rendered from health.json (--health)."""

    def render(self, **kwargs):
        html, _, tmp = render_fixture(fixture_data(), **kwargs)
        self.addCleanup(tmp.cleanup)
        return html, baked_app(html)

    def test_banner_renders_for_each_state_above_the_hero(self):
        for state, glyph, cls in (
            ("GREEN", "🟢", "hs-green"),
            ("DEGRADED", "🟡", "hs-amber"),
            ("OUTAGE", "🔴", "hs-red"),
        ):
            _, app = self.render(health=fixture_health(state))
            banner = banner_of(app)
            # The very top of the page: before the hero's h1.
            self.assertLess(app.index('id="healthbanner"'), app.index("<h1>"))
            # Glyph, word and colour class together -- never colour alone.
            self.assertIn(f'<span class="hpill">{glyph} {state}</span>', banner)
            self.assertIn(f'<section class="health {cls}"', banner)
            self.assertIn('role="status"', banner)

    def test_banner_carries_since_cause_advice_and_the_details_link(self):
        _, app = self.render(health=fixture_health())
        banner = banner_of(app)
        # Absolute UTC start plus the duration up to the verdict's own
        # generated_at (09-01 12:00 - 08-31 10:00 = 26h), not the wall clock.
        self.assertIn("since 2026-08-31 10:00 UTC · for 1d 2h", banner)
        self.assertIn('<span class="hfresh" id="healthfresh">checked 12:00 UTC</span>', banner)
        self.assertIn(
            '<div class="hcause">shared fixture/environment break: case-a, case-e</div>', banner
        )
        self.assertIn("Don&#x27;t retest yet; the failing cases share a cause.", banner)
        # The drill-down is the deep link: failing cases + since, then #gate.
        self.assertIn(
            '<a class="hdetails" href="?cases=case-a,case-e&amp;since=2026-08-31T10%3A00%3A00Z#gate">details ↓</a>',
            banner,
        )

    def test_details_link_is_the_bare_anchor_without_cases_or_since(self):
        _, app = self.render(health=fixture_health("GREEN", failing_cases=[], since=None))
        banner = banner_of(app)
        self.assertIn('href="#gate"', banner)
        self.assertIn("since unknown", banner)

    def test_duration_is_floored_and_never_negative(self):
        # A verdict adjudicated before its own since (clock skew in the
        # writer) shows the start but no duration rather than "-1h".
        _, app = self.render(
            health=fixture_health(since="2026-09-01T13:00:00Z", generated_at="2026-09-01T12:00:00Z")
        )
        self.assertIn("since 2026-09-01 13:00 UTC</span>", banner_of(app))
        minute = render.MINUTE_MS
        self.assertEqual(render.duration_text(-5 * minute), "0m")
        self.assertEqual(render.duration_text(59 * minute + 59999), "59m")
        self.assertEqual(render.duration_text((3 * 60 + 12) * minute), "3h 12m")
        self.assertEqual(render.duration_text(26 * 60 * minute), "1d 2h")

    def test_absent_health_means_no_banner_and_a_null_bootstrap(self):
        html, app = self.render()
        self.assertNotIn("healthbanner", app)
        self.assertIn("health: null,", html)
        self.assertIn("Case × run outcome matrix", app)

    def test_malformed_health_means_no_banner_and_the_page_still_renders(self):
        shapes = (
            "{not json",
            "[]",
            '"OUTAGE"',
            "{}",
            '{"state": "PURPLE"}',
            '{"state": 5, "cause": "x"}',
            '{"state": null}',
        )
        for text in shapes:
            html, app = self.render(health_text=text)
            self.assertNotIn("healthbanner", app, text)
            self.assertIn("health: null,", html, text)
            self.assertIn("Case × run outcome matrix", app, text)
        html, app = self.render(health_text="")
        self.assertNotIn("healthbanner", app)

    def test_off_shape_optional_fields_degrade_field_by_field(self):
        # A recognised state with everything else wrong still renders the
        # pill: strings that are not strings drop, cases that are not a list
        # drop, an unparseable since reads "unknown".
        _, app = self.render(
            health={
                "state": "outage",  # case-insensitive
                "since": "yesterday",
                "cause": ["a", "list"],
                "advice": 7,
                "failing_cases": "case-a",
                "generated_at": "2026-09-01T12:00:00Z",
            }
        )
        banner = banner_of(app)
        self.assertIn("🔴 OUTAGE", banner)
        self.assertIn("since unknown", banner)
        self.assertIn('href="#gate"', banner)
        self.assertNotIn("hcause", banner)
        self.assertNotIn("hadvice", banner)
        _, app = self.render(health=fixture_health(failing_cases=["case-a", 3, None, {"x": 1}]))
        self.assertIn('href="?cases=case-a&amp;since=', banner_of(app))

    def test_hostile_health_text_is_escaped_everywhere_it_lands(self):
        html, app = self.render(
            health=fixture_health(
                cause='<img src=x onerror=alert(3)> "quoted"',
                advice="</script><script>alert(4)</script>",
                failing_cases=['"><script>alert(5)</script>', "case-a"],
                since="2026-08-31T10:00:00Z",
            )
        )
        banner = banner_of(app)
        self.assertNotIn("<img src=x", banner)
        self.assertNotIn("<script>", banner)
        self.assertIn("&lt;img src=x onerror=alert(3)&gt; &quot;quoted&quot;", banner)
        # The case id reaches the href percent-encoded and then HTML-escaped.
        self.assertIn(
            'href="?cases=%22%3E%3Cscript%3Ealert(5)%3C%2Fscript%3E,case-a&amp;since=', banner
        )
        # And the bootstrap copy carries no '<' at all.
        self.assertNotIn("<script>alert(4)", html)
        self.assertIn("\\u003c/script>\\u003cscript>alert(4)", html)

    def test_out_of_range_stamps_and_lone_surrogates_never_abort_the_render(self):
        # Both crashed render.main before the adversarial pass: an offset
        # that pushes year 9999 or year 1 out of datetime's range raised
        # OverflowError past parse_iso's ValueError guard, and a lone
        # surrogate (a surrogateescape'd log byte, json.dumps'd as \udc80)
        # raised UnicodeEncodeError in quote() and in write_text().
        _, app = self.render(
            health=fixture_health(
                since="9999-12-31T23:00:00-05:00",
                generated_at="0001-01-01T00:00:00+05:00",
                cause="a\udc80b",
                failing_cases=["x\udc80", "case-a"],
            )
        )
        banner = banner_of(app)
        self.assertIn("since unknown", banner)
        self.assertIn("checked —", banner)
        self.assertIn('<div class="hcause">a�b</div>', banner)
        self.assertIn('href="?cases=x%EF%BF%BD,case-a#gate"', banner)
        self.assertIsNone(render.parse_iso("9999-12-31T23:00:00-05:00"))
        # A real astral character is one code point in Python and a valid
        # pair in JS; only the lone surrogate is replaced on either side.
        self.assertEqual(render.well_formed("😀 \udc80"), "😀 �")

    def test_banner_renders_on_the_empty_state_page_too(self):
        data = {"schema_version": 1, "generated_at": "2026-08-28T14:02:11Z",
                "source": "logs", "runs": [], "cases": []}
        html, _, tmp = render_fixture(data, health=fixture_health("DEGRADED"))
        self.addCleanup(tmp.cleanup)
        app = baked_app(html)
        self.assertIn("🟡 DEGRADED", banner_of(app))
        self.assertIn('id="empty-state"', app)
        self.assertLess(app.index('id="healthbanner"'), app.index('id="empty-state"'))

    def test_health_json_is_not_copied_into_the_out_dir(self):
        # The adjudicator owns that object in the bucket; publishing a copy
        # from the render would overwrite a fresher verdict with this one.
        _, out_dir, tmp = render_fixture(fixture_data(), health=fixture_health())
        self.addCleanup(tmp.cleanup)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["data.json", "index.html"])

    def test_load_health_direct(self):
        self.assertIsNone(render.load_health(None))
        self.assertIsNone(render.load_health(pathlib.Path("/nonexistent/health.json")))
        normalized = render.normalize_health(fixture_health())
        self.assertEqual(normalized["state"], "OUTAGE")
        self.assertEqual(normalized["failing_cases"], ["case-a", "case-e"])
        self.assertFalse(normalized["stale"])
        self.assertNotIn("evidence", normalized)
        self.assertTrue(render.normalize_health(fixture_health(stale=True))["stale"])

    def test_uri_component_mirrors_encode_uri_component(self):
        # encodeURIComponent's unreserved set, byte for byte.
        self.assertEqual(render.uri_component("a-b_c.d~e!f*g'h(i)j"), "a-b_c.d~e!f*g'h(i)j")
        self.assertEqual(render.uri_component("a b,c/d?e#f&g=h"), "a%20b%2Cc%2Fd%3Fe%23f%26g%3Dh")
        self.assertEqual(render.uri_component("é"), "%C3%A9")
        self.assertEqual(render.utc_iso(render.iso_ms("2026-08-31T10:00:00.5+02:00")), "2026-08-31T08:00:00Z")


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

    def test_health_travels_with_the_bootstrap(self):
        html, _, tmp = render_fixture(fixture_data(), health=fixture_health(cause="<b>x</b>"))
        self.addCleanup(tmp.cleanup)
        self.assertIn('health: {"state":"OUTAGE","since":"2026-08-31T10:00:00Z"', html)
        self.assertIn('"cause":"\\u003cb>x\\u003c/b>"', html)

    def test_polls_health_json_beside_data_json_on_the_same_cadence(self):
        js = script_source(self.html)
        self.assertIn(f'healthFile: "{render.HEALTH_FILE}"', js)
        self.assertIn('fetch(DASH.healthFile, { cache: "no-store" })', js)
        self.assertIn("setInterval(refreshHealth, DASH.refreshMs)", js)
        # A file:// preview never fetches, so the poll sits behind the same
        # protocol guard as the data poll.
        guard = js.split('if (location.protocol !== "file:")', 1)[1].split("}", 1)[0]
        self.assertIn("refreshHealth();", guard)

    def test_health_banner_stale_and_unreachable_states_carry_text_labels(self):
        # Same tripwire as the freshness badge: the banner's "checked" stamp
        # is labelled STALE / UNREACHABLE in words, not amber alone, and a
        # failed poll keeps the last verdict rather than blanking the banner.
        js = script_source(self.html)
        self.assertEqual(js.count("`STALE · ${text}`"), 2)
        self.assertEqual(js.count("`UNREACHABLE · ${text}`"), 2)
        self.assertIn(".hfresh.stale", self.html)
        self.assertIn("healthUnreachable = true;", js)
        self.assertIn("health = next;", js)

    def test_js_mirror_carries_the_health_and_deep_link_vocabulary(self):
        # The banner is re-rendered client-side from the template's own
        # mirror, so a state, glyph, class or parameter name that exists
        # only in render.py is invisible on the live page (the #1184 lesson,
        # test_the_js_mirror_carries_the_never_ran_signature).
        js = script_source(self.html)
        self.assertIn('healthStates: ["GREEN", "DEGRADED", "OUTAGE"]', js)
        for state in render.HEALTH_STATES:
            self.assertIn(f'{state}: "{render.HEALTH_GLYPHS[state]}"', js)
            self.assertIn(f'{state}: "{render.HEALTH_CLASSES[state]}"', js)
        self.assertIn(f'healthDetailsAnchor: "{render.HEALTH_DETAILS_ANCHOR}"', js)
        self.assertIn(f'linkParamCases: "{render.LINK_PARAM_CASES}"', js)
        self.assertIn(f'linkParamSince: "{render.LINK_PARAM_SINCE}"', js)
        self.assertIn(f'linkParamUntil: "{render.LINK_PARAM_UNTIL}"', js)
        # Deep links are parsed client-side only, from location.search.
        self.assertIn("new URLSearchParams(location.search)", js)


@unittest.skipIf(CHROME_SKIP_REASON, CHROME_SKIP_REASON)
class BrowserParityTest(unittest.TestCase):
    """The template's script, run for real: renderAll() must reproduce the
    baked page byte for byte (as parsed DOM), and a deep-linked URL must
    produce the highlight, window and Pareto the contract promises."""

    @classmethod
    def setUpClass(cls):
        cls.html, cls.out_dir, cls._tmp = render_fixture(
            fixture_data(), events_yaml=fixture_events_yaml(), health=fixture_health()
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def dom(self, query="", fragment="", html=None, out_dir=None):
        return browser_dom(html or self.html, out_dir or self.out_dir, query, fragment)

    def assert_parity(self, verdict):
        self.assertTrue(
            verdict["parity"],
            f"baked and live #app diverge at {verdict['at']}:\n"
            f"  baked: {verdict['baked']!r}\n  live:  {verdict['live']!r}",
        )

    def test_js_rerender_matches_the_baked_page_with_a_banner(self):
        dom, verdict = self.dom()
        self.assert_parity(verdict)
        # The one deliberate difference: the wall-clock label on "checked".
        # The fixture's verdict is days old, so it must read STALE.
        self.assertRegex(live_app(dom), r'id="healthfresh">STALE · checked 12:00 UTC · \d+m ago<')

    def test_js_rerender_matches_the_baked_page_with_a_space_separated_since(self):
        # fromisoformat accepts "2026-08-31 10:00:00"; bare Date.parse would
        # read that as local time (this machine is not on UTC in CI either
        # way), so the mirror rewrites the space to "T" before the Z rule.
        html, out_dir, tmp = render_fixture(
            fixture_data(), health=fixture_health(since="2026-08-31 10:00:00")
        )
        self.addCleanup(tmp.cleanup)
        _, verdict = self.dom(html=html, out_dir=out_dir)
        self.assert_parity(verdict)
        self.assertIn("since 2026-08-31 10:00 UTC · for 1d 2h", baked_app(html))

    def test_js_rerender_matches_the_baked_page_without_health(self):
        html, out_dir, tmp = render_fixture(fixture_data(), events_yaml=fixture_events_yaml())
        self.addCleanup(tmp.cleanup)
        dom, verdict = self.dom(html=html, out_dir=out_dir)
        self.assert_parity(verdict)
        self.assertIn('<div id="health"></div>', live_app(dom))

    def test_js_rerender_matches_the_baked_empty_state(self):
        data = {"schema_version": 1, "generated_at": "2026-08-28T14:02:11Z",
                "source": "logs", "runs": [], "cases": []}
        html, out_dir, tmp = render_fixture(data, health=fixture_health("GREEN"))
        self.addCleanup(tmp.cleanup)
        _, verdict = self.dom(html=html, out_dir=out_dir)
        self.assert_parity(verdict)

    def test_deep_link_parameters_do_not_change_the_baked_page(self):
        # The server render is parameter-blind: the probe's __baked capture
        # is taken from the same page whatever the URL carries, so parity
        # must fail (the live side highlighted) while the baked side is the
        # plain page.
        dom, verdict = self.dom(query="?cases=case-a")
        self.assertFalse(verdict["parity"])
        # The first divergence is the link note under the gate heading,
        # which the baked side does not have; the highlight follows.
        self.assertIn('id="linknote"', verdict["live"])
        self.assertNotIn("linknote", verdict["baked"])
        self.assertNotIn("linknote", baked_app(self.html))
        self.assertIn('class="mx-row hl"', live_app(dom))

    def test_linked_cases_highlight_and_the_rest_dim(self):
        dom, _ = self.dom(query="?cases=case-a,case-e,nope", fragment="#gate")
        app = live_app(dom)
        self.assertEqual(app.count('class="mx-row hl"'), 2)
        self.assertEqual(app.count('class="mx-row dim"'), 3)
        self.assertIn('<em class="b-link">linked</em>', live_mx_row_for(app, "case-a"))
        self.assertIn('<em class="b-link">linked</em>', live_mx_row_for(app, "case-e"))
        self.assertNotIn("b-link", live_mx_row_for(app, "case-b"))
        # The unknown id is ignored silently: not named, not rendered.
        self.assertNotIn("nope", app)
        self.assertIn("highlighting case-a, case-e", app)
        self.assertIn(">clear</a>", app)
        # No window: the Pareto keeps its rolling caption and no column is marked.
        self.assertIn("Failure signatures · last 7 days", app)
        self.assertNotIn(' win"', app)

    def test_unknown_or_inactive_cases_alone_leave_the_matrix_untouched(self):
        # case-f is on record but inactive: it has no matrix row, so the
        # note must not claim to highlight it either.
        for query in ("?cases=nope,also-nope", "?cases=case-f"):
            dom, _ = self.dom(query=query)
            app = live_app(dom)
            self.assertNotIn(' hl"', app, query)
            self.assertNotIn(' dim"', app, query)
            self.assertNotIn("highlighting", app, query)
            self.assertIn("no linked case has a row in the matrix", app, query)

    def test_window_marks_columns_and_filters_the_pareto(self):
        # Fixture starts: C 08-31T09:00, D 08-31T11:00, E 09-01T08:00,
        # F 09-01T09:00. [08-31T10:00, 09-01T08:30] holds exactly D and E.
        dom, _ = self.dom(query="?since=2026-08-31T10:00:00Z&until=2026-09-01T08:30:00Z")
        app = live_app(dom)
        self.assertEqual(app.count('class="mx-col win"'), 2)
        for case in ("case-a", "case-b", "case-c", "case-d", "case-e"):
            self.assertEqual(live_mx_row_for(app, case).count(' win"'), 2, case)
        self.assertIn("showing 2026-08-31 10:00 → 2026-09-01 08:30 UTC window", app)
        # The Pareto now counts D and E only; D is a run-level event, so E
        # alone: one kanban-columns miss (not the rolling window's three),
        # the 429 group, and none of run C's reason-less infra reps.
        self.assertIn("Failure signatures · linked window", app)
        self.assertIn(
            '<div class="pa-count">1</div><div class="pa-name">exact-check: kanban-columns</div>',
            app,
        )
        self.assertIn("endpoint saturation (infra)", app)
        self.assertNotIn("(infra, no reason recorded)", app)
        self.assertNotIn("EVENT-ONLY-REASON", app)

    def test_since_without_until_is_open_ended(self):
        dom, _ = self.dom(query="?since=2026-09-01T08:30:00Z")
        app = live_app(dom)
        self.assertEqual(app.count('class="mx-col win"'), 1)  # run F only
        self.assertIn("showing 2026-09-01 08:30 UTC → now window", app)

    def test_window_bounds_are_inclusive_and_offsets_are_honoured(self):
        # E starts 09-01T08:00Z and F 09:00Z exactly: both bounds inclusive.
        dom, _ = self.dom(query="?since=2026-09-01T08:00:00Z&until=2026-09-01T09:00:00Z")
        self.assertEqual(live_app(dom).count('class="mx-col win"'), 2)
        # 10:00+02:00 is 08:00Z (E), a naive stamp is UTC, and the note
        # shows the window in UTC whatever offset the link carried.
        dom, _ = self.dom(query="?since=2026-09-01T10:00:00%2B02:00&until=2026-09-01T08:30")
        app = live_app(dom)
        self.assertEqual(app.count('class="mx-col win"'), 1)
        self.assertIn("showing 2026-09-01 08:00–08:30 UTC window", app)

    def test_same_day_window_is_written_once(self):
        dom, _ = self.dom(query="?since=2026-09-01T08:30:00Z&until=2026-09-01T09:30:00Z")
        self.assertIn("showing 2026-09-01 08:30–09:30 UTC window", live_app(dom))

    def test_until_before_since_or_without_since_is_ignored(self):
        dom, _ = self.dom(query="?since=2026-09-01T09:00:00Z&until=2026-08-01T00:00:00Z")
        self.assertIn("UTC → now window", live_app(dom))
        dom, _ = self.dom(query="?until=2026-09-01T09:00:00Z")
        self.assertNotIn('id="linknote"', live_app(dom))

    def test_the_banners_details_link_applies_the_deep_link(self):
        href = html_lib.unescape(
            banner_of(baked_app(self.html)).split('class="hdetails" href="', 1)[1].split('"', 1)[0]
        )
        query, fragment = href.split("#", 1)
        dom, _ = self.dom(query=query, fragment="#" + fragment)
        app = live_app(dom)
        self.assertEqual(app.count('class="mx-row hl"'), 2)
        # since 08-31T10:00 with no until: D, E and F are in the window.
        self.assertEqual(app.count('class="mx-col win"'), 3)

    def test_hostile_parameters_never_reach_the_dom_unescaped(self):
        dom, _ = self.dom(
            query=(
                "?cases=%3Cscript%3Ealert(1)%3C%2Fscript%3E,case-a,%22%20onmouseover%3D%22x"
                "&since=%3Cimg%20src%3Dx%20onerror%3Dalert(2)%3E"
                "&until=2026-09-01T09:00:00Z"
            ),
            fragment="#%22%3E%3Cb%3Eboom",
        )
        app = live_app(dom)
        self.assertNotIn("<script>alert(1)", dom)
        self.assertNotIn("<img src=x", dom)
        self.assertNotIn("onmouseover", app)
        self.assertNotIn("<b>boom", dom)
        # The one well-formed id still works; the malformed since is dropped
        # (no window), so until is dropped with it.
        self.assertEqual(app.count('class="mx-row hl"'), 1)
        self.assertNotIn("window", app.split('id="linknote"', 1)[1].split("</div>", 1)[0])
        self.assertNotIn(' win"', app)
        # The clear link carries the path only: the fragment was not a bare
        # section anchor.
        note = app.split('id="linknote"', 1)[1].split("</div>", 1)[0]
        self.assertRegex(note, r'<a href="[^"#<>]*probe\.html">clear</a>')

    def test_case_id_grammar_bounds_length_and_count(self):
        too_long = "x" * 81
        many = ",".join(f"c{i}" for i in range(60)) + ",case-a"
        dom, _ = self.dom(query=f"?cases={too_long},case-b")
        self.assertEqual(live_app(dom).count('class="mx-row hl"'), 1)
        # case-a sits past the 50-id cap and is not honoured.
        dom, _ = self.dom(query=f"?cases={many}")
        self.assertNotIn(' hl"', live_app(dom))

    def test_nav_tab_follows_the_fragment(self):
        # Attribute order in the serialized DOM is whatever the tag started
        # with, so match the tab by its href rather than a literal tag.
        def on_tabs(dom):
            nav = dom.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
            return [t for t in re.findall(r"<a [^>]*>", nav) if 'class="on"' in t]

        for fragment in ("#gate", "#evidence"):
            tabs = on_tabs(self.dom(fragment=fragment)[0])
            self.assertEqual(len(tabs), 1, fragment)
            self.assertIn(f'href="{fragment}"', tabs[0])
        # No fragment, or one that names no tab: the first tab stays on
        # rather than every tab going dark.
        for fragment in ("", "#healthbanner"):
            tabs = on_tabs(self.dom(fragment=fragment)[0])
            self.assertEqual(len(tabs), 1, fragment)
            self.assertIn('href="#agent"', tabs[0])


@unittest.skipIf(CHROME_SKIP_REASON, CHROME_SKIP_REASON)
class BrowserPollTest(unittest.TestCase):
    """The 60 s health.json poll over a real HTTP server: the page is baked
    with one verdict and the server answers with another (or nothing, or
    garbage), and the banner must follow the fail-safe rules the string
    tripwires above only pin as source text."""

    @classmethod
    def setUpClass(cls):
        cls.html, cls.out_dir, cls._tmp = render_fixture(
            fixture_data(), events_yaml=fixture_events_yaml(), health=fixture_health("GREEN")
        )
        cls.www = pathlib.Path(cls._tmp.name) / "www"
        cls.www.mkdir()
        shutil.copyfile(cls.out_dir / "index.html", cls.www / "index.html")
        shutil.copyfile(cls.out_dir / "data.json", cls.www / "data.json")

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(Quiet, directory=str(cls.www))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def serve_health(self, text):
        path = self.www / "health.json"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)
        self.addCleanup(path.unlink, missing_ok=True)

    def banner(self, dom):
        app = live_app(dom)
        return app.split('<div id="health">', 1)[1].split("</div>", 1)[0]

    def freshness(self, dom):
        return dom.split('id="freshness"', 1)[1].split("</span>", 1)[0]

    def test_a_polled_verdict_replaces_the_baked_one(self):
        self.serve_health(json.dumps(fixture_health("OUTAGE")))
        dom = chrome_dump(self.base + "index.html")
        banner = self.banner(dom)
        self.assertIn("🔴 OUTAGE", banner)
        self.assertNotIn("UNREACHABLE", banner)
        self.assertIn("checked 12:00 UTC", banner)
        # The data poll succeeded too: its badge is STALE (old fixture), not
        # UNREACHABLE.
        self.assertNotIn("UNREACHABLE", self.freshness(dom))
        self.assertIn("STALE · updated 12:00 UTC", self.freshness(dom))

    def test_a_missing_or_malformed_health_json_keeps_the_last_verdict(self):
        for text in (None, '{"state": "PURPLE"}', "{not json"):
            self.serve_health(text)
            dom = chrome_dump(self.base + "index.html")
            banner = self.banner(dom)
            self.assertIn("🟢 GREEN", banner, text)
            self.assertIn("UNREACHABLE · checked 12:00 UTC", banner, text)
            self.assertNotIn("UNREACHABLE", self.freshness(dom), text)

    def test_a_poisoned_verdict_is_not_committed_and_the_data_feed_stays_up(self):
        # A lone surrogate in failing_cases made encodeURIComponent throw
        # after the verdict had been committed; every later data re-render
        # then threw too and the badge blamed data.json. The escaped form
        # below is what json.dumps writes for such a string.
        self.serve_health('{"state":"OUTAGE","failing_cases":["case-a","\\udc80"],'
                          '"since":"2026-08-31T10:00:00Z","generated_at":"2026-09-01T12:00:00Z"}')
        dom = chrome_dump(self.base + "index.html")
        banner = self.banner(dom)
        # wellFormed() now makes it renderable, so the verdict is taken...
        self.assertIn("🔴 OUTAGE", banner)
        self.assertIn("cases=case-a,%EF%BF%BD", banner)
        self.assertNotIn("UNREACHABLE", banner)
        # ...and the data feed's badge is untouched either way.
        self.assertNotIn("UNREACHABLE", self.freshness(dom))
        self.assertIn("Case × run outcome matrix", live_app(dom))

    def test_stale_flag_from_the_writer_labels_the_banner(self):
        self.serve_health(json.dumps(fixture_health("DEGRADED", stale=True)))
        dom = chrome_dump(self.base + "index.html")
        banner = self.banner(dom)
        self.assertIn("🟡 DEGRADED", banner)
        self.assertIn("STALE · checked 12:00 UTC", banner)


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
