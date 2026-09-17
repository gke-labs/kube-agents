"""trend.py: the Trend page's document from the evidence store's records, and
-- when headless Chrome is present -- trend.html, the Cases page's link to it
and the Brief's last-incident link, as a browser renders them.

The records are the four real objects of night one (testdata_store/, read
through store.py's parser) plus synthetic later nights, so the trailing
window, the spread band and a version-key change all have something to
draw. Every asserted time is America/Toronto.
"""

import copy
import json
import pathlib
import re
import tempfile
import unittest
import unittest.mock

from eval_dashboard import render, store, trend
from test_eval_dashboard_pages import (
    chrome,
    clicked_page,
    dom_text,
    health_doc,
    history_lines,
    load_fixture,
    render_to,
)

FIXTURE = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_store" / "evidence"
LOCATION = "gs://kube-agents-evals-bench/evidence"
NOW = "2026-09-17T14:30:00+00:00"  # Thu 10:30 AM ET
NIGHT_1_BUILD = "2100374258805903360"  # the real night: Wed 8 PM ET start, recorded Thu 1:54 AM ET
KEY_1 = "gemini-3-1-pro-preview-kubeagents-mcp/gemini-3.1-pro-preview/v1-f1-v1"
KEY_2 = "gemini-3-1-pro-preview-kubeagents-mcp/gemini-3.5-pro/v1-f1-v1"


def night_one_records():
    """The four real records, parsed the way store.py parses a cat."""
    warnings = []
    records = []
    for path in sorted(FIXTURE.rglob("*.jsonl")):
        url = f"{LOCATION}/{path.relative_to(FIXTURE).as_posix()}"
        records.extend(store.parse_records(path.read_text(encoding="utf-8"), [url], LOCATION, warnings))
    assert not warnings
    return records


def later_night(records, day, build, passes=None, judged=None, key=None):
    """Night one's records replayed on another day, with overrides."""
    out = []
    for record in records:
        r = copy.deepcopy(record)
        r["recorded_at"] = f"2026-09-{day:02d}T05:50:00Z"
        r["build"] = build
        r["object"] = f"{LOCATION}/{r['case']}/x/{day}.jsonl"
        r["commit"] = f"c{day:02d}" + "0" * 38
        if passes is not None:
            r["passes"] = passes.get(r["case"], r["passes"])
        if judged:
            for metric, mean in judged.items():
                r["judged"][metric] = {"mean": mean, "n": 3}
        if key:
            r["key"] = dict(r["key"], **key)
        out.append(r)
    return out


def store_doc(records, **extra):
    doc = {"schema_version": 1, "source": LOCATION, "read_at": "2026-09-17T14:09:02Z", "window_days": 90, "max_objects": 200,
           "listed": len(records), "fetched": len(records), "truncated": {}, "warnings": [], "error": None, "records": records}
    doc.update(extra)
    return doc


def data_with_nightly(cases, runs=()):
    return {"schema_version": 1, "generated_at": NOW,
            "cases": [{"name": n, "domain": d, "active": True, "nightly_active": True} for n, d in cases],
            "runs": list(runs)}


NIGHT_1_RUN = {"build_id": NIGHT_1_BUILD, "tier": "nightly", "job": "ci-kube-agents-eval-nightly", "pr": None, "head_sha": "b458323",
               "started": "2026-09-17T00:00:10+00:00", "finished": "2026-09-17T05:55:00+00:00", "result": "SUCCESS", "eval_verdict": "RED",
               "duration_s": 21290, "log_url": f"https://oss.gprow.dev/view/gs/kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly/{NIGHT_1_BUILD}", "tasks": []}
DOMAINS = [("agent-kanban-smoke", "chat-and-routing"), ("cluster-agent-crashloop-debug", "cluster-debugging"),
           ("rca-remediation-pr", "remediation"), ("upgrades-fleet-version-table", "upgrades")]


