"""The Brief and the PR view: render.py's brief.json, the optional health
inputs, and -- when headless Chrome is present -- the pages as a browser
renders them from that document.

The page tests run the shipped script for real (Chrome is present on
ubuntu-latest and skipped with a reason elsewhere): the Brief in each state
the design covers (OUTAGE, DEGRADED storm, DEGRADED setup deaths,
RECOVERING, HEALTHY, a PAST incident opened through the URL) and the PR
view in its three verdicts, plus the not-found page. Every asserted time is
America/Toronto.
"""

import contextlib
import gzip
import io
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from datetime import timezone

from eval_dashboard import render

FIXTURE = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_classify" / "incidents.json.gz"
PAGES_JS = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "template" / "pages.js"
CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
UTC = timezone.utc
CRASHLOOP_TRIO = [
    "cluster-agent-crashloop-debug",
    "cluster-agent-crashloop-evidence-chain",
    "cluster-agent-crashloop-misleading-symptom",
]
# 2026-09-08 12:00Z is 08:00 ET (EDT); the outage began 09-08 09:00Z = 5:00 AM ET.
NOW = "2026-09-08T14:30:00+00:00"
OUTAGE_SINCE = "2026-09-08T09:00:00+00:00"


def health_doc(state="OUTAGE", **overrides):
    doc = {
        "schema_version": 1, "state": state,
        "condition": "shared_break" if state == "OUTAGE" else None,
        "since": OUTAGE_SINCE if state != "GREEN" else "2026-09-06T01:00:00+00:00",
        "cause": "shared fixture/environment break: " + ", ".join(CRASHLOOP_TRIO) if state == "OUTAGE" else "",
        "failing_cases": list(CRASHLOOP_TRIO) if state == "OUTAGE" else [],
        "tracking_issues": ["#1278"] if state == "OUTAGE" else [],
        "incident": {"prs": [1275, 1246], "runs": 6, "window_start": None, "window_end": None},
        "evidence": [], "advice": "Don't retest yet; the failing cases share a cause. Tracking: #1278",
        "recovering": False, "stale": False,
        "metrics": {"window_hours": 24, "full_runs": 10},
        "generated_at": NOW,
    }
    doc.update(overrides)
    return doc


def history_lines(*docs):
    return "\n".join(json.dumps(d) for d in docs) + "\n"


def load_fixture():
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        path = shutil.which(candidate) or (candidate if pathlib.Path(candidate).exists() else None)
        if path:
            return path
    return None


def render_to(tmp, data, health=None, history=None, extra_args=()):
    """Run the CLI; returns the out-dir."""
    root = pathlib.Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    (root / "data.json").write_text(json.dumps(data))
    # --repo-root points at a directory that is not a git checkout, so the
    # merges block depends on the test, not on where the suite runs.
    argv = ["--data", str(root / "data.json"), "--out-dir", str(root / "out"), "--repo-root", str(root),
            "--notes", str(root / "no-notes.yaml"), "--events", str(root / "no-events.yaml")]
    if health is not None:
        (root / "health.json").write_text(json.dumps(health))
        argv += ["--health", str(root / "health.json")]
    if history is not None:
        (root / "health-history.jsonl").write_text(history)
        argv += ["--health-history", str(root / "health-history.jsonl")]
    argv += list(extra_args)
    with contextlib.redirect_stdout(io.StringIO()):
        render.main(argv)
    return root / "out"


