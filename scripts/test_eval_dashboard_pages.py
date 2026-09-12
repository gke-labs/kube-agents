"""The Brief and the PR view: render.py's brief.json, the optional health
inputs, and -- when headless Chrome is present -- the pages as a browser
renders them from that document.

The page tests run the shipped script for real (Chrome is present on
ubuntu-latest and skipped with a reason elsewhere): the Brief in each state
the design covers (OUTAGE, DEGRADED storm, DEGRADED setup deaths,
RECOVERING, HEALTHY, a PAST incident opened through the URL) and the PR
view in its three verdicts, plus the not-found page; the Cases page's sorts,
filters and pills; the Grid's window, deep link, markers and the detail a
cell click opens. Every asserted time is America/Toronto.
"""

import contextlib
import gzip
import html
import io
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
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


def strict_date_parse_page(page: pathlib.Path) -> pathlib.Path:
    """A copy of the rendered page whose Date.parse rejects a no-colon
    ±HHMM offset. V8 (Chrome, the only engine these tests drive) accepts
    one; ECMA-262's date-time format does not, and the engines that follow
    it return NaN. The copy stands in for those engines."""
    shim = ('<script>(() => { const native = Date.parse; '
            'Date.parse = (text) => (/[+-]\\d{4}$/.test(String(text)) ? NaN : native(text)); })();</script>')
    copy = page.with_name(page.stem + "-strict" + page.suffix)
    copy.write_text(page.read_text().replace("<head>", "<head>" + shim, 1))
    return copy