class NightOneTest(unittest.TestCase):
    """The real first night, alone: one point per case, no line, no band."""

    @classmethod
    def setUpClass(cls):
        cls.doc = trend.trend_document(store_doc(night_one_records()), data_with_nightly(DOMAINS, [NIGHT_1_RUN]))

    def test_the_document_shape(self):
        d = self.doc
        self.assertEqual((d["source"], d["read_at"], d["error"], d["window_days"], d["max_objects"]), (LOCATION, "2026-09-17T14:09:02Z", None, 90, 200))
        self.assertEqual(d["records"], 4)
        self.assertEqual(d["metrics"], ["OutcomeValidity", "OutcomeScore", "ToolInvocation"], "rung 6's metric first, then by name")
        self.assertEqual(d["default_metric"], "OutcomeValidity")
        self.assertEqual(d["bar"], {"rate": 0.95, "min_runs": 20})
        self.assertEqual(list(d["keys"]), [KEY_1])
        self.assertEqual(d["keys"][KEY_1], {"setup_id": "gemini-3-1-pro-preview-kubeagents-mcp", "scoring_version": "v1", "judge_model": "gemini-3.1-pro-preview", "fleet": 1, "verifiers": 1})
        self.assertEqual(sorted(d["cases"]), [n for n, _ in DOMAINS])
        self.assertEqual(sorted(d["domains"]), sorted({dom for _, dom in DOMAINS}))

    def test_the_night_is_joined_to_the_collectors_nightly_run(self):
        (night,) = self.doc["nights"]
        self.assertEqual(night, {"id": f"build:{NIGHT_1_BUILD}", "at": "2026-09-17T05:54:31Z", "build": NIGHT_1_BUILD, "commit": "b458323d9c1dbf36551b498e6ab097828b4c2cba",
                                 "started": "2026-09-17T00:00:10+00:00", "log_url": NIGHT_1_RUN["log_url"], "cases": 4})
        # Without the run in data.json the night stands on its own stamp.
        alone = trend.trend_document(store_doc(night_one_records()), data_with_nightly(DOMAINS))
        self.assertEqual((alone["nights"][0]["started"], alone["nights"][0]["log_url"]), (None, None))

    def test_a_case_with_one_night_has_a_partial_window_and_no_spread(self):
        rca = self.doc["cases"]["rca-remediation-pr"]
        self.assertEqual(rca["domain"], "remediation")
        (point,) = rca["points"]
        self.assertEqual((point["runs"], point["passes"], point["key"], point["build"]), (3, 2, KEY_1, NIGHT_1_BUILD))
        self.assertEqual(point["window"], {"runs": 3, "passes": 2, "lines": 1, "full": False})
        self.assertEqual(point["judged"]["OutcomeValidity"]["n"], 3)
        self.assertEqual(point["judged"]["OutcomeValidity"]["spread"]["nights"], 1, "one night is no spread")
        self.assertEqual(rca["key_changes"], [])
        self.assertEqual(rca["record"]["state"], "collecting")
        self.assertEqual((rca["record"]["runs"], rca["record"]["passes"], rca["record"]["lines"]), (3, 2, 1))
        self.assertAlmostEqual(rca["record"]["rate"], 2 / 3)

    def test_a_domain_pools_its_cases_per_night_with_the_range_across_them(self):
        # Two cases share a domain here only by the test's map; use a map that pools two.
        doc = trend.trend_document(store_doc(night_one_records()), data_with_nightly([(n, "one") for n, _ in DOMAINS]))
        (point,) = doc["domains"]["one"]["points"]
        self.assertEqual((point["runs"], point["passes"], point["cases"], point["keys"]), (12, 11, 4, [KEY_1]))
        ov = point["judged"]["OutcomeValidity"]
        self.assertEqual((ov["n"], ov["cases"]), (12, 4))
        self.assertAlmostEqual(ov["low"], 0.7667, places=3)
        self.assertAlmostEqual(ov["high"], 1.0)
        self.assertAlmostEqual(ov["mean"], (1.0 + 0.9 + 0.7666666666666666 + 0.8) / 4, places=6)
        self.assertEqual(doc["domains"]["one"]["cases"], [n for n, _ in DOMAINS])

    def test_a_case_the_checkout_does_not_know_lands_in_the_unknown_domain(self):
        doc = trend.trend_document(store_doc(night_one_records()), data_with_nightly([]))
        self.assertEqual({c["domain"] for c in doc["cases"].values()}, {"unknown"})


