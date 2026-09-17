"""trend.py: the Trend page's document from the evidence store's records, and
-- when headless Chrome is present -- trend.html, the Cases page's link to it
and the Brief's last-incident link, as a browser renders them.

The records are the four real objects of night one (testdata_store/, read
through store.py's parser) plus synthetic later nights, so the trailing
window, the spread band and a version-key change all have something to
draw. Every asserted time is America/Toronto.
"""

import copy
import itertools
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
    dom_html,
    dom_text,
    health_doc,
    history_lines,
    inline_blob,
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


def scripted_page(page: pathlib.Path, script: str) -> pathlib.Path:
    """A copy of the rendered page that runs ``script`` after the page's own
    script rendered (test_eval_dashboard_pages.clicked_page, for a
    sequence rather than one click)."""
    copy_path = page.with_name(page.stem + "-scripted" + page.suffix)
    copy_path.write_text(page.read_text().replace("</body>", f"<script>{script}</script></body>", 1))
    return copy_path


def data_with_nightly(cases, runs=()):
    return {"schema_version": 1, "generated_at": NOW,
            "cases": [{"name": n, "domain": d, "active": True, "nightly_active": True} for n, d in cases],
            "runs": list(runs)}


HOSTILE_CASE = "<img src=x onerror=alert(2)>"
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
        self.assertEqual(point["window"], {"runs": 3, "passes": 2, "lines": 1, "full": False, "cut": False})
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