def dom_text(page: pathlib.Path, query: str = "", fragment: str = "") -> str:
    """The page's #app innerHTML after the script ran, via headless Chrome."""
    url = page.as_uri() + (f"?{query}" if query else "") + fragment
    result = subprocess.run(
        [chrome(), "--headless", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=3000", "--dump-dom", url],
        capture_output=True, text=True, timeout=90, check=False,
    )
    html = result.stdout
    start = html.find('<div id="app">')
    # Slice up to the page's own inline script (its first comment line), not
    # the first <script> tag: an injected tag inside #app must stay visible.
    end = html.find("<script>\n/* The Brief", start)
    return html[start:end]


class HealthInputsTest(unittest.TestCase):
    def test_normalize_defaults_every_field_and_rejects_unknown_states(self):
        self.assertIsNone(render.normalize_health(None))
        self.assertIsNone(render.normalize_health([]))
        self.assertIsNone(render.normalize_health({"state": "PURPLE"}))
        minimal = render.normalize_health({"state": "green"})
        self.assertEqual(minimal["state"], "GREEN")
        self.assertEqual(minimal["failing_cases"], [])
        self.assertIsNone(minimal["since"])
        self.assertFalse(minimal["recovering"])
        full = render.normalize_health(health_doc(failing_cases=["a", 3, None], since="not a time"))
        self.assertEqual(full["failing_cases"], ["a"])
        self.assertIsNone(full["since"])
        self.assertEqual(full["tracking_issues"], ["#1278"])
        self.assertEqual(full["incident"]["prs"], [1275, 1246])

    def test_load_health_degrades_on_absent_or_broken_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "health.json"
            self.assertIsNone(render.load_health(path))
            path.write_text("{not json")
            self.assertIsNone(render.load_health(path))
            path.write_text(json.dumps(health_doc()))
            self.assertEqual(render.load_health(path)["state"], "OUTAGE")

    def test_history_reader_skips_bad_lines_and_sorts_by_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "health-history.jsonl"
            self.assertIsNone(render.load_health_history(path), "absent means current state only")
            later = dict(health_doc("GREEN"), tick="2026-09-08T15:00:00+00:00")
            earlier = dict(health_doc(), tick="2026-09-08T09:00:00+00:00")
            no_tick = dict(health_doc(), generated_at="2026-09-08T12:00:00+00:00")
            path.write_text(json.dumps(later) + "\n\n{broken\n" + json.dumps({"state": "MAUVE", "tick": "2026-09-08T10:00:00+00:00"}) + "\n" + json.dumps(no_tick) + "\n" + json.dumps(earlier) + "\n")
            ticks = render.load_health_history(path)
            self.assertEqual([t["tick"] for t in ticks], ["2026-09-08T09:00:00+00:00", "2026-09-08T12:00:00+00:00", "2026-09-08T15:00:00+00:00"])

    def test_incidents_are_runs_of_non_green_ticks(self):
        docs = [
            dict(health_doc("GREEN"), tick="2026-09-07T20:00:00+00:00"),
            dict(health_doc(), tick="2026-09-08T09:00:00+00:00"),
            dict(health_doc(failing_cases=CRASHLOOP_TRIO + ["agent-kanban-smoke"]), tick="2026-09-08T10:00:00+00:00"),
            dict(health_doc("DEGRADED", condition="storm", failing_cases=[]), tick="2026-09-08T11:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-08T12:00:00+00:00"),
            dict(health_doc("DEGRADED", condition="setup_deaths", failing_cases=[], since="2026-09-08T13:00:00+00:00"), tick="2026-09-08T13:00:00+00:00"),
        ]
        normalized = [render.normalize_health(d) for d in docs]
        incidents = render.history_incidents(normalized)
        self.assertEqual(len(incidents), 2)
        first, second = incidents
        self.assertEqual((first["since"], first["until"], first["state"]), (OUTAGE_SINCE, "2026-09-08T12:00:00+00:00", "OUTAGE"))
        self.assertEqual(first["failing_cases"], CRASHLOOP_TRIO + ["agent-kanban-smoke"], "the union over the episode")
        self.assertEqual(first["condition"], "storm", "the last condition reported")
        self.assertEqual(first["tracking_issues"], ["#1278"])
        self.assertEqual((second["since"], second["until"], second["condition"]), ("2026-09-08T13:00:00+00:00", None, "setup_deaths"))

    def test_health_at_picks_the_tick_in_force(self):
        docs = [
            dict(health_doc("GREEN"), tick="2026-09-08T08:00:00+00:00"),
            dict(health_doc(), tick="2026-09-08T09:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-08T12:00:00+00:00"),
        ]
        ticks = [render.normalize_health(d) for d in docs]
        incidents = render.history_incidents(ticks)
        ms = render.iso_ms
        self.assertIsNone(render.health_at(ticks, ms("2026-09-08T07:00:00+00:00"), incidents), "before the first tick")
        at = render.health_at(ticks, ms("2026-09-08T10:30:00+00:00"), incidents)
        self.assertEqual((at["state"], at["since"], at["until"]), ("OUTAGE", OUTAGE_SINCE, "2026-09-08T12:00:00+00:00"))
        self.assertEqual(render.health_at(ticks, ms("2026-09-08T12:20:00+00:00"), incidents)["state"], "GREEN")
        self.assertIsNone(render.health_at(ticks, ms("2026-09-08T13:00:00+00:00"), incidents), "past the last tick's slack: the current verdict applies instead")
        self.assertIsNone(render.health_at(None, ms(NOW)))


class MergesTest(unittest.TestCase):
    def test_a_shallow_checkout_or_a_failing_git_omits_the_block(self):
        def shallow(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
        self.assertIsNone(render.recent_merges(pathlib.Path("."), render.iso_ms(NOW), runner=shallow))

        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal")
        self.assertIsNone(render.recent_merges(pathlib.Path("."), render.iso_ms(NOW), runner=failing))

        def raising(argv, **kwargs):
            raise OSError("no git")
        self.assertIsNone(render.recent_merges(pathlib.Path("."), render.iso_ms(NOW), runner=raising))
        self.assertIsNone(render.recent_merges(pathlib.Path("."), None))

    def test_the_log_is_parsed_into_pr_numbers(self):
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="false\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout=(
                "abc123\x1f2026-09-08T08:10:00+00:00\x1ffix(ci): the thing (#1280)\n"
                "def456\x1f2026-09-08T07:00:00+00:00\x1fdocs: no pr number\n"
                "garbage line\n"
            ), stderr="")
        merges = render.recent_merges(pathlib.Path("/repo"), render.iso_ms(NOW), runner=fake)
        self.assertEqual(merges, [
            {"sha": "abc123", "at": "2026-09-08T08:10:00+00:00", "title": "fix(ci): the thing", "pr": 1280},
            {"sha": "def456", "at": "2026-09-08T07:00:00+00:00", "title": "docs: no pr number", "pr": None},
        ])
        self.assertIn("--first-parent", calls[1])
        self.assertTrue(any(a.startswith("--since=2026-09-05T14:30:00") for a in calls[1]))


class BriefDocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_fixture()
        cls.data["generated_at"] = NOW
        cls.data["cases"] = [{"name": n, "active": True} for n in CRASHLOOP_TRIO]

    def test_the_document_shape_and_the_window(self):
        health = render.normalize_health(health_doc())
        brief = render.brief_document(self.data, health, None, None)
        self.assertEqual(brief["run_days"], render.RUN_VIEW_DAYS)
        self.assertEqual(brief["health"]["state"], "OUTAGE")
        self.assertIsNone(brief["history"])
        self.assertIsNone(brief["merges"])
        self.assertIn("cluster-agent-crashloop-debug", brief["admitted"])
        builds = [r["build"] for r in brief["runs"]]
        self.assertIn("2097282860221206528", builds)
        run_1275 = next(r for r in brief["runs"] if r["build"] == "2097282860221206528")
        self.assertEqual(run_1275["verdict"], "infra")
        self.assertTrue(run_1275["matches_incident"], "classified against the current verdict when there is no history")
        self.assertIsNone(run_1275["health_at"])
        self.assertEqual({c["cls"] for c in run_1275["cases"] if c["case"] in CRASHLOOP_TRIO}, {"shared"})
        for key in ("pr", "head_sha", "project", "started", "finished", "duration_s", "result", "headline", "lede", "cases", "setup_death", "storm_reps"):
            self.assertIn(key, run_1275)

    def test_runs_older_than_the_window_are_left_out(self):
        data = dict(self.data, generated_at="2026-09-30T00:00:00+00:00")
        brief = render.brief_document(data, None, None, None)
        self.assertEqual(brief["runs"], [])

    def test_history_gives_each_run_the_verdict_of_its_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "h.jsonl"
            path.write_text(history_lines(
                dict(health_doc("GREEN"), tick="2026-09-07T20:00:00+00:00"),
                dict(health_doc(), tick="2026-09-08T09:00:00+00:00"),
                dict(health_doc(), tick=NOW),
            ))
            ticks = render.load_health_history(path)
        brief = render.brief_document(self.data, render.normalize_health(health_doc()), ticks, [])
        self.assertEqual(len(brief["history"]["incidents"]), 1)
        self.assertEqual(brief["history"]["ticks"][0]["state"], "GREEN")
        run_1275 = next(r for r in brief["runs"] if r["build"] == "2097282860221206528")  # finished 09-08 13:28Z
        self.assertEqual(run_1275["health_at"]["state"], "OUTAGE")
        early = next(r for r in brief["runs"] if r["build"] == "2097160163919138816")  # #1246, finished 09-08 05:12Z
        self.assertEqual(early["health_at"]["state"], "GREEN")
        before = next(r for r in brief["runs"] if r["build"] == "2096888100671197184")  # #1200, finished 09-07 11:30Z, before the history began
        self.assertIsNone(before["health_at"])
        self.assertFalse(before["matches_incident"], "today's outage says nothing about a run that predates the history")
        self.assertEqual(brief["merges"], [])