class ManyNightsTest(unittest.TestCase):
    """Eight nights: the window fills on the seventh, a key change on the
    eighth starts pooling over, and the spread spans the nights at one key."""

    @classmethod
    def setUpClass(cls):
        base = night_one_records()
        records = list(base)
        for day in range(18, 24):  # six more nights at key 1: 21 runs from the seventh
            records += later_night(base, day, f"21007{day}0000000000000", passes={"rca-remediation-pr": 3 if day % 2 else 2}, judged={"OutcomeValidity": 0.6 + day / 100})
        records += later_night(base, 24, "2100724000000000000", judged={"OutcomeValidity": 0.95}, key={"judge_model": "gemini-3.5-pro"})
        cls.doc = trend.trend_document(store_doc(records), data_with_nightly(DOMAINS, [NIGHT_1_RUN]))
        cls.rca = cls.doc["cases"]["rca-remediation-pr"]

    def test_the_trailing_window_pools_whole_nights_to_the_bar_and_never_across_a_key(self):
        windows = [(p["window"]["runs"], p["window"]["lines"], p["window"]["full"]) for p in self.rca["points"]]
        self.assertEqual(windows[:7], [(3, 1, False), (6, 2, False), (9, 3, False), (12, 4, False), (15, 5, False), (18, 6, False), (21, 7, True)])
        self.assertEqual(windows[7], (3, 1, False), "the new key starts its own window")
        self.assertEqual(self.rca["record"]["state"], "collecting")
        self.assertEqual(self.rca["record"]["key"], KEY_2)

    def test_the_spread_is_the_range_of_nightly_means_at_one_key(self):
        spreads = [p["judged"]["OutcomeValidity"]["spread"] for p in self.rca["points"]]
        self.assertEqual([s["nights"] for s in spreads], [1, 2, 3, 4, 5, 6, 7, 1])
        self.assertAlmostEqual(spreads[6]["low"], 0.7667, places=3)
        self.assertAlmostEqual(spreads[6]["high"], 0.83)
        self.assertEqual(spreads[7], {"low": 0.95, "high": 0.95, "nights": 1})

    def test_the_key_change_is_marked_with_the_components_that_moved(self):
        self.assertEqual(self.rca["key_changes"], [{"night": "build:2100724000000000000", "at": "2026-09-24T05:50:00Z", "from": KEY_1, "to": KEY_2, "changed": ["judge_model"]}])
        self.assertEqual(sorted(self.doc["keys"]), [KEY_1, KEY_2])
        self.assertEqual(self.doc["domains"]["remediation"]["key_changes"], self.rca["key_changes"], "a domain carries its cases' changes once")
        self.assertEqual([n["id"] for n in self.doc["nights"]][:2], [f"build:{NIGHT_1_BUILD}", "build:21007180000000000000"])
        self.assertEqual(len(self.doc["nights"]), 8)

    def test_a_full_window_is_judged_against_the_bar(self):
        seven = self.rca["points"][6]
        self.assertTrue(seven["window"]["full"])
        state = trend.record_state(self.rca["points"][:7])
        self.assertEqual(state["state"], "would-demote")
        self.assertEqual((state["runs"], state["passes"]), (21, 2 + 3 + 2 + 3 + 2 + 3 + 2))
        clean = trend.record_state(self.doc["cases"]["agent-kanban-smoke"]["points"][:7])
        self.assertEqual((clean["state"], clean["runs"], clean["passes"]), ("would-admit", 21, 21))