class LeadInTest(unittest.TestCase):
    """The read reaches ``lead_days`` past the drawn window (store.py): those
    nights pool into the first drawn nights' windows and spreads and are
    drawn nowhere; a window that still ran out at the read's edge is
    ``cut``, not ``collecting``. Read at Thu 2026-09-17 14:09 UTC with a
    10-day window and a 14-day lead-in: drawn from 09-07 14:09, read from
    08-24 14:09."""

    @classmethod
    def setUpClass(cls):
        base = night_one_records()
        records = []
        for day in range(1, 7):  # six nights in the lead-in
            records += later_night(base, day, f"21007{day:02d}0000000000000", judged={"OutcomeValidity": 0.5 + day / 100})
        for day in (8, 9, 10):  # three drawn nights
            records += later_night(base, day, f"21007{day:02d}0000000000000", judged={"OutcomeValidity": 0.9})

        def one(case, at, build):
            r = copy.deepcopy(base[0])  # agent-kanban-smoke: 3 of 3
            r.update(case=case, recorded_at=at, build=build, object=f"{LOCATION}/{case}/x/{build}.jsonl")
            return r

        extra = [
            one("gone-case", "2026-09-03T05:50:00Z", "2100903000000000001"),  # lead-in only
            one("sparse-case", "2026-08-25T05:50:00Z", "2100825000000000002"),  # at the read's edge...
            one("sparse-case", "2026-09-09T05:50:00Z", "21007090000000000000"),  # ...so this pool may go on past it
            one("young-case", "2026-09-05T05:50:00Z", "2100905000000000003"),  # started well inside the read...
            one("young-case", "2026-09-09T05:50:00Z", "21007090000000000000"),  # ...so this pool is genuinely short
        ]
        cls.doc = trend.trend_document(store_doc(records + extra, window_days=10, lead_days=14), data_with_nightly(DOMAINS))

    def test_the_lead_in_is_pooled_and_not_drawn(self):
        doc = self.doc
        self.assertEqual((doc["window_days"], doc["lead_days"]), (10, 14))
        self.assertEqual(sorted(doc["cases"]), sorted([n for n, _ in DOMAINS] + ["sparse-case", "young-case"]), "a case recorded in the lead-in only is not on the page")
        kanban = doc["cases"]["agent-kanban-smoke"]
        self.assertEqual([p["at"][:10] for p in kanban["points"]], ["2026-09-08", "2026-09-09", "2026-09-10"])
        self.assertEqual(kanban["points"][0]["window"], {"runs": 21, "passes": 21, "lines": 7, "full": True, "cut": False}, "the first drawn night pools the six lead-in nights before it")
        self.assertEqual(kanban["points"][0]["judged"]["OutcomeValidity"]["spread"]["nights"], 7)
        self.assertEqual(kanban["record"]["state"], "would-admit")
        self.assertEqual(doc["records"], 3 * 4 + 2)
        self.assertEqual([n["at"][:10] for n in doc["nights"]], ["2026-09-08", "2026-09-09", "2026-09-10"])
        self.assertEqual(doc["domains"]["chat-and-routing"]["points"][0]["at"][:10], "2026-09-08")

    def test_a_pool_that_ran_out_at_the_reads_edge_is_cut_and_one_that_ran_out_inside_it_is_collecting(self):
        sparse = self.doc["cases"]["sparse-case"]
        self.assertEqual([p["at"][:10] for p in sparse["points"]], ["2026-09-09"])
        self.assertEqual(sparse["points"][0]["window"], {"runs": 6, "passes": 6, "lines": 2, "full": False, "cut": True})
        self.assertEqual((sparse["record"]["state"], sparse["record"]["runs"], sparse["record"]["lines"]), ("cut", 6, 2))
        young = self.doc["cases"]["young-case"]
        self.assertEqual(young["points"][0]["window"], {"runs": 6, "passes": 6, "lines": 2, "full": False, "cut": False})
        self.assertEqual(young["record"]["state"], "collecting")

    def test_a_store_without_a_read_time_or_lead_draws_everything(self):
        base = night_one_records()
        doc = trend.trend_document(store_doc(base, read_at="junk"), data_with_nightly(DOMAINS))
        self.assertEqual((doc["read_at"], doc["window_days"], doc["lead_days"], doc["records"]), (None, 90, 0, 4))
        self.assertFalse(doc["cases"]["agent-kanban-smoke"]["points"][0]["window"]["cut"], "with no read edge nothing is cut")


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

    def test_trend_json_carries_the_trend_block_and_store_json_is_copied_only_when_given(self):
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
            self.assertIsNone(brief["trend"], "every page polls brief.json; the block rides in trend.json")
            self.assertEqual(json.loads((out / "trend.json").read_text())["records"], 4)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["brief.json", "cases.html", "data.json", "grid.html", "index.html", "nightly.html", "run.html", "store.json", "trend.html", "trend.json"])
            self.assertEqual(json.loads((out / "store.json").read_text())["records"][0]["case"], "agent-kanban-smoke")
            self.assertIsNone(json.loads((without / "trend.json").read_text())["source"])
            self.assertFalse((without / "store.json").exists())
            self.assertFalse((unreadable / "store.json").exists(), "a store that did not parse is not republished over the good prior")
            self.assertIsNone(json.loads((unreadable / "trend.json").read_text())["source"])


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
        data["cases"].append({"name": HOSTILE_CASE, "domain": "cost", "active": True, "nightly_active": True})
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
            # A read that failed this tick: the prior document with error set,
            # a cap that trimmed one case and a line that would not parse.
            stale = store_doc(records, error="2026-09-17T14:30:00Z: gsutil ls gs://kube-agents-evals-bench/evidence: AccessDeniedException: 403",
                              truncated={"rca-remediation-pr": 2}, warnings=["gs://kube-agents-evals-bench/evidence/x/1.jsonl: not valid JSON: Expecting value"])
            (root / "stale.json").write_text(json.dumps(stale))
            cls.stale = render_to(root / "stale", data, health=health_doc("GREEN"), extra_args=["--store", str(root / "stale.json")])
            empty = store_doc([])
            (root / "empty.json").write_text(json.dumps(empty))
            cls.empty = render_to(root / "empty", data, health=health_doc("GREEN"), extra_args=["--store", str(root / "empty.json")])
            # A month of nights at one key (the density a quarter's chart works
            # at; rca fails two of three every night), read with a lead-in,
            # plus a sparse case whose pool runs out at the read's edge.
            # A key change on night 11; night 20 recorded no OutcomeValidity.
            dense = []
            for day in range(1, 31):
                night = later_night(base, day, f"21007{day:02d}0000000000000", passes={"rca-remediation-pr": 1}, key={"judge_model": "gemini-3.5-pro"} if day >= 11 else None)
                if day == 20:
                    for r in night:
                        r["judged"].pop("OutcomeValidity")
                dense += night
            for at, build in (("2026-06-20T05:50:00Z", "2100620000000000009"), ("2026-09-15T05:50:00Z", "21007150000000000000")):
                r = copy.deepcopy(base[0])
                r.update(case="sparse-case", recorded_at=at, build=build, object=f"{LOCATION}/sparse-case/x/{build}.jsonl")
                dense.append(r)
            # A record whose own case name is hostile (parse_records only asks
            # for a string): the store is the one input that reaches the page.
            hostile = copy.deepcopy(base[0])
            hostile.update(case=HOSTILE_CASE, recorded_at="2026-09-30T05:50:00Z", build="21007300000000000000", object=f"{LOCATION}/hostile/x/30.jsonl")
            dense.append(hostile)
            (root / "dense.json").write_text(json.dumps(store_doc(dense, read_at="2026-10-01T14:09:02Z", lead_days=14)))
            cls.dense = render_to(root / "dense", data, health=health_doc("GREEN"), extra_args=["--store", str(root / "dense.json")])
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
        self.assertIn(f'<a href="nightly.html#build={NIGHT_1_BUILD}">Thu 1:54 AM ET</a>', app, "night one is dated by its record (recorded_at, the stamp its bar is placed by), not by the run's start, and links the report")
        self.assertNotIn("Wed 8:00 PM ET", app, "the run's start is not a second date for the same night")
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
        # The marker is dated like the bars: its x is a night's x, not a
        # point between two nights.
        marker = re.search(r'<line class="keym" x1="([0-9.]+)"', app)
        self.assertIsNotNone(marker, "the key change is marked")
        self.assertIn(f'cx="{marker.group(1)}"', app, "the key-change marker sits on the night it marks")
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

    def test_the_record_line_and_the_window_labels_say_admit_demote_and_cut_on_the_page(self):
        admit = dom_text(self.dense / "trend.html", fragment="#cases=agent-kanban-smoke")
        self.assertIn("<b>Record today: the record would admit it.</b> 21/21 across 7 nights at the current key against a bar of 95% over 20", admit)
        self.assertIn("100% of 21</text>", admit, "the window label of a full window carries no caveat")
        demote = dom_text(self.dense / "trend.html", fragment="#cases=rca-remediation-pr")
        self.assertIn("<b>Record today: the record would demote it.</b> 7/21 across 7 nights at the current key against a bar of 95% over 20", demote)
        # The sparse case: one record at the read's edge (06-20, the read began 06-19), one drawn.
        cut = dom_text(self.dense / "trend.html", fragment="#cases=sparse-case")
        self.assertIn("<b>Record today: not knowable from this read.</b> 6/6 across 2 nights at the current key inside this read; the store may hold older records at this key that admission pools and this page did not read", cut)
        self.assertIn("100% of 6 · window reaches past this read</text>", cut, "the chart label")
        self.assertIn("trailing window: 100% (6/6) across 2 nights · reaches past this read", cut, "the tooltip")
        self.assertIn("<td>100% (6/6) · past this read</td>", cut, "the table twin")
        self.assertNotIn("window not full", cut)
        self.assertNotIn(" · not full yet", cut, "the legend's '(hollow: not full yet)' is the only mention")
        # A short pool that started inside the read stays "not full" / collecting (the main fixture's eighth night).
        short = dom_text(self.page, fragment="#cases=rca-remediation-pr")
        self.assertIn("<b>Record today: collecting.</b> 2/3 across 1 night at the current key, 17 more runs before the window is full", short)
        self.assertIn("67% of 3 · window not full</text>", short)
        self.assertIn(" · not full yet", short)
        self.assertNotIn("past this read", short)

    def test_the_stale_banner_also_names_a_trimmed_case_and_an_unreadable_line(self):
        stale = dom_text(self.stale / "trend.html")
        self.assertIn('<p class="stale">The read was capped at 200 objects per case per version key for 1 case (rca-remediation-pr); their oldest nights inside the window are not drawn.</p>', stale)
        self.assertIn('<p class="stale">1 record in the store could not be read and is left out: <code>gs://kube-agents-evals-bench/evidence/x/1.jsonl: not valid JSON: Expecting value</code></p>', stale)
        self.assertNotIn('class="stale"', dom_text(self.page), "a clean read carries neither note")

    def test_the_polls_re_render_keeps_an_open_table_view_and_the_focused_night(self):
        # The page's own script rendered; open the first table view and
        # focus a night, then let the poll's renderAll (the failed fetch
        # from file://) rebuild #app inside the virtual-time budget.
        script = ('document.querySelector("details.tv").open = true;'
                  'document.querySelector(\'[data-hit="case:rca-remediation-pr:rate:2"]\').focus();'
                  'setTimeout(() => { const a = document.activeElement; document.body.dataset.focused = a && a.dataset ? a.dataset.hit || "other" : "none";'
                  ' document.body.dataset.tip = document.getElementById("ttip").style.display; }, 1500);')
        html = dom_html(scripted_page(self.page, script), fragment="#cases=rca-remediation-pr")
        self.assertIn('<details class="tv" data-tv="case:rca-remediation-pr" open="">', html)
        self.assertIn('data-focused="case:rca-remediation-pr:rate:2"', html)
        self.assertIn('data-tip="block"', html, "the focused night's tooltip is showing again")

    def test_the_band_and_the_window_line_break_at_a_key_change_and_the_band_at_an_unrecorded_night(self):
        app = dom_text(self.dense / "trend.html", fragment="#cases=agent-kanban-smoke")
        rate, judged = re.findall(r'<svg class="tchart".*?</svg>', app, re.DOTALL)[:2]
        self.assertIn("key: judge_model", app)
        self.assertEqual(rate.count('<path class="line"'), 2, "the trailing-window line restarts at the new key")
        self.assertEqual(judged.count('<path class="band"'), 3, "nights 2-10 at key 1, 12-19 and 21-30 at key 2: the lone night 11 and the unrecorded night 20 break it")
        self.assertEqual(judged.count('<path class="line"'), 2, "the line of means breaks at the unrecorded night only")
        self.assertEqual(judged.count('class="dot lone"'), 2, "night 1 and night 11, each the first at its key")
        # No band crosses the key marker: every band's x range is on one side of it.
        marker = float(re.search(r'<line class="keym" x1="([0-9.]+)"', judged).group(1))
        for d in re.findall(r'<path class="band" d="([^"]+)"', judged):
            xs = [float(v) for v in re.findall(r'[ML]([0-9.]+),', d)]
            self.assertTrue(max(xs) < marker or min(xs) > marker, (min(xs), max(xs), marker))
        self.assertIn("OutcomeValidity: not recorded that night", judged)

    def test_a_hostile_case_name_in_a_store_record_renders_as_text_everywhere(self):
        # Reached through its domain: the case-id grammar keeps the name out
        # of the fragment, so the domain view is the only route to its card.
        app = dom_text(self.dense / "trend.html", fragment="#domain=cost")
        name = "&lt;img src=x onerror=alert(2)&gt;"
        self.assertIn("<h1>cost on main</h1>", app)
        # The case-id grammar drops the name from the link, so its title links the overview.
        self.assertIn(f'<h3><a href="trend.html#"><code>{name}</code></a></h3>', app)
        self.assertIn(f'aria-label="{name}: pass rate by night"', app, "the chart's accessible name is text, not markup")
        self.assertIn(f'data-hit="case:{name}:rate:0"', app)
        self.assertIn(f'data-tv="case:{name}"', app)
        self.assertIn("Table view · 1 night", app)
        self.assertNotIn("<img", app)
        self.assertNotIn("alert(2)>", app)
        # The domain card pools it: one case, and the chip for its domain is text too.
        self.assertIn("<small>1 case pooled per night</small>", app)

    def test_hit_targets_are_disjoint_and_cover_the_plot_at_a_months_density(self):
        # SVG hit-testing returns the topmost element: overlapping rects
        # would name a later night than the one under the pointer.
        app = dom_text(self.dense / "trend.html", fragment="#cases=agent-kanban-smoke")
        svg = re.search(r'<svg class="tchart"[^>]*pass rate by night.*?</svg>', app, re.DOTALL).group(0)
        rects = [(float(x), float(w)) for x, w in re.findall(r'<rect class="hit" tabindex="0" data-hit="[^"]*" x="([0-9.]+)" y="\d+" width="([0-9.]+)"', svg)]
        self.assertEqual(len(rects), 30)
        self.assertIn('data-hit="case:agent-kanban-smoke:rate:0"', svg)
        for (x0, w0), (x1, _) in itertools.pairwise(rects):
            self.assertLessEqual(x0 + w0, x1 + 0.11, "adjacent hit targets overlap")
            self.assertGreaterEqual(x0 + w0, x1 - 0.11, "a gap between adjacent hit targets")
        self.assertEqual(rects[0][0], 38.0, "the first target starts at the plot's left edge")
        self.assertAlmostEqual(rects[-1][0] + rects[-1][1], 506.0, places=0)
        self.assertTrue(all(w > 0 for _, w in rects))

    def test_a_chip_writes_the_link_so_the_pages_own_links_still_navigate(self):
        # A scope chip, then the domain view's case title: the title wins,
        # because the chip wrote the fragment instead of pinning a state
        # the hashchange would not clear.
        script = ('document.querySelector(\'button[data-scope="upgrades"]\').click();'
                  'window.addEventListener("hashchange", () => { document.querySelector(\'a[href="trend.html#cases=upgrades-fleet-version-table"]\').click(); }, { once: true });')
        app = dom_text(scripted_page(self.page, script))
        self.assertIn("<h1><code>upgrades-fleet-version-table</code> on main</h1>", app)
        self.assertNotIn("<h1>upgrades on main</h1>", app)
        # A metric chip keeps the scope it was clicked in and lands in the fragment
        # (recorded beside #app: the poll's re-render would wipe it inside).
        script = ('document.querySelector(\'button[data-metric="ToolInvocation"]\').click();'
                  'window.addEventListener("hashchange", () => { document.getElementById("app").insertAdjacentHTML("afterend", `<p id="where">${location.hash}</p>`); }, { once: true });')
        app = dom_text(scripted_page(self.page, script), fragment="#domain=remediation")
        self.assertIn("<h1>remediation on main</h1>", app)
        self.assertIn("Judged ToolInvocation by night · advisory", app)
        self.assertIn('<p id="where">#domain=remediation&amp;metric=ToolInvocation</p>', app)

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
        # The trend block rides inside trend.html and trend.json only; the
        # five other pages never read it, and brief.json, which every page
        # polls every minute, does not carry it either.
        brief = json.loads((self.out / "brief.json").read_text())
        trend_json = json.loads((self.out / "trend.json").read_text())
        self.assertIsNone(brief["trend"])
        self.assertEqual(trend_json["records"], 32)
        self.assertEqual(inline_blob(page, render.INLINE_BRIEF_ID), {**brief, "trend": trend_json}, "trend.html carries the whole document, trend block included")
        for name in ("index.html", "run.html", "grid.html", "cases.html", "nightly.html"):
            inlined = inline_blob((self.out / name).read_text(), render.INLINE_BRIEF_ID)
            self.assertIsNone(inlined["trend"], f"{name}: no trend block inlined")
            self.assertEqual({**inlined, "trend": brief["trend"]}, brief, f"{name}: everything else is brief.json")


if __name__ == "__main__":
    unittest.main()