def dom_html(page: pathlib.Path, query: str = "", fragment: str = "", budget_ms: int = 3000) -> str:
    """The whole document after the script ran, via headless Chrome. From
    file:// every fetch fails, which is the condition a host that answers
    an XHR with a login redirect puts the pages in. ``budget_ms`` is the
    virtual time the page is given; timers fire inside it, so a budget past
    PAGE.refreshMs runs the poll too."""
    url = page.as_uri() + (f"?{query}" if query else "") + fragment
    result = subprocess.run(
        [chrome(), "--headless", "--disable-gpu", "--no-sandbox", f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
        capture_output=True, text=True, timeout=90, check=False,
    )
    return result.stdout


def scroll_counting_page(page: pathlib.Path) -> pathlib.Path:
    """A copy of the rendered page whose scrollIntoView records each call
    on <body data-scrolls>, which --dump-dom serialises."""
    shim = ("<script>Element.prototype.scrollIntoView = function () {"
            " document.body.dataset.scrolls = String(Number(document.body.dataset.scrolls || 0) + 1); };</script>")
    copy = page.with_name(page.stem + "-scrolls" + page.suffix)
    copy.write_text(page.read_text().replace("<head>", "<head>" + shim, 1))
    return copy


def dom_text(page: pathlib.Path, query: str = "", fragment: str = "") -> str:
    """The page's #app innerHTML after the script ran."""
    html = dom_html(page, query, fragment)
    start = html.find('<div id="app">')
    # Slice up to the page's own inline script (its first comment line), not
    # the first <script> tag: an injected tag inside #app must stay visible.
    end = html.find("<script>\n/* The Brief", start)
    return html[start:end]


def freshness_badge(html: str) -> tuple[str, str]:
    """(class, text) of the header's freshness badge in a rendered document."""
    match = re.search(r'<span id="freshness" class="([^"]*)">([^<]*)</span>', html)
    assert match, "no freshness badge in the document"
    return match.group(1), match.group(2)


def clock_page(page: pathlib.Path, now_iso: str) -> pathlib.Path:
    """A copy of the rendered page whose wall clock is pinned to
    ``now_iso``, so the badge's age and staleness are the test's, not the
    day the suite happens to run."""
    ms = int(render.iso_ms(now_iso))
    shim = f"<script>Date.now = () => {ms};</script>"
    copy = page.with_name(page.stem + "-clock" + page.suffix)
    copy.write_text(page.read_text().replace("<head>", "<head>" + shim, 1))
    return copy


def inline_blob(html: str, element_id: str):
    """The parsed JSON of a page's ``<script type="application/json">``
    data element, or None when the page carries none by that id."""
    match = re.search(rf'<script type="application/json" id="{element_id}">(.*?)</script>', html, re.DOTALL)
    return json.loads(match.group(1)) if match else None


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
            self.assertEqual(names, ["brief.json", "cases.html", "data.json", "grid.html", "index.html", "run.html"])
            brief = json.loads((out / "brief.json").read_text())
            self.assertEqual(brief["health"]["state"], "OUTAGE")
            index = (out / "index.html").read_text()
            self.assertIn('data-page="brief"', index)
            self.assertIn('timeZone: PAGE.tz', index.replace("Object.assign({ timeZone: PAGE.tz }", "timeZone: PAGE.tz"))
            self.assertIn('tz: "America/Toronto"', index)
            self.assertIn("\\u003c", render.bootstrap_json({"x": "<script>"}))
            self.assertNotIn("__PAGES_JS__", index)
            self.assertNotIn("__INLINE_", index)
            self.assertNotIn("__BASE__", index)
            run_page = (out / "run.html").read_text()
            self.assertIn('data-page="run"', run_page)
            for name in ("grid.html", "cases.html"):
                page = (out / name).read_text()
                self.assertIn(f'data-page="{name[:-5]}"', page)
                self.assertIn('<a href="index.html" >Brief</a>', page)
                self.assertNotIn("__BASE__", page)
            self.assertFalse((out / "health.json").exists(), "the adjudicator owns health.json")

    def test_each_page_carries_its_data_inline(self):
        # The pages must render with no request beyond themselves (the
        # published host answers an XHR with a login redirect), so brief.json
        # and the verdict travel inside each page as JSON data elements.
        data = load_fixture()
        data["generated_at"] = NOW
        with tempfile.TemporaryDirectory() as tmp:
            out = render_to(tmp, data, health=health_doc())
            brief = json.loads((out / "brief.json").read_text())
            for name in ("index.html", "run.html"):
                page = (out / name).read_text()
                head = page.split("<body", 1)[0]
                self.assertEqual(inline_blob(head, render.INLINE_BRIEF_ID), brief, f"{name}: the inline brief is brief.json")
                self.assertEqual(inline_blob(head, render.INLINE_HEALTH_ID), brief["health"], f"{name}: the inline verdict")
                self.assertEqual(page.count('id="inline-brief">'), 1, f"{name}: one copy of the document, not two")
                raw = re.search(r'id="inline-brief">(.*?)</script>', head, re.DOTALL).group(1)
                self.assertNotIn("<", raw, f"{name}: no '<' inside the data element, so no string in it can close it")
            self.assertIn('inlineBrief: "inline-brief"', page, "pages.js reads the id render.py writes")
            self.assertIn('inlineHealth: "inline-health"', page)
            self.assertNotIn("__BRIEF_JSON__", page, "the document is a data element, not a JS literal in pages.js")
            # No verdict: the health element is absent and the page still parses its brief.
            out = render_to(pathlib.Path(tmp) / "nohealth", data)
            page = (out / "index.html").read_text()
            self.assertIsNone(inline_blob(page, render.INLINE_HEALTH_ID))
            self.assertIsNone(inline_blob(page, render.INLINE_BRIEF_ID)["health"])

    def test_base_href_only_with_a_public_url(self):
        data = load_fixture()
        data["generated_at"] = NOW
        with tempfile.TemporaryDirectory() as tmp:
            out = render_to(tmp, data)
            for name in ("index.html", "run.html", "grid.html", "cases.html"):
                self.assertNotIn("<base", (out / name).read_text(), f"{name}: a local render keeps relative links")
            out = render_to(pathlib.Path(tmp) / "pub", data, extra_args=["--public-url", "https://example.test/evals"])
            for name in ("index.html", "run.html", "grid.html", "cases.html"):
                page = (out / name).read_text()
                self.assertEqual(page.count("<base "), 1, name)
                self.assertIn('<base href="https://example.test/evals/">', page.split("<body", 1)[0], f"{name}: in <head>, with the trailing slash")
                # Under <base>, a bare "#view=agent" resolves to the site root,
                # not this page: every in-page link must carry its file name.
                self.assertNotIn('href="#', page, f"{name}: no fragment-only link")
            # A trailing slash on the flag is not doubled.
            out = render_to(pathlib.Path(tmp) / "slash", data, extra_args=["--public-url", "https://example.test/evals/"])
            self.assertIn('<base href="https://example.test/evals/">', (out / "index.html").read_text())
            # The bare flag names the published dashboard, post_health's URL.
            out = render_to(pathlib.Path(tmp) / "bare", data, extra_args=["--public-url"])
            self.assertIn('<base href="https://storage.cloud.google.com/kube-agents-dashboards/evals/">', (out / "index.html").read_text())
            self.assertEqual(render.PUBLISHED_SITE + "/index.html", render.post_health.DASHBOARD_URL)
        self.assertEqual(render.base_html(None), "")
        self.assertEqual(render.base_html('https://h/"><script>'), '<base href="https://h/&quot;&gt;&lt;script&gt;/">')

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
        for token in ('param("cases")', 'param("since")', 'param("until")', 'param("build")', 'param("view")', "location.search", "location.hash",
                      "shared_break", "storm", "setup_deaths", "only-this-pr", "#build=", 'pick("window"', 'pick("sort"', 'pick("show"', 'pick("rows"',
                      "grid.html", "cases.html", "health.json", "brief.json"):
            self.assertIn(token, script)
        self.assertNotIn("run.html?build=", script, "the pages write the fragment form only")
        self.assertNotIn("index.html?", script)
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
        self.assertIn('href="run.html#build=2097282860221206528"', app)
        self.assertIn('class="chip hit">cluster-agent-crashloop-debug', app)
        self.assertNotIn("What changed right before", app, "no merges were given, so the block is omitted")
        self.assertIn("See it in the grid", app)
        # The Grid link carries the incident's scope in the fragment, the
        # same text as the Brief's own links, with no view.
        self.assertIn('href="grid.html#since=2026-09-08T09:00:00Z&amp;cases=' + ",".join(CRASHLOOP_TRIO) + '"', app)
        self.assertNotIn("grid.html?", app)
        self.assertIn("Release candidates", app)
        self.assertNotIn(" UTC", app, "no UTC clock text on the page")
        self.assertNotIn("legacy", app.lower())

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

    def test_lost_pods_banner_and_window(self):
        # A build-cluster node loss (#1478): the run page's banner names it
        # rather than calling a DEGRADED state an outage, and the Brief's
        # window reaches back two hours, so the lost runs are on the page.
        lost = health_doc("DEGRADED", condition="lost_pods", failing_cases=[], tracking_issues=[], since="2026-09-08T14:30:00+00:00",
                          cause="lost pods: 12 runs on 12 PRs died with their build node 14:05–14:19 UTC",
                          incident={"prs": [1275, 1246], "runs": 12, "window_start": "2026-09-08T14:05:52+00:00", "window_end": "2026-09-08T14:19:16+00:00", "nodes": {"gke-kube-agents-prow-default-pool-eb220b2a-sgnk": 3}, "event": True})
        out = render_to(pathlib.Path(self.tmp.name) / "lost", self.data, health=lost)
        run_app = dom_text(out / "run.html", query="build=2097282860221206528")
        self.assertIn("<b>Build nodes lost right now</b> since Tue 10:30 AM ET: runs died with the node under them; nothing about the branch.", run_app)
        self.assertNotIn("Gate outage", run_app)
        self.assertIn('lost_pods: 2 * 3600 * 1000', (out / "index.html").read_text())

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
        # "Open the brief for it →" carries the whole scope in the fragment,
        # the text post_health.dashboard_link writes for the same incident.
        self.assertIn('href="index.html#since=2026-09-07T14:00:00Z&amp;until=2026-09-08T01:00:00Z&amp;cases=' + ",".join(CRASHLOOP_TRIO) + '&amp;view=gate"', app)
        self.assertNotIn("index.html?", app)

    def test_healthy_brief_without_any_health_files(self):
        out = render_to(pathlib.Path(self.tmp.name) / "nohealth", self.data)
        app = dom_text(out / "index.html")
        self.assertNotIn("Smoke gate is healthy", app, "the runs alone cannot declare the gate healthy")
        self.assertIn("NO VERDICT", app)
        self.assertIn("No gate verdict is published", app)
        self.assertIn("Last 24 hours", app)
        self.assertIn("No incident history is published yet", app)

    def assert_past_outage_window(self, app, label):
        self.assertIn("PAST OUTAGE · Mon 10:00 AM – 9:00 PM ET", app, label)
        self.assertIn("3 gate cases failed on", app, label)
        self.assertIn("This incident is over", app, label)
        self.assertIn("run.html#build=2096999509014876160", app, f"{label}: #1195, red inside that window")
        self.assertNotIn("run.html#build=2097282860221206528", app, f"{label}: #1275 ran the next morning")

    def test_past_incident_through_the_fragment(self):
        # The form every emitter writes: the scope in the fragment, which the
        # published host's login redirect preserves where it drops a query.
        fragment = "#since=2026-09-07T14:00:00Z&until=2026-09-08T01:00:00Z&cases=" + ",".join(CRASHLOOP_TRIO) + "&view=gate"
        app = dom_text(self.index, fragment=fragment)
        self.assert_past_outage_window(app, "fragment form")
        self.assertIn('id="gate"', app, "the view lands on the why-we-think block")
        # Without the scope the same page is the live outage: the fragment is
        # what scoped it.
        self.assertIn("OUTAGE · since Tue 5:00 AM ET", dom_text(self.index))

    def test_past_incident_through_the_old_query_form(self):
        # Links posted before the fragment form still open the same incident.
        query = urllib.parse.urlencode({"cases": ",".join(CRASHLOOP_TRIO), "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z"})
        app = dom_text(self.index, query=query, fragment="#gate")
        self.assert_past_outage_window(app, "query form")
        fragment = "#since=2026-09-07T14:00:00Z&until=2026-09-08T01:00:00Z&cases=" + ",".join(CRASHLOOP_TRIO) + "&view=gate"
        self.assertEqual(app, dom_text(self.index, fragment=fragment), "both forms render the same page")

    def test_the_query_wins_when_both_forms_are_present(self):
        query = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z"})
        app = dom_text(self.index, query=query, fragment="#since=2026-09-08T09:00:00Z&view=gate")
        self.assertIn("PAST OUTAGE · Mon 10:00 AM – 9:00 PM ET", app)
        self.assertIn("1 gate case failed on", app)
        app = dom_text(self.run_page, query="build=2097282860221206528", fragment="#build=1")
        self.assertIn("PR #1275", app)
        self.assertNotIn("No run with that id", app)

    def test_pr_view_through_the_fragment(self):
        app = dom_text(self.run_page, fragment="#build=2097282860221206528")
        self.assertIn("PR #1275", app)
        self.assertIn("3 of 10 gate cases failed. None of them look like your PR.", app)
        self.assertIn("Gate outage at the time of this run", app)
        self.assertEqual(app, dom_text(self.run_page, query="build=2097282860221206528"), "the old query form renders the same run")
        app = dom_text(self.run_page, fragment="#build=1")
        self.assertIn(f"No run with that id in the last {render.RUN_VIEW_DAYS} days.", app)

    def test_past_incident_without_history_is_described_by_the_parameters(self):
        out = render_to(pathlib.Path(self.tmp.name) / "nohist", self.data, health=health_doc("GREEN"))
        query = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z"})
        app = dom_text(out / "index.html", query=query)
        self.assertIn("PAST INCIDENT", app)
        self.assertIn("1 gate case failed on", app)

    def test_agent_view_shows_the_numbers(self):
        app = dom_text(self.index, fragment="#view=agent")
        self.assertIn("The last 24 hours in numbers", app)
        self.assertIn('id="agent"', app)
        self.assertEqual(app, dom_text(self.index, fragment="#agent"), "the old bare anchor selects the same view")
        self.assertIn('href="index.html#view=agent"', app, "the footer link is the fragment form")

    def test_a_view_scrolls_once_on_navigation_and_not_on_the_poll(self):
        # 130 s of virtual time: boot, the refresh after it, and two polls.
        # The section is scrolled to once; the polls, which re-render the
        # page, must not pull a reader back to it.
        page = scroll_counting_page(self.index)
        scrolls = lambda html: re.search(r'<body[^>]*data-scrolls="(\d+)"', html)
        self.assertEqual(scrolls(dom_html(page, fragment="#since=2026-09-07T14:00:00Z&view=gate", budget_ms=130000)).group(1), "1")
        self.assertEqual(scrolls(dom_html(page, fragment="#gate", budget_ms=130000)).group(1), "1", "the bare anchor, the same way")
        self.assertIsNone(scrolls(dom_html(page, budget_ms=130000)), "no view, no scroll")

    def test_hostile_parameters_never_reach_the_dom(self):
        for form in ({"query": "cases=%3Cimg%20src%3Dx%3E&since=%3Cscript%3E"}, {"fragment": "#cases=%3Cimg%20src%3Dx%3E&since=%3Cscript%3E&view=%3Cb%3E"}):
            app = dom_text(self.index, **form)
            self.assertNotIn("<img", app, form)
            self.assertNotIn("<script", app, form)
            self.assertIn("OUTAGE · since Tue 5:00 AM ET", app, f"{form}: the bad parameters were dropped and the live page still rendered")
        for form in ({"query": "build=%3Cb%3E1%3C%2Fb%3E"}, {"fragment": "#build=%3Cb%3E1%3C%2Fb%3E"}):
            app = dom_text(self.run_page, **form)
            self.assertNotIn("<b>1", app, form)
            self.assertIn("Which run?", app, form)

    def test_a_space_separated_since_is_read_as_utc(self):
        query = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07 14:00:00", "until": "2026-09-08 01:00:00"})
        app = dom_text(self.index, query=query)
        self.assertIn("Mon 10:00 AM – 9:00 PM ET", app)

    def test_a_no_colon_offset_in_since_scopes_the_brief_like_the_colon_form(self):
        # 16:00+02:00 is 14:00Z and 03:00+02:00 the next day is 01:00Z: the
        # same past window test_a_space_separated_since_is_read_as_utc opens.
        with_colon = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07T16:00:00+02:00", "until": "2026-09-08T03:00:00+02:00"})
        without = urllib.parse.urlencode({"cases": "cluster-agent-crashloop-debug", "since": "2026-09-07T16:00:00+0200", "until": "2026-09-08T03:00:00+0200"})
        expected = dom_text(self.index, query=with_colon)
        self.assertIn("Mon 10:00 AM – 9:00 PM ET", expected)
        self.assertEqual(dom_text(self.index, query=without), expected, "in V8, which reads ±HHMM on its own")
        strict = dom_text(strict_date_parse_page(self.index), query=without)
        self.assertNotIn("OUTAGE · since Tue 5:00 AM ET", strict, "the parameter was dropped and the live brief rendered instead")
        self.assertEqual(strict, expected, "in an engine that rejects ±HHMM, so parseIso must normalise it")

    def test_the_current_outage_opened_through_its_own_link_is_still_live(self):
        scope = "since=2026-09-08T09:00:00Z&cases=" + ",".join(CRASHLOOP_TRIO)
        forms = (("fragment form", {"fragment": f"#{scope}&view=gate"}), ("old query form", {"query": scope, "fragment": "#gate"}))
        nohist = render_to(pathlib.Path(self.tmp.name) / "nohist-live", self.data, health=health_doc())
        for label, page in (("with history", self.index), ("without history", nohist / "index.html")):
            for form, args in forms:
                app = dom_text(page, **args)
                self.assertIn("OUTAGE · since Tue 5:00 AM ET", app, f"{label}, {form}")
                self.assertNotIn("PAST", app, f"{label}, {form}")
                self.assertNotIn("This incident is over", app, f"{label}, {form}")
                self.assertIn("What's being done", app, f"{label}, {form}")
                self.assertIn("issues/1278", app, f"{label}, {form}")
        # The PR view's own banner link is that fragment, so following it
        # lands on the live brief.
        run_app = dom_text(nohist / "run.html", fragment="#build=2097282860221206528")
        self.assertIn(f'href="index.html#{scope}&view=gate"'.replace("&", "&amp;"), run_app)

    def test_an_incident_without_a_start_or_a_red_run_dates_nothing_from_1969(self):
        # normalizeHealth keeps a non-GREEN state whose `since` will not
        # parse, and a case that never failed in the window leaves no first
        # red run: with no anchor the merge lines are dropped, not dated
        # from epoch zero (Dec 31, 1969 ET).
        merges = [{"sha": "abc1234", "at": "2026-09-08T08:10:00+00:00", "title": "fix(ci): the thing", "pr": 1280}]
        with unittest.mock.patch.object(render, "recent_merges", return_value=merges):
            out = render_to(pathlib.Path(self.tmp.name) / "nosince", self.data, health=health_doc(since="not a time", failing_cases=["never-failed-here"], recovering=True))
            control = render_to(pathlib.Path(self.tmp.name) / "withsince", self.data, health=health_doc())
        app = dom_text(out / "index.html")
        self.assertIn("RECOVERING · since unknown time", app)
        self.assertIn("What changed right before", app)
        self.assertNotIn("Nothing merged", app)
        # et() prints no year, so the epoch shows as "Dec 31" in ET.
        self.assertNotIn("Dec 31", app)
        self.assertIn("no start time on record", app)
        self.assertIn("0 of 3 clean runs", app, "with no start there is nothing after the incident to count as recovery")
        # The normal case still anchors on the first red run.
        control_app = dom_text(control / "index.html")
        self.assertIn("What changed right before", control_app)
        self.assertNotIn("no start time on record", control_app)
        self.assertNotIn("Dec 31", control_app)

    def test_the_incident_link_carries_only_what_the_parser_reads(self):
        # Sixty failing cases, one of them outside the id grammar: the
        # banner's link must carry the first 50 in-grammar ids and nothing
        # else, so following it scopes the Brief to exactly what it shows.
        sixty = [f"case-{i:02d}" for i in range(59)]
        sixty.insert(3, "bad case!")
        out = render_to(pathlib.Path(self.tmp.name) / "sixty", self.data, health=health_doc(failing_cases=sixty))
        app = dom_text(out / "run.html", fragment="#build=2097282860221206528")
        hrefs = re.findall(r'href="(index\.html#since=[^"]*)"', app)
        self.assertEqual(len(hrefs), 1, app[:300])
        fragment = urllib.parse.urlparse(html.unescape(hrefs[0])).fragment
        cases = urllib.parse.parse_qs(fragment)["cases"][0].split(",")
        self.assertEqual(cases, [f"case-{i:02d}" for i in range(50)])
        self.assertNotIn("bad case!", fragment)
        # And the parser reads that link back whole: 50 ids, not 49.
        self.assertIn("50 gate cases fail", dom_text(out / "index.html", fragment=f"#{fragment}"))
        # A hand-written link with more entries than the cap and an
        # off-grammar one inside the first 50 still yields 50 in-grammar ids:
        # the parser filters before it caps, as the writer does. (`since`
        # names the live incident; without it the link's cases are not read.)
        hand_written = urllib.parse.urlencode({"cases": ",".join(sixty[:51]), "since": OUTAGE_SINCE})
        self.assertIn("50 gate cases fail", dom_text(out / "index.html", query=hand_written))
        self.assertIn("50 gate cases fail", dom_text(out / "index.html", fragment=f"#{hand_written}"))

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
        self.assertIn('href="cases.html#cluster-agent-crashloop-debug">this case\'s history</a>', app)

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

    def test_pages_render_whole_when_every_fetch_fails(self):
        # From file:// every fetch fails, as it does on storage.cloud.google.com
        # (an XHR there is answered with a login redirect). The pages must
        # render fully from their inlined data, the badge must not call the
        # feed unreachable, and it must say how old the page can be instead.
        # The clock is pinned to the data's generated_at so the badge is fresh.
        for name, query, expect in (
            ("index.html", "", "3 gate cases fail on every PR"),
            ("index.html", "", "Runs in this window"),
            ("run.html", "build=2097282860221206528", "3 of 10 gate cases failed. None of them look like your PR."),
        ):
            page = clock_page(self.out / name, NOW)
            html = dom_html(page, query=query)
            app = html[html.find('<div id="app">'):html.find("<script>\n/* The Brief")]
            self.assertIn(expect, app, f"{name}?{query}")
            self.assertNotIn("could not render", app, name)
            cls, text = freshness_badge(html)
            self.assertEqual(cls, "fresh", f"{name}: not amber")
            self.assertNotIn("UNREACHABLE", text, name)
            self.assertNotIn("STALE", text, name)
            self.assertEqual(text, "updated Tue 10:30 AM ET · 0m ago · regenerated every 15 min", name)

    def test_a_page_without_its_data_element_says_so(self):
        # A truncated upload must not read as a quiet, empty dashboard.
        for name, query in (("index.html", ""), ("run.html", "build=2097282860221206528")):
            page = self.out / name
            copy = page.with_name(page.stem + "-nodata" + page.suffix)
            copy.write_text(re.sub(r'<script type="application/json" id="inline-brief">.*?</script>', "", page.read_text(), count=1, flags=re.DOTALL))
            app = dom_text(copy, query=query)
            self.assertIn("This page could not render.", app, name)
            self.assertIn("inline-brief", app, name)
            self.assertNotIn("No gate verdict is published", app, name)
            self.assertNotIn("No run with that id", app, name)

    def test_old_inline_data_still_reads_stale(self):
        # Not calling a failed poll UNREACHABLE must not hide real staleness:
        # inlined data older than its stale_after_s (7200 s default) is STALE.
        three_hours_on = "2026-09-08T17:30:00+00:00"
        for name in ("index.html", "run.html", "grid.html", "cases.html"):
            cls, text = freshness_badge(dom_html(clock_page(self.out / name, three_hours_on)))
            self.assertEqual(cls, "fresh stale", name)
            self.assertTrue(text.startswith("STALE · updated "), f"{name}: {text}")
            self.assertIn("180m ago · regenerated every 15 min", text, name)
            self.assertNotIn("UNREACHABLE", text, name)


RC_RELEASE = {
    "build_id": "2097891568546484224", "rc_tag": "staging_2609092307_5b5ad10", "commit": "5b5ad10", "tier": "nightly",
    "verdict": "GREEN", "result": "SUCCESS", "started": "2026-09-10T03:35:01+00:00", "finished": "2026-09-10T07:51:10+00:00",
    "duration_s": 15006, "project": "kube-agents-evals-10",
    "artifacts_url": "https://oss.gprow.dev/view/gs/kube-agents-prow/logs/post-kube-agents-eval-rc/2097891568546484224",
    "pass_rate": 0.9, "baseline_rate": None, "margin": None,
    "tasks": [{"name": f"case-{n}", "result": "pass" if n < 15 else "fail"} for n in range(25)] + [{"name": "case-infra", "result": "infra"}],
}
MERGES = [
    {"sha": "abc1234abc1234", "at": "2026-09-07T20:10:00+00:00", "title": "fix(ci): the thing", "pr": 1280},
    {"sha": "def5678def5678", "at": "2026-09-08T12:40:00+00:00", "title": "docs: no pr number", "pr": None},
]


def clicked_page(page: pathlib.Path, selector: str) -> pathlib.Path:
    """A copy of the rendered page that clicks `selector` once the script
    has rendered: the page's script runs synchronously at the end of the
    body, so a script after it sees the rendered DOM."""
    shim = f"<script>(() => {{ const el = document.querySelector({json.dumps(selector)}); if (el) el.click(); }})();</script>"
    copy = page.with_name(page.stem + "-click" + page.suffix)
    copy.write_text(page.read_text().replace("</body>", shim + "</body>", 1))
    return copy


@unittest.skipUnless(chrome(), "headless Chrome not found")
class CasesAndGridPagesTest(unittest.TestCase):
    """The Cases page and the Grid as a browser renders them from the real
    fixture week. The roster is the test's: two of the trio are admitted,
    the third is dated as demoted, so every pill has a row."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = load_fixture()
        data["generated_at"] = NOW
        data["cases"] = [{"name": n, "domain": "cluster-debugging", "active": True} for n in CRASHLOOP_TRIO]
        data["cases"].append({"name": "retired-probe", "domain": "cost", "active": False})
        data["releases"] = [RC_RELEASE, dict(RC_RELEASE, build_id="7", rc_tag="hostile", artifacts_url="javascript:alert(1)", verdict=None, started="2026-09-09T03:35:01+00:00")]
        data["pending_builds"] = [{"build_id": "2097300000000000000", "first_seen": "2026-09-08T14:00:00+00:00"}]
        # A green run that recorded no cases (the gate revalidated the
        # branch's earlier run) sits inside the live window: no Grid column.
        data["runs"].append({"build_id": "2097299999999999999", "pr": 4242, "started": "2026-09-08T13:00:00+00:00",
                             "finished": "2026-09-08T13:04:00+00:00", "result": "SUCCESS", "duration_s": 240, "tasks": []})
        # Hostile strings on the paths the Grid and the Cases page render.
        oldest = next(r for r in data["runs"] if any(t.get("name") == CRASHLOOP_TRIO[0] and t.get("reps") for t in r["tasks"]))
        next(t for t in oldest["tasks"] if t.get("name") == CRASHLOOP_TRIO[0])["reps"][0]["reason"] = "<script>alert(1)</script> required phrases absent"
        data["cases"].append({"name": "<img src=x onerror=alert(2)>", "domain": "<b>x</b>", "active": False, "nightly_active": True})
        # A case whose only failure is older than brief.json's 14-day run
        # window: its "last failure" cannot link a run page that has no run.
        data["runs"].append({"build_id": "2090000000000000000", "pr": 777, "started": "2026-08-20T10:00:00+00:00",
                             "finished": "2026-08-20T11:00:00+00:00", "result": "FAILURE", "duration_s": 3600,
                             "tasks": [{"name": "old-failure-case", "result": "fail", "reps": [{"n": 1, "result": "fail", "reason": "check ancient: required phrases absent"}]}]})
        data["cases"].append({"name": "old-failure-case", "domain": "cost", "active": True})
        history = history_lines(
            dict(health_doc("GREEN"), tick="2026-09-06T01:00:00+00:00", since="2026-09-06T01:00:00+00:00"),
            dict(health_doc(since="2026-09-07T14:00:00+00:00"), tick="2026-09-07T14:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-08T01:00:00+00:00", since="2026-09-08T01:00:00+00:00"),
            dict(health_doc(), tick="2026-09-08T09:00:00+00:00"),
            dict(health_doc(), tick=NOW),
        )
        admitted = frozenset(CRASHLOOP_TRIO[:2])
        with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=admitted), \
                unittest.mock.patch.object(render, "demotion_dates", return_value={CRASHLOOP_TRIO[2]: "2026-09-02"}), \
                unittest.mock.patch.object(render, "recent_merges", return_value=MERGES):
            cls.out = render_to(cls.tmp.name, data, health=health_doc(), history=history)
        cls.cases_page = cls.out / "cases.html"
        cls.grid_page = cls.out / "grid.html"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cases_page_rows_pills_rates_and_last_failure(self):
        app = dom_text(self.cases_page)
        self.assertIn("How reliable is each test?", app)
        self.assertIn('<tr class="grp"><td colspan="8">cluster debugging</td></tr>', app)
        self.assertIn('id="case-cluster-agent-crashloop-debug"', app)
        self.assertIn('<span class="st blocking">blocking</span>', app)
        self.assertIn('<span class="st demoted">demoted 09-02</span>', app)
        self.assertIn("held out · 2 cases", app)
        self.assertIn("not in any matrix · 1 case", app)
        self.assertNotIn('id="case-retired-probe"', app, "retired cases are folded until asked for")
        self.assertIn('class="rate ', app)
        self.assertIn("Nightly 7d", app)
        self.assertIn('<span class="rate none">—</span>', app, "no nightly on record reads as a dash")
        self.assertIn("Last failure was", app)
        self.assertIn("rca-names-the-oom", app)
        self.assertIn('<span class="lbl">Last failure was</span> <span><b>', app)
        self.assertIn(" ET", app)
        self.assertNotIn(" UTC", app)
        self.assertIn("how the roster works", app)
        self.assertIn('href="run.html#build=', app)
        self.assertNotIn(".html?", app, "the pages write the fragment form only")

    def test_cases_page_sorts_filters_and_the_hash_highlight(self):
        by_name = dom_text(self.cases_page, query="sort=name")
        self.assertNotIn('class="grp"', by_name, "a flat list by name has no group rows")
        self.assertIn('id="case-retired-probe"', by_name)
        names = re.findall(r'id="case-([^"]+)"', by_name)
        self.assertEqual(names, sorted(names))
        blocking = dom_text(self.cases_page, query="show=blocking")
        self.assertNotIn("demoted 09-02", blocking)
        self.assertIn('<span class="st blocking">blocking</span>', blocking)
        held = dom_text(self.cases_page, query="show=held")
        self.assertIn("demoted 09-02", held)
        self.assertNotIn('class="st blocking"', held)
        highlighted = dom_text(self.cases_page, fragment="#cluster-agent-crashloop-evidence-chain")
        self.assertIn('<tr id="case-cluster-agent-crashloop-evidence-chain" class="main hl">', highlighted)
        self.assertEqual(dom_text(self.cases_page, fragment="#sort=name&show=held"), dom_text(self.cases_page, query="sort=name&show=held"), "the fragment form reads the same")
        # The row is scrolled to once, on navigation; the polls that follow
        # re-render without pulling the reader back to it.
        scrolls = re.search(r'<body[^>]*data-scrolls="(\d+)"', dom_html(scroll_counting_page(self.cases_page), fragment="#cluster-agent-crashloop-evidence-chain", budget_ms=130000))
        self.assertEqual(scrolls.group(1), "1")
        clicked = dom_text(clicked_page(self.cases_page, 'button[data-toggle="retired"]'))
        self.assertIn('id="case-retired-probe"', clicked)

    def test_grid_live_window_columns_markers_and_folding(self):
        app = dom_text(self.grid_page)
        self.assertIn("Cases by run", app)
        self.assertIn('data-window="36h" class="on"', app)
        self.assertIn('class="grp">cluster debugging · blocking</div>', app)
        self.assertIn('class="nm h"', app, "the demoted case is a held-out row")
        self.assertIn("outage: shared break", app)
        self.assertIn("merge #1280", app, "merged 18 hours before the data's generated_at")
        self.assertIn('class="mk incident"', app)
        self.assertIn('class="c running"', app, "a pending build is a still-running column")
        self.assertIn('class="c died"', app, "a setup death is a column with no cases")
        self.assertIn("presubmit runs in start order", app)
        six = dom_text(self.grid_page, query="window=6h")
        self.assertLess(six.count('class="hd"'), app.count('class="hd"'))
        self.assertEqual(six, dom_text(self.grid_page, fragment="#window=6h"), "the fragment form reads the same")
        failing = dom_text(self.grid_page, query="rows=failing")
        self.assertNotIn("passed everything in this window", failing)

    def test_hostile_data_and_a_bad_fragment_never_break_the_new_pages(self):
        for page in (self.cases_page, self.grid_page):
            for fragment in ("", "#%", "#%E0%A4%A"):
                app = dom_text(page, fragment=fragment)
                self.assertNotIn("<img", app, f"{page.name}{fragment}")
                self.assertNotIn("<script>alert", app, f"{page.name}{fragment}")
                self.assertNotIn("<b>x</b>", app, f"{page.name}{fragment}")
                self.assertNotIn("could not render", app, f"{page.name}{fragment}")
                self.assertIn("cluster-agent-crashloop-debug", app, f"{page.name}{fragment} rendered")
        cases = dom_text(self.cases_page)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", cases, "the hostile name is on the page, escaped")

    def test_an_old_last_failure_links_the_pull_request_not_a_missing_run_page(self):
        app = dom_text(self.cases_page)
        row = app.split('id="case-old-failure-case"', 1)[1].split("</tr>", 2)[1]
        self.assertIn("check ancient", row)
        self.assertNotIn("run.html#build=2090000000000000000", row)
        self.assertIn('href="https://github.com/gke-labs/kube-agents/pull/777"', row)
        self.assertIn("older than this page&#x27;s 14-day run window".replace("&#x27;", "'"), row)
        # A failure inside the window still opens the run's page.
        self.assertIn('href="run.html#build=', app)

    def test_a_green_run_without_cases_gets_no_grid_column(self):
        app = dom_text(self.grid_page)
        self.assertNotIn("#4242", app)
        self.assertIn("without cases", app)
        self.assertIn("#4242", dom_text(self.out / "index.html"), "the Brief still lists it")

    def test_the_this_incident_chip_wins_over_a_window_parameter(self):
        query = urllib.parse.urlencode({"cases": CRASHLOOP_TRIO[0], "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z", "window": "24h"})
        app = dom_text(self.grid_page, query=query)
        self.assertIn('data-window="24h" class="on"', app)
        clicked = dom_text(clicked_page(self.grid_page, 'button[data-window="linked"]'), query=query)
        self.assertIn('data-window="linked" class="on"', clicked)
        self.assertNotIn('data-window="24h" class="on"', clicked)

    def test_grid_deep_link_scopes_to_the_incident(self):
        # The form the Brief's "See it in the grid" link writes.
        fragment = "#since=2026-09-07T14:00:00Z&until=2026-09-08T01:00:00Z&cases=" + ",".join(CRASHLOOP_TRIO)
        app = dom_text(self.grid_page, fragment=fragment)
        self.assertIn("PAST OUTAGE", app)
        self.assertIn("read the brief →", app)
        self.assertIn('data-window="linked" class="on">this incident</button>', app)
        self.assertIn('class="grp">in this incident</div>', app)
        self.assertEqual(app.count('class="nm lk"'), 3, "the linked cases are pinned first")
        self.assertIn("#1195", app, "red inside that window")
        self.assertNotIn("#1275", app, "ran the next morning")
        self.assertIn("merge #1280", app, "merged inside the window")
        self.assertNotIn('class="c running"', app, "a past window has no running columns")
        self.assertIn("Mon 10:00 AM – 9:00 PM ET", app)
        # A link posted in the older query form opens the same window.
        query = urllib.parse.urlencode({"cases": ",".join(CRASHLOOP_TRIO), "since": "2026-09-07T14:00:00Z", "until": "2026-09-08T01:00:00Z"})
        self.assertEqual(app, dom_text(self.grid_page, query=query))

    def test_a_cell_click_opens_the_detail_panel(self):
        page = clicked_page(self.grid_page, 'button.c.fail[data-case="cluster-agent-crashloop-debug"]')
        app = dom_text(page)
        self.assertIn('id="detail"', app)
        self.assertIn("<code>cluster-agent-crashloop-debug</code>", app)
        self.assertIn("failed all 3 graded reps", app)
        self.assertIn("rca-names-the-oom", app)
        self.assertIn("other case", app)
        self.assertIn('href="cases.html#cluster-agent-crashloop-debug"', app)
        self.assertIn("this run&#x27;s page".replace("&#x27;", "'"), app)
        self.assertIn("artifacts/eval_cluster-agent-crashloop-debug_rep1.log", app)
        self.assertIn('class="c fail sel"', app)
        self.assertNotIn('id="detail"', dom_text(self.grid_page), "nothing is selected until a cell is clicked")

    def test_the_brief_lists_the_release_candidates(self):
        app = dom_text(self.out / "index.html")
        self.assertIn("Release candidates", app)
        self.assertIn('href="https://oss.gprow.dev/view/gs/kube-agents-prow/logs/post-kube-agents-eval-rc/2097891568546484224"', app)
        self.assertIn('<span class="pill p-pass">GREEN</span>', app)
        self.assertIn("90.0%", app)
        self.assertIn("baselines maturing", app)
        self.assertIn("15/25", app)
        self.assertIn("1 infra excluded", app)
        self.assertIn("Wed 11:35 PM ET", app)
        self.assertIn("4h 10m", app)
        self.assertIn('<span class="pill p-infra">NO VERDICT</span>', app)
        self.assertIn("no eval banner · job SUCCESS", app)
        self.assertNotIn("javascript:", app)
        self.assertNotIn('href="hostile', app)


if __name__ == "__main__":
    unittest.main()