class StoreStatesTest(unittest.TestCase):
    def test_no_store_an_error_and_odd_records_all_yield_a_document(self):
        empty = trend.trend_document(None, data_with_nightly(DOMAINS))
        self.assertEqual((empty["source"], empty["read_at"], empty["records"], empty["cases"], empty["domains"], empty["metrics"], empty["default_metric"]), (None, None, 0, {}, {}, [], None))
        failed = trend.trend_document(store_doc(night_one_records(), error="2026-09-17T14:30:00Z: gsutil ls: 403", truncated={"rca-remediation-pr": 2}, warnings=["x:1: not valid JSON"]), data_with_nightly(DOMAINS))
        self.assertEqual((failed["error"], failed["truncated"], failed["warnings"], failed["records"]), ("2026-09-17T14:30:00Z: gsutil ls: 403", {"rca-remediation-pr": 2}, ["x:1: not valid JSON"], 4))
        odd = store_doc(night_one_records() + [None, {"case": 3}, {"case": "x", "key": {}, "recorded_at": "junk"}, {"case": "y", "key": {}, "recorded_at": "2026-09-17T06:00:00Z", "runs": "three", "judged": {"OutcomeValidity": {"mean": "high", "n": 3}, "Other": {"mean": 0.5, "n": 0}}}])
        doc = trend.trend_document(odd, data_with_nightly(DOMAINS))
        self.assertEqual(doc["records"], 5)
        (y,) = doc["cases"]["y"]["points"]
        self.assertEqual((y["runs"], y["passes"], y["judged"]), (0, 0, {}), "unusable numbers are zero and an unusable metric is absent")
        self.assertEqual(y["night"], "at:2026-09-17T06:00:00Z")

    def test_brief_json_carries_the_trend_block_and_store_json_is_copied_only_when_given(self):
        data = load_fixture()
        data["generated_at"] = NOW
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "store.json").write_text(json.dumps(store_doc(night_one_records())))
            with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=frozenset()), \
                    unittest.mock.patch.object(render, "demotion_dates", return_value={}), \
                    unittest.mock.patch.object(render, "recent_merges", return_value=None):
                out = render_to(root / "with", data, extra_args=["--store", str(root / "store.json")])
                without = render_to(root / "without", data)
                broken = (root / "broken.json")
                broken.write_text("{not json")
                unreadable = render_to(root / "unreadable", data, extra_args=["--store", str(broken)])
            brief = json.loads((out / "brief.json").read_text())
            self.assertEqual(brief["trend"]["records"], 4)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["brief.json", "cases.html", "data.json", "grid.html", "index.html", "nightly.html", "run.html", "store.json", "trend.html"])
            self.assertEqual(json.loads((out / "store.json").read_text())["records"][0]["case"], "agent-kanban-smoke")
            self.assertIsNone(json.loads((without / "brief.json").read_text())["trend"]["source"])
            self.assertFalse((without / "store.json").exists())
            self.assertFalse((unreadable / "store.json").exists(), "a store that did not parse is not republished over the good prior")
            self.assertIsNone(json.loads((unreadable / "brief.json").read_text())["trend"]["source"])