class RenderedFilesTest(unittest.TestCase):
    def test_all_pages_and_data_files_are_written(self):
        data = load_fixture()
        data["generated_at"] = NOW
        with tempfile.TemporaryDirectory() as tmp:
            out = render_to(tmp, data, health=health_doc())
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(names, ["brief.json", "data.json", "index.html", "legacy.html", "run.html"])
            brief = json.loads((out / "brief.json").read_text())
            self.assertEqual(brief["health"]["state"], "OUTAGE")
            index = (out / "index.html").read_text()
            self.assertIn('data-page="brief"', index)
            self.assertIn('timeZone: PAGE.tz', index.replace("Object.assign({ timeZone: PAGE.tz }", "timeZone: PAGE.tz"))
            self.assertIn('tz: "America/Toronto"', index)
            self.assertIn("brief:", index)
            self.assertIn("\\u003c", render.bootstrap_json({"x": "<script>"}))
            self.assertNotIn("__BRIEF_JSON__", index)
            self.assertNotIn("__PAGES_JS__", index)
            run_page = (out / "run.html").read_text()
            self.assertIn('data-page="run"', run_page)
            legacy = (out / "legacy.html").read_text()
            self.assertIn('href="index.html">Brief</a>', legacy)
            self.assertIn('id="agent"', legacy)
            self.assertFalse((out / "health.json").exists(), "the adjudicator owns health.json")

    def test_hostile_data_never_escapes_the_script_block(self):
        data = load_fixture()
        data["generated_at"] = NOW
        victim = next(r for r in data["runs"] if r["tasks"] and r["tasks"][0].get("reps"))
        victim["project"] = "</script><script>alert(1)</script>"
        victim["tasks"][0]["reps"][0]["reason"] = "<img src=x onerror=alert(1)>"
        with tempfile.TemporaryDirectory() as tmp:
            out = render_to(tmp, data)
            index = (out / "index.html").read_text()
            self.assertNotIn("</script><script>alert", index)
            self.assertNotIn("<img src=x", index)

    def test_pages_js_carries_the_url_contract_and_the_vocabulary(self):
        script = PAGES_JS.read_text()
        for token in ('params.get("cases")', 'params.get("since")', 'params.get("until")', 'params.get("build")', "#agent", "#gate",
                      "shared_break", "storm", "setup_deaths", "only-this-pr", "run.html?build=", "legacy.html", "health.json", "brief.json"):
            self.assertIn(token, script)
        self.assertEqual(script.count("new Intl.DateTimeFormat"), 1, "one place a time becomes text")
        self.assertNotIn("toISOString().slice(11, 16)", script, "no UTC clock text on the new pages")


@unittest.skipUnless(chrome(), "headless Chrome not found")
class BrowserTest(unittest.TestCase):
    """The pages as a browser renders them. Data: the real fixture week with
    generated_at pinned to 2026-09-08 14:30Z; the verdicts are what #1305's
    adjudicator published at those ticks, reduced to the fields read."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = load_fixture()
        data["generated_at"] = NOW
        data["cases"] = [{"name": n, "active": True} for n in CRASHLOOP_TRIO]
        cls.data = data
        history = history_lines(
            dict(health_doc("GREEN"), tick="2026-09-05T00:00:00+00:00", since="2026-09-04T23:30:00+00:00"),
            dict(health_doc("DEGRADED", condition="setup_deaths", failing_cases=[], since="2026-09-05T13:00:00+00:00", tracking_issues=[]), tick="2026-09-05T13:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-06T01:00:00+00:00", since="2026-09-06T01:00:00+00:00"),
            dict(health_doc(since="2026-09-07T14:00:00+00:00"), tick="2026-09-07T14:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-08T01:00:00+00:00", since="2026-09-08T01:00:00+00:00"),
            dict(health_doc(), tick="2026-09-08T09:00:00+00:00"),
            dict(health_doc(), tick=NOW),
        )
        cls.out = render_to(cls.tmp.name, data, health=health_doc(), history=history)
        cls.index = cls.out / "index.html"
        cls.run_page = cls.out / "run.html"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render_state(self, health, sub="s"):
        out = render_to(pathlib.Path(self.tmp.name) / sub, self.data, health=health)
        return dom_text(out / "index.html")

    def test_outage_brief(self):
        app = dom_text(self.index)
        self.assertIn("OUTAGE · since Tue 5:00 AM ET", app)
        self.assertIn("3 gate cases fail on every PR", app)
        self.assertIn("Why we think it's the environment, not a PR", app)
        self.assertIn("unrelated PR", app)
        self.assertIn("What the agent saw", app)
        self.assertIn("rca-names-the-oom", app)
        self.assertIn("Tracking <a", app)
        self.assertIn("issues/1278", app)
        self.assertIn("Runs in this window", app)
        self.assertIn("run.html?build=2097282860221206528", app)
        self.assertIn('class="chip hit">cluster-agent-crashloop-debug', app)
        self.assertNotIn("What changed right before", app, "no merges were given, so the block is omitted")
        self.assertIn("legacy.html", app)
        self.assertNotIn(" UTC", app.split("legacy.html")[0], "no UTC clock text above the footer")

    def test_storm_brief(self):
        app = self.render_state(health_doc("DEGRADED", condition="storm", failing_cases=[], tracking_issues=[],
                                           since="2026-09-08T12:00:00+00:00",
                                           incident={"prs": [1, 2, 3], "runs": 3, "window_start": "2026-09-08T12:00:00+00:00", "window_end": "2026-09-08T14:00:00+00:00"}), "storm")
        self.assertIn("DEGRADED · since Tue 8:00 AM ET", app)
        self.assertIn("repetitions are being lost to API quota", app)
        self.assertIn("Why we think it's a quota storm", app)
        self.assertIn("Retest after Tue 10:30 AM ET", app)

    def test_setup_deaths_brief(self):
        app = self.render_state(health_doc("DEGRADED", condition="setup_deaths", failing_cases=[], tracking_issues=[], since="2026-09-07T15:00:00+00:00"), "setup")
        self.assertIn("Runs are dying before any case runs", app)
        self.assertIn("Why we think it's the setup, not the PRs", app)
        self.assertIn("died within 5 minutes", app)

    def test_recovering_brief(self):
        app = self.render_state(health_doc(recovering=True), "rec")
        self.assertIn("RECOVERING · since Tue 5:00 AM ET", app)
        self.assertIn("The condition has cleared", app)
        self.assertIn("of 3 clean runs on distinct PRs", app)

    def test_healthy_brief_with_the_last_incident_from_history(self):
        out = render_to(pathlib.Path(self.tmp.name) / "green", self.data, health=health_doc("GREEN"), history=history_lines(
            dict(health_doc(since="2026-09-07T14:00:00+00:00"), tick="2026-09-07T14:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-08T01:00:00+00:00"),
        ))
        app = dom_text(out / "index.html")
        self.assertIn("Smoke gate is healthy", app)
        self.assertIn("Last 24 hours", app)
        self.assertIn("Last incident", app)
        self.assertIn("PAST OUTAGE", app)
        self.assertIn("Mon 10:00 AM – 9:00 PM ET", app)
        self.assertIn("index.html?cases=cluster-agent-crashloop-debug", app)

    def test_healthy_brief_without_any_health_files(self):
        out = render_to(pathlib.Path(self.tmp.name) / "nohealth", self.data)
        app = dom_text(out / "index.html")
        self.assertNotIn("Smoke gate is healthy", app, "the runs alone cannot declare the gate healthy")
        self.assertIn("NO VERDICT", app)
        self.assertIn("No gate verdict is published", app)
        self.assertIn("Last 24 hours", app)
        self.assertIn("No incident history is published yet", app)

    def test_past_incident_through_the_url(self):
        query = urllib.parse.urlencode({"cases": ",".join(CRASHLOOP_TRIO), "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z"})
        app = dom_text(self.index, query=query, fragment="#gate")
        self.assertIn("PAST OUTAGE · Mon 10:00 AM – 9:00 PM ET", app)
        self.assertIn("3 gate cases failed on", app)
        self.assertIn("This incident is over", app)
        self.assertIn("run.html?build=2096999509014876160", app, "#1195, red inside that window")
        self.assertNotIn("run.html?build=2097282860221206528", app, "#1275 ran the next morning")

    def test_past_incident_without_history_is_described_by_the_parameters(self):
        out = render_to(pathlib.Path(self.tmp.name) / "nohist", self.data, health=health_doc("GREEN"))
        query = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z"})
        app = dom_text(out / "index.html", query=query)
        self.assertIn("PAST INCIDENT", app)
        self.assertIn("1 gate case failed on", app)

    def test_agent_fragment_shows_the_numbers(self):
        app = dom_text(self.index, fragment="#agent")
        self.assertIn("The last 24 hours in numbers", app)
        self.assertIn('id="agent"', app)

    def test_hostile_parameters_never_reach_the_dom(self):
        app = dom_text(self.index, query="cases=%3Cimg%20src%3Dx%3E&since=%3Cscript%3E")
        self.assertNotIn("<img", app)
        self.assertNotIn("<script", app)
        self.assertIn("OUTAGE", app, "the bad parameters were dropped and the page still rendered")
        app = dom_text(self.run_page, query="build=%3Cb%3E1%3C%2Fb%3E")
        self.assertNotIn("<b>1", app)
        self.assertIn("Which run?", app)

    def test_a_space_separated_since_is_read_as_utc(self):
        query = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07 14:00:00", "until": "2026-09-08 01:00:00"})
        app = dom_text(self.index, query=query)
        self.assertIn("Mon 10:00 AM – 9:00 PM ET", app)

    def test_the_current_outage_opened_through_its_own_link_is_still_live(self):
        query = urllib.parse.urlencode({"cases": ",".join(CRASHLOOP_TRIO), "since": "2026-09-08T09:00:00Z"})
        for label, page in (("with history", self.index), ("without history", render_to(pathlib.Path(self.tmp.name) / "nohist-live", self.data, health=health_doc()) / "index.html")):
            app = dom_text(page, query=query, fragment="#gate")
            self.assertIn("OUTAGE · since Tue 5:00 AM ET", app, label)
            self.assertNotIn("PAST", app, label)
            self.assertNotIn("This incident is over", app, label)
            self.assertIn("What's being done", app, label)
            self.assertIn("issues/1278", app, label)
        # The PR view's own banner link, followed, lands on the live brief.
        run_app = dom_text(render_to(pathlib.Path(self.tmp.name) / "nohist-live", self.data, health=health_doc()) / "run.html", query="build=2097282860221206528")
        self.assertIn("index.html?cases=cluster-agent-crashloop-debug", run_app)
        self.assertIn("since=2026-09-08T09%3A00%3A00Z", run_app)

    def test_pr_view_outage_run(self):
        app = dom_text(self.run_page, query="build=2097282860221206528")
        self.assertIn("Smoke run for <a", app)
        self.assertIn("PR #1275", app)
        self.assertIn("started Tue 7:16 AM ET", app)
        self.assertIn("finished 9:28 AM ET", app)
        self.assertIn("3 of 10 gate cases failed. None of them look like your PR.", app)
        self.assertIn("Gate outage at the time of this run", app)
        self.assertIn("since Tue 5:00 AM ET", app)
        self.assertIn('class="tag shared">failing on 4 other PRs', app)
        self.assertIn("rca-names-the-oom", app)
        self.assertIn("passed", app)
        self.assertIn("artifacts/eval_cluster-agent-crashloop-debug_rep1.log", app)
        self.assertIn("oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/1275/pull-kube-agents-smoke-test/2097282860221206528", app)
        self.assertIn("Nothing right now.", app)
        self.assertIn("held out", app)

    def test_pr_view_run_with_an_unexplained_failure(self):
        app = dom_text(self.run_page, query="build=2097253644305960960")
        self.assertIn("3 of 4 failures match the outage; 1 is unexplained so far.", app)
        self.assertIn('class="tag unclear">unexplained', app)
        self.assertIn("Fix the PR.", app)

    def test_pr_view_green_and_setup_death(self):
        app = dom_text(self.run_page, query="build=2096047888260927488")
        self.assertIn("All 10 gate cases passed.", app)
        self.assertIn("This run is green", app)
        app = dom_text(self.run_page, query="build=2096985236955992064")
        self.assertIn("died during setup", app)
        self.assertIn("Retest.", app)

    def test_pr_view_unknown_build(self):
        app = dom_text(self.run_page, query="build=1")
        self.assertIn(f"No run with that id in the last {render.RUN_VIEW_DAYS} days.", app)
        app = dom_text(self.run_page)
        self.assertIn("Which run?", app)


if __name__ == "__main__":
    unittest.main()