@unittest.skipUnless(chrome(), "headless Chrome not found")
class TrendPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        base = night_one_records()
        records = list(base)
        for day in range(18, 24):
            records += later_night(base, day, f"21007{day}0000000000000", passes={"rca-remediation-pr": 3 if day % 2 else 2}, judged={"OutcomeValidity": 0.6 + day / 100})
        records += later_night(base, 24, "2100724000000000000", judged={"OutcomeValidity": 0.95}, key={"judge_model": "gemini-3.5-pro"})
        (root / "store.json").write_text(json.dumps(store_doc(records)))
        data = load_fixture()
        data["generated_at"] = NOW
        data["cases"] = [{"name": n, "domain": d, "active": True, "nightly_active": True} for n, d in DOMAINS]
        data["cases"].append({"name": "<img src=x onerror=alert(2)>", "domain": "cost", "active": True, "nightly_active": True})
        data["runs"].append(copy.deepcopy(NIGHT_1_RUN))
        # Health history with one past incident on the crashloop case, for the Brief's link.
        history = history_lines(
            dict(health_doc("GREEN"), tick="2026-09-06T01:00:00+00:00", since="2026-09-06T01:00:00+00:00"),
            dict(health_doc(), tick="2026-09-08T09:00:00+00:00"),
            dict(health_doc("GREEN"), tick="2026-09-08T12:00:00+00:00", since="2026-09-08T12:00:00+00:00"),
        )
        with unittest.mock.patch.object(render.classify, "admitted_cases", return_value=frozenset()), \
                unittest.mock.patch.object(render, "demotion_dates", return_value={}), \
                unittest.mock.patch.object(render, "recent_merges", return_value=None):
            cls.out = render_to(root / "site", data, health=health_doc("GREEN"), history=history, extra_args=["--store", str(root / "store.json")])
            cls.bare = render_to(root / "bare", data, health=health_doc("GREEN"))
            # A read that failed this tick: the prior document with error set.
            stale = store_doc(records, error="2026-09-17T14:30:00Z: gsutil ls gs://kube-agents-evals-bench/evidence: AccessDeniedException: 403")
            (root / "stale.json").write_text(json.dumps(stale))
            cls.stale = render_to(root / "stale", data, health=health_doc("GREEN"), extra_args=["--store", str(root / "stale.json")])
            empty = store_doc([])
            (root / "empty.json").write_text(json.dumps(empty))
            cls.empty = render_to(root / "empty", data, health=health_doc("GREEN"), extra_args=["--store", str(root / "empty.json")])
        cls.page = cls.out / "trend.html"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_overview_draws_every_domain_with_two_charts_and_a_table_twin(self):
        app = dom_text(self.page)
        self.assertIn("<h1>Scores over time on main</h1>", app)
        self.assertIn("Pass rate is the gate's number", app)
        self.assertIn("Judged quality is advisory", app)
        self.assertIn('href="https://github.com/gke-labs/kube-agents/blob/main/docs/designs/eval-scorer.md#what-a-score-is"', app)
        self.assertIn("Store <code>gs://kube-agents-evals-bench/evidence</code> read Thu 10:09 AM ET · 32 records over 8 nights inside the last 90 days", app)
        self.assertIn('<button type="button" data-scope="" class="on">all domains</button>', app)
        self.assertIn('<button type="button" data-scope="remediation">remediation</button>', app)
        self.assertIn('<button type="button" data-metric="OutcomeValidity" class="on">OutcomeValidity</button>', app)
        self.assertEqual(app.count('class="tcard"'), 4, "one card per domain")
        self.assertEqual(app.count('<svg class="tchart"'), 8, "two charts per card")
        self.assertIn('<a href="trend.html#domain=remediation">remediation</a><small>1 case pooled per night</small>', app)
        self.assertIn("95% bar", app)
        self.assertIn("key: judge_model", app, "the version-key marker is labelled with what changed")
        self.assertIn("Table view · 8 nights", app)
        self.assertIn('<th>Night</th><th>Cases</th><th>Pass rate</th><th>Trailing window</th><th>OutcomeValidity</th><th>Spread</th><th>Version key</th>', app)
        self.assertIn(f'<a href="nightly.html#build={NIGHT_1_BUILD}">Wed 8:00 PM ET</a>', app, "night one is dated by its run and links the report")
        self.assertIn(" ET", app)
        self.assertNotIn(" UTC", app)
        self.assertNotIn(".html?", app)
        self.assertNotIn("<img src=x", app)

    def test_a_case_view_draws_the_window_the_band_and_the_record(self):
        app = dom_text(self.page, fragment="#cases=rca-remediation-pr")
        self.assertIn("<h1><code>rca-remediation-pr</code> on main</h1>", app)
        self.assertEqual(app.count('class="tcard"'), 1)
        self.assertIn('<path class="line"', app, "the trailing window and the judged means are lines once there are two nights")
        self.assertIn('<path class="band"', app, "the spread band spans the nights at one key")
        self.assertIn('class="dot lone"', app, "the night after the key change is a lone point")
        self.assertIn("one night, no spread yet", app)
        self.assertIn("<b>Record today: collecting.</b> 2/3 across 1 night at the current key, 17 more runs before the window is full", app)
        self.assertIn("<b>Version key changed</b>", app)
        self.assertIn("<b>judge_model</b> changed", app)
        self.assertIn('href="cases.html#rca-remediation-pr"', app)
        self.assertIn('href="trend.html#domain=remediation"', app)
        self.assertIn("trailing window: 81% (17/21) across 7 nights", app, "the tooltip carries the pooled window")
        self.assertIn("range of the last 7 nightly means: 0.77 – 0.83", app)
        self.assertIn(f"key {KEY_1}", app)

    def test_a_domain_view_pools_then_lists_each_case(self):
        app = dom_text(self.page, fragment="#domain=cluster-debugging")
        self.assertIn("<h1>cluster debugging on main</h1>", app)
        self.assertIn('<button type="button" data-scope="cluster-debugging" class="on">cluster debugging</button>', app)
        self.assertIn("<h2>Each case in cluster debugging</h2>", app)
        self.assertIn('<a href="trend.html#cases=cluster-agent-crashloop-debug"><code>cluster-agent-crashloop-debug</code></a>', app)
        self.assertEqual(app.count('class="tcard"'), 2)

    def test_the_incident_link_from_the_brief_marks_the_night_it_started(self):
        brief_app = dom_text(self.out / "index.html")
        link = "trend.html#since=2026-09-08T09:00:00Z&until=2026-09-08T12:00:00Z&cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain,cluster-agent-crashloop-misleading-symptom"
        self.assertIn(f'<a href="{link.replace("&", "&amp;")}">The record on main around the night it started →</a>', brief_app)
        app = dom_text(self.page, fragment="#" + link.split("#", 1)[1])
        self.assertIn("<h1>3 cases on main</h1>", app)
        self.assertIn("Marked: the incident that started Sep 8, 5:00 AM ET and ended Sep 8, 8:00 AM ET.", app)
        self.assertIn(">incident start</text>", app)
        self.assertIn(">healthy again</text>", app)
        self.assertIn("No record in the store for these cases inside the window", app)
        self.assertIn("<code>cluster-agent-crashloop-evidence-chain</code>, <code>cluster-agent-crashloop-misleading-symptom</code>", app)

    def test_the_cases_page_links_each_case_to_its_trend(self):
        app = dom_text(self.out / "cases.html")
        self.assertIn('<small><a href="trend.html#cases=rca-remediation-pr">trend on main →</a></small>', app)
        self.assertNotIn('trend.html#cases=<img', app)

    def test_chips_change_the_scope_and_the_metric(self):
        clicked = dom_text(clicked_page(self.page, 'button[data-scope="upgrades"]'))
        self.assertIn("<h1>upgrades on main</h1>", clicked)
        clicked = dom_text(clicked_page(self.page, 'button[data-metric="ToolInvocation"]'))
        self.assertIn("Judged ToolInvocation by night · advisory", clicked)
        self.assertNotIn("Judged OutcomeValidity by night", clicked)
        # A metric the store does not carry falls back to the default; an unknown case says so.
        self.assertIn("Judged OutcomeValidity by night", dom_text(self.page, fragment="#metric=Nope"))
        self.assertIn("No record in the store for this case inside the window", dom_text(self.page, fragment="#cases=no-such-case"))

    def test_no_store_a_failed_read_and_an_empty_store_say_so(self):
        bare = dom_text(self.bare / "trend.html")
        self.assertIn("STORE NOT READ", bare)
        self.assertIn("was not read for this render", bare)
        self.assertNotIn('class="tcard"', bare)
        stale = dom_text(self.stale / "trend.html")
        self.assertIn("STALE READ", stale)
        self.assertIn("this page shows the last good read, Thu 10:09 AM ET", stale)
        self.assertEqual(stale.count('class="tcard"'), 4, "the last good read is still drawn")
        empty = dom_text(self.empty / "trend.html")
        self.assertIn("holds no record inside its 90-day window yet", empty)
        self.assertNotIn('class="tcard"', empty)

    def test_the_page_carries_its_data_inline_and_the_nav_tab(self):
        page = self.page.read_text()
        self.assertIn('data-page="trend"', page)
        self.assertIn('<a href="trend.html" class="on">Trend</a>', page)
        self.assertIn('<a href="trend.html" >Trend</a>', (self.out / "index.html").read_text())
        self.assertNotIn("<", re.search(r'id="inline-brief">(.*?)</script>', page, re.DOTALL).group(1))


if __name__ == "__main__":
    unittest.main()
