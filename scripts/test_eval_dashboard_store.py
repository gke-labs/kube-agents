"""store.py reads the eval evidence store into store.json for the Trend page.

The fixture under scripts/eval_dashboard/testdata_store/ is four REAL objects
from the store's first recording night (2026-09-17, SCHEMA.md "Fixtures"),
in the store's own layout. A fake gcloud serves them: `ls` walks the tree,
`cat` concatenates the files, so the reader is exercised end to end without
a bucket. What is pinned: the parse and the object attribution, the window
and the per-key cap, the incremental read against a prior, and the failure
posture (a malformed line is a warning, an unreachable listing keeps the
prior).
"""

import datetime
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr

from eval_dashboard import store

FIXTURE = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_store" / "evidence"
LOCATION = "gs://kube-agents-evals-bench/evidence"
BUILD = "2100374258805903360"
KEY_DIR = "gemini-3-1-pro-preview-kubeagents-mcp/gemini-3.1-pro-preview/v1-f1-v1"
NAME = f"2026-09-17T05-54-31Z-{BUILD}.jsonl"
CASES = ["agent-kanban-smoke", "cluster-agent-crashloop-debug", "rca-remediation-pr", "upgrades-fleet-version-table"]
NOW = datetime.datetime(2026, 9, 17, 14, 0, tzinfo=datetime.timezone.utc)
UTC = datetime.timezone.utc


def url_of(case, name=NAME, key_dir=KEY_DIR):
    return f"{LOCATION}/{case}/{key_dir}/{name}"


class FakeGcloud:
    """A gcloud whose `storage` group serves a dict of {url: text}:
    `ls <prefix>/**` lists the urls under the prefix, `cat` concatenates the
    objects in argument order. Records every call so a test can count
    listings and fetches. `fail_urls` fail a cat whole, with exit 1, as a
    5xx does; `absent_urls` are objects deleted since the listing, which
    `gcloud storage cat` names on stderr and exits 1 on, after printing the
    others."""

    def __init__(self, objects=None, fail_ls=None, fail_cat=None, fail_urls=(), absent_urls=()):
        self.objects = dict(objects if objects is not None else fixture_objects())
        self.fail_ls = fail_ls
        self.fail_cat = fail_cat
        self.fail_urls = set(fail_urls)
        self.absent_urls = set(absent_urls)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[:2] == ["gcloud", "storage"], argv
        args = argv[2:]
        if args[0] == "ls":
            if self.fail_ls:
                return subprocess.CompletedProcess(argv, 1, "", self.fail_ls)
            prefix = args[1][: -len("/**")]
            hits = sorted(u for u in self.objects if u.startswith(prefix + "/"))
            if not hits:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: (gcloud.storage.ls) One or more URLs matched no objects.")
            return subprocess.CompletedProcess(argv, 0, "\n".join(hits) + "\n", "")
        if args[0] == "cat":
            if self.fail_cat or self.fail_urls.intersection(args[1:]):
                return subprocess.CompletedProcess(argv, 1, "".join(self.objects.get(u, "") for u in args[1:] if u not in self.fail_urls), self.fail_cat or "HTTPError 503: Service Unavailable")
            absent = [u for u in args[1:] if u in self.absent_urls]
            if absent:
                text = "".join(self.objects[u] for u in args[1:] if u not in self.absent_urls)
                err = "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files:\n" + "\n".join(absent)
                return subprocess.CompletedProcess(argv, 1, text, err)
            return subprocess.CompletedProcess(argv, 0, "".join(self.objects[u] for u in args[1:]), "")
        raise AssertionError(f"unexpected gcloud storage call {argv}")

    @property
    def cats(self):
        return [c for c in self.calls if c[2] == "cat"]

    @property
    def listings(self):
        return [c for c in self.calls if c[2] == "ls"]


def fixture_objects():
    out = {}
    for path in sorted(FIXTURE.rglob("*.jsonl")):
        relative = path.relative_to(FIXTURE).as_posix()
        out[f"{LOCATION}/{relative}"] = path.read_text(encoding="utf-8")
    return out


def read(gcloud, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("max_objects", 200)
    return store.read_store(LOCATION, gcloud="gcloud", runner=gcloud, **kwargs)


class FixtureTest(unittest.TestCase):
    def test_the_fixture_is_the_store_layout_one_record_per_object(self):
        objects = fixture_objects()
        self.assertEqual(sorted(objects), [url_of(c) for c in CASES])
        for url, text in objects.items():
            self.assertTrue(text.endswith("\n"), f"{url}: the writer ends every object with a newline")
            (line,) = text.splitlines()
            doc = json.loads(line)
            self.assertEqual(doc["recorded_at"], "2026-09-17T05:54:31Z")
            self.assertEqual(doc["commit"][:8], "b458323d")
            self.assertEqual(doc["key"], {"setup_id": "gemini-3-1-pro-preview-kubeagents-mcp", "scoring_version": "v1", "judge_model": "gemini-3.1-pro-preview", "fleet": 1, "verifiers": 1})
            self.assertEqual(doc["runs"], 3)
            self.assertEqual(set(doc["judged"]), {"OutcomeValidity", "ToolInvocation", "OutcomeScore"})


class ReadStoreTest(unittest.TestCase):
    def test_a_cold_read_lists_once_fetches_everything_and_attributes_each_record(self):
        gcloud = FakeGcloud()
        doc = read(gcloud)
        self.assertEqual(len(gcloud.listings), 1)
        self.assertEqual(len(gcloud.cats), 1, "four objects fit one cat")
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["source"], LOCATION)
        self.assertEqual(doc["read_at"], "2026-09-17T14:00:00Z")
        self.assertEqual((doc["listed"], doc["fetched"], doc["truncated"], doc["warnings"], doc["error"]), (4, 4, {}, [], None))
        self.assertEqual([r["case"] for r in doc["records"]], CASES, "oldest first, then by case")
        for record in doc["records"]:
            self.assertEqual(record["object"], url_of(record["case"]))
            self.assertEqual(record["build"], BUILD)
        rca = next(r for r in doc["records"] if r["case"] == "rca-remediation-pr")
        self.assertEqual((rca["runs"], rca["passes"]), (3, 2))
        self.assertAlmostEqual(rca["judged"]["OutcomeValidity"]["mean"], 0.7667, places=3)

    def test_the_incremental_read_fetches_only_what_the_prior_lacks(self):
        first = read(FakeGcloud())
        # Night two appends one object per case; the prior holds night one.
        objects = fixture_objects()
        night2 = "2026-09-18T05-50-00Z-2100736000000000000.jsonl"
        for case in CASES:
            line = json.loads(objects[url_of(case)])
            line.update(recorded_at="2026-09-18T05:50:00Z", passes=3)
            objects[url_of(case, night2)] = json.dumps(line) + "\n"
        gcloud = FakeGcloud(objects)
        second = read(gcloud, prior=first)
        self.assertEqual(len(gcloud.listings), 1)
        self.assertEqual(second["fetched"], 4, "night one came from the prior")
        self.assertEqual(sorted(u for c in gcloud.cats for u in c[3:]), [url_of(c, night2) for c in CASES])
        self.assertEqual(len(second["records"]), 8)
        self.assertEqual([r["build"] for r in second["records"]][-1], "2100736000000000000")
        # A prior from another location is not trusted for this one.
        other = dict(first, source="gs://elsewhere/evidence")
        gcloud = FakeGcloud(objects)
        self.assertEqual(read(gcloud, prior=other)["fetched"], 8)

    def test_the_window_and_the_per_key_cap_bound_what_is_fetched_and_say_so(self):
        objects = {}
        for day in range(1, 6):
            for case in ("case-a", "case-b"):
                name = f"2026-09-{day:02d}T05-00-00Z-{day}.jsonl"
                objects[url_of(case, name)] = json.dumps({"case": case, "recorded_at": f"2026-09-{day:02d}T05:00:00Z", "key": {"setup_id": "s", "scoring_version": "v1", "judge_model": "j", "fleet": 1, "verifiers": 1}, "runs": 3, "passes": 3}) + "\n"
        # An older key directory for case-a: capped on its own, never against the current one.
        objects[url_of("case-a", "2026-09-05T04-00-00Z-9.jsonl", "old-setup/j/v1-f1-v1")] = json.dumps({"case": "case-a", "recorded_at": "2026-09-05T04:00:00Z", "key": {"setup_id": "old-setup", "scoring_version": "v1", "judge_model": "j", "fleet": 1, "verifiers": 1}, "runs": 3, "passes": 0}) + "\n"
        # A stray object outside the layout is not read, and is named in a warning.
        objects[f"{LOCATION}/notes.jsonl"] = "{}\n"
        now = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
        doc = read(FakeGcloud(objects), now=now, window_days=3, lead_days=0, max_objects=2)
        # Window: 09-03 12:00 onwards keeps days 4 and 5 (day 3 05:00 is outside). Cap: two per key, so nothing more is cut here...
        self.assertEqual(doc["listed"], 12)
        self.assertEqual(doc["warnings"], [f"{LOCATION}/notes.jsonl: not in the store's layout (no stamp in the name); not read"], "the object this reader cannot place is named, since the gate's reader would pool it")
        self.assertEqual((doc["window_days"], doc["lead_days"]), (3, 0))
        self.assertEqual(doc["older"], {"case-a": {KEY_DIR: 3}, "case-b": {KEY_DIR: 3}}, "days 1-3 at the current key were listed and left behind; the old key's one object is inside the span")
        self.assertEqual(sorted((r["case"], r["recorded_at"]) for r in doc["records"]), [
            ("case-a", "2026-09-04T05:00:00Z"), ("case-a", "2026-09-05T04:00:00Z"), ("case-a", "2026-09-05T05:00:00Z"),
            ("case-b", "2026-09-04T05:00:00Z"), ("case-b", "2026-09-05T05:00:00Z")])
        self.assertEqual(doc["truncated"], {})
        # ...and with a wider window the cap binds per key and is reported per case.
        doc = read(FakeGcloud(objects), now=now, window_days=30, lead_days=0, max_objects=2)
        self.assertEqual(doc["truncated"], {"case-a": 3, "case-b": 3})
        self.assertEqual(doc["older"], {"case-a": {KEY_DIR: 3}, "case-b": {KEY_DIR: 3}}, "what the cap trimmed is left behind too, per key")
        self.assertEqual(len([r for r in doc["records"] if r["case"] == "case-a"]), 3, "two at the current key, one at the old one")
        # The lead-in reaches past the window: one more day brings day 3 in (the page pools it, does not draw it).
        doc = read(FakeGcloud(objects), now=now, window_days=3, lead_days=1, max_objects=5)
        self.assertEqual((doc["window_days"], doc["lead_days"]), (3, 1))
        self.assertEqual(sorted(r["recorded_at"][:10] for r in doc["records"] if r["case"] == "case-b"), ["2026-09-03", "2026-09-04", "2026-09-05"])

    def test_a_malformed_line_is_a_warning_and_the_rest_is_read(self):
        objects = fixture_objects()
        objects[url_of("rca-remediation-pr")] = "{not json\n"
        objects[url_of("agent-kanban-smoke")] = '{"case": "agent-kanban-smoke", "runs": 3}\n'
        doc = read(FakeGcloud(objects))
        self.assertEqual([r["case"] for r in doc["records"]], ["cluster-agent-crashloop-debug", "upgrades-fleet-version-table"])
        self.assertEqual(len(doc["warnings"]), 2)
        self.assertIn("not valid JSON", doc["warnings"][0] + doc["warnings"][1])
        self.assertIn("record without case, recorded_at or key", doc["warnings"][0] + doc["warnings"][1])
        # With the counts agreeing, each record still knows its object.
        for record in doc["records"]:
            self.assertEqual(record["object"], url_of(record["case"]))

    def test_a_multi_line_object_is_read_alone_so_every_record_carries_its_object(self):
        # Not the writer's rule (one record per object), but nothing forbids
        # it: the chunk's line count disagrees with its object count, so the
        # chunk is read again one object at a time.
        objects = fixture_objects()
        extra = json.dumps({"case": "agent-kanban-smoke", "recorded_at": "2026-09-16T05:00:00Z", "key": {"setup_id": "s", "scoring_version": "v1", "judge_model": "j", "fleet": 1, "verifiers": 1}, "runs": 3, "passes": 3}) + "\n"
        objects[url_of("agent-kanban-smoke")] = objects[url_of("agent-kanban-smoke")] + extra
        gcloud = FakeGcloud(objects)
        doc = read(gcloud)
        self.assertEqual(len(doc["records"]), 5)
        self.assertEqual([len(c) - 3 for c in gcloud.cats], [4, 1, 1, 1, 1])
        by = {(r["case"], r["recorded_at"]): r for r in doc["records"]}
        self.assertEqual(by[("agent-kanban-smoke", "2026-09-17T05:54:31Z")]["build"], BUILD)
        self.assertEqual(by[("agent-kanban-smoke", "2026-09-16T05:00:00Z")]["object"], url_of("agent-kanban-smoke"), "the second line is the object's too")
        self.assertEqual(by[("rca-remediation-pr", "2026-09-17T05:54:31Z")]["build"], BUILD)
        # The incremental read keeps both records of the object without a fetch.
        again = read(FakeGcloud(objects), prior=doc)
        self.assertEqual((len(again["records"]), again["fetched"]), (5, 0))
        # parse_records alone, handed a chunk it cannot re-read, still does not guess.
        warnings = []
        records = store.parse_records(objects[url_of("agent-kanban-smoke")] + objects[url_of("rca-remediation-pr")], [url_of("agent-kanban-smoke"), url_of("rca-remediation-pr")], LOCATION, warnings)
        self.assertEqual([r["object"] for r in records], [url_of("agent-kanban-smoke"), None, url_of("rca-remediation-pr")])

    def test_what_is_left_behind_is_keyed_by_the_writers_directories_and_the_flat_layout_does_not_crash(self):
        objects = {
            # A judge spelled with a slash is filed under its sanitised segment.
            url_of("case-a", "2026-05-01T05-00-00Z-1.jsonl", "s/vertex_ai-gemini-x/v1-f1-v1"): "{}\n",
            # A record without a key is filed under unkeyed/.
            url_of("case-a", "2026-05-02T05-00-00Z-2.jsonl", "unkeyed"): "{}\n",
            # The flat layout: directly under the case (the gate's reader groups by directory, whatever the depth).
            f"{LOCATION}/case-b/2026-05-03T05-00-00Z-3.jsonl": "{}\n",
            # One inside the span, so the read has something to read.
            url_of("case-b", "2026-09-16T05-00-00Z-4.jsonl"): json.dumps({"case": "case-b", "recorded_at": "2026-09-16T05:00:00Z", "key": {}}) + "\n",
        }
        doc = read(FakeGcloud(objects), lead_days=0)
        self.assertEqual(doc["older"], {"case-a": {"s/vertex_ai-gemini-x/v1-f1-v1": 1, "unkeyed": 1}, "case-b": {"": 1}})
        self.assertEqual([r["case"] for r in doc["records"]], ["case-b"])

    def test_an_empty_prefix_is_an_empty_store_and_a_failed_listing_raises(self):
        doc = read(FakeGcloud({}))
        self.assertEqual((doc["listed"], doc["records"], doc["error"]), (0, [], None))
        with self.assertRaises(RuntimeError):
            read(FakeGcloud(fail_ls="HTTPError 403"))

    def test_a_failed_cat_is_a_warning_per_object_and_the_next_chunk_is_still_read(self):
        doc = read(FakeGcloud(fail_cat="HTTPError 503"))
        self.assertEqual(doc["records"], [])
        self.assertEqual(len(doc["warnings"]), 4, "one warning per object the read lost, not per chunk")
        self.assertTrue(all(w.endswith(": gcloud storage cat failed: HTTPError 503") for w in doc["warnings"]), doc["warnings"])
        self.assertTrue(doc["warnings"][0].startswith(url_of("agent-kanban-smoke")))

    def test_an_object_absent_since_the_listing_is_a_warning_and_its_chunk_mates_are_kept(self):
        """`gcloud storage cat` of several URLs prints the ones it finds,
        names the gone one on stderr and exits 1; the chunk is re-read one
        object at a time and the gone object is one warning in gcloud's
        words, not a lost record or a failed read."""
        gone = url_of("rca-remediation-pr")
        gcloud = FakeGcloud(absent_urls=[gone])
        doc = read(gcloud)
        self.assertEqual(sorted(r["case"] for r in doc["records"]), ["agent-kanban-smoke", "cluster-agent-crashloop-debug", "upgrades-fleet-version-table"])
        self.assertEqual(len(doc["warnings"]), 1, doc["warnings"])
        self.assertTrue(doc["warnings"][0].startswith(f"{gone}: gcloud storage cat failed: ERROR: (gcloud.storage.cat) The following URLs matched no objects"), doc["warnings"])
        self.assertEqual(doc["error"], None)
        self.assertEqual([len(c) - 3 for c in gcloud.cats], [4, 1, 1, 1, 1])

    def test_one_bad_object_costs_that_object_and_not_its_chunk_mates(self):
        bad = url_of("rca-remediation-pr")
        gcloud = FakeGcloud(fail_urls=[bad])
        doc = read(gcloud)
        self.assertEqual(sorted(r["case"] for r in doc["records"]), ["agent-kanban-smoke", "cluster-agent-crashloop-debug", "upgrades-fleet-version-table"])
        self.assertEqual(doc["warnings"], [f"{bad}: gcloud storage cat failed: HTTPError 503: Service Unavailable"])
        self.assertEqual(doc["error"], None, "a lost object is a warning the page shows, not a failed read")
        # One chunked cat, then one cat per object of the failed chunk.
        self.assertEqual([len(c) - 3 for c in gcloud.cats], [4, 1, 1, 1, 1])
        # The lost object is not in the prior, so the next tick fetches it alone.
        again = read(FakeGcloud(), prior=doc)
        self.assertEqual(len(again["records"]), 4)
        self.assertEqual(again["fetched"], 1)

    def test_a_read_that_outruns_its_deadline_stops_between_waves_and_finishes_next_tick(self):
        ticks = iter(range(0, 10_000, 10))  # every look at the clock is 10 s later
        clock = lambda: float(next(ticks))
        # Waves of one object: started 0, wave 1 runs 10..20 (10 s); before
        # wave 2 the clock reads 30, plus a 10 s wave is past a 25 s deadline.
        gcloud = FakeGcloud()
        doc = read(gcloud, chunk=1, workers=1, deadline_s=25, clock=clock)
        self.assertEqual((len(doc["records"]), doc["fetched"], doc["partial"]), (1, 1, {"fetched": 1, "remaining": 3}))
        self.assertEqual(len(gcloud.cats), 1)
        self.assertIsNone(doc["error"], "a read that stopped is a read, not a failure")
        # The next tick keeps that record from the prior and reads on; with no deadline it finishes.
        again = read(FakeGcloud(), prior=doc, chunk=1, workers=1)
        self.assertEqual((len(again["records"]), again["fetched"], again["partial"]), (4, 3, None))
        # The first wave always runs, whatever the deadline, so every tick makes progress.
        ticks = iter(range(0, 10_000, 10))
        doc = read(FakeGcloud(), chunk=1, workers=1, deadline_s=0, clock=clock)
        self.assertEqual((len(doc["records"]), doc["partial"]), (1, {"fetched": 1, "remaining": 3}))
        # Without a deadline the waves run to the end.
        doc = read(FakeGcloud(), chunk=1, workers=1)
        self.assertEqual((len(doc["records"]), doc["partial"]), (4, None))

    def test_cats_are_chunked(self):
        objects = {}
        for i in range(250):
            name = f"2026-09-{1 + i % 9:02d}T05-00-{i % 60:02d}Z-{i}.jsonl"
            objects[url_of(f"case-{i}", name)] = json.dumps({"case": f"case-{i}", "recorded_at": f"2026-09-{1 + i % 9:02d}T05:00:{i % 60:02d}Z", "key": {}, "runs": 1, "passes": 1}) + "\n"
        gcloud = FakeGcloud(objects)
        doc = read(gcloud)
        # The chunks are read on a thread pool, so the fake records them in the
        # order the threads finished, not the order they were submitted; the
        # sizes are what the test pins, not which finished first.
        self.assertEqual(sorted(len(c) - 3 for c in gcloud.cats), [50, 100, 100])
        self.assertEqual(len(doc["records"]), 250)


class NamesTest(unittest.TestCase):
    def test_parse_url_reads_the_layout_and_rejects_what_is_outside_it(self):
        parsed = store.parse_url(url_of("rca-remediation-pr"), LOCATION)
        self.assertEqual(parsed, {"case": "rca-remediation-pr", "key_dir": f"rca-remediation-pr/{KEY_DIR}", "name": NAME, "stamp": "2026-09-17T05-54-31Z", "build": BUILD})
        self.assertIsNone(store.parse_url(f"{LOCATION}/stray.jsonl", LOCATION))
        self.assertIsNone(store.parse_url(f"{LOCATION}/case/x/nostamp.jsonl", LOCATION))
        self.assertIsNone(store.parse_url("gs://other/evidence/case/x/" + NAME, LOCATION))
        local = store.parse_url(url_of("c", "2026-09-17T05-54-31Z-local.jsonl"), LOCATION)
        self.assertIsNone(local["build"], "a laptop run's name carries no Prow build")
        self.assertEqual(store.stamp_ms("2026-09-17T05-54-31Z"), datetime.datetime(2026, 9, 17, 5, 54, 31, tzinfo=UTC).timestamp() * 1000)
        self.assertIsNone(store.stamp_ms("junk"))

    def test_the_cap_reads_the_gates_environment_variable_and_falls_back_on_junk(self):
        self.assertEqual(store.max_objects_from_env({}), 200)
        self.assertEqual(store.max_objects_from_env({"EVAL_BASELINE_MAX_OBJECTS": "50"}), 50)
        self.assertEqual(store.max_objects_from_env({"EVAL_BASELINE_MAX_OBJECTS": "junk"}), 200)
        self.assertEqual(store.max_objects_from_env({"EVAL_BASELINE_MAX_OBJECTS": "0"}), 200)


class CliTest(unittest.TestCase):
    def run_cli(self, argv, gcloud):
        # The wall clock is pinned: the fixture is stamped 2026-09-17 and
        # would age out of the read's span on 2026-12-30 otherwise.
        err = io.StringIO()
        with unittest.mock.patch.object(store.subprocess, "run", gcloud), unittest.mock.patch.object(store, "now_utc", return_value=NOW), redirect_stderr(err):
            code = store.main(argv)
        return code, err.getvalue()

    def test_writes_the_document_and_reads_it_back_as_the_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "store.json"
            code, err = self.run_cli(["--location", LOCATION, "--out", str(out)], FakeGcloud())
            self.assertEqual(code, 0, err)
            self.assertIn("4 records (4 objects fetched, 4 listed, 0 warnings)", err)
            self.assertIsNone(json.loads(out.read_text())["partial"])
            doc = json.loads(out.read_text())
            self.assertEqual(len(doc["records"]), 4)
            self.assertEqual((doc["window_days"], doc["lead_days"]), (store.DEFAULT_WINDOW_DAYS, store.DEFAULT_LEAD_DAYS))
            gcloud = FakeGcloud()
            code, err = self.run_cli(["--location", LOCATION, "--prior", str(out), "--out", str(out)], gcloud)
            self.assertEqual(code, 0, err)
            self.assertIn("0 objects fetched", err)
            self.assertEqual(gcloud.cats, [])

    def test_an_unreachable_store_keeps_the_prior_with_the_error_or_fails_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "store.json"
            code, err = self.run_cli(["--location", LOCATION, "--out", str(out)], FakeGcloud(fail_ls="HTTPError 403"))
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())
            self.assertIn("no prior store.json", err)
            self.run_cli(["--location", LOCATION, "--out", str(out)], FakeGcloud())
            good = json.loads(out.read_text())
            code, err = self.run_cli(["--location", LOCATION, "--prior", str(out), "--out", str(out)], FakeGcloud(fail_ls="HTTPError 403"))
            self.assertEqual(code, 0)
            kept = json.loads(out.read_text())
            self.assertEqual(kept["records"], good["records"])
            self.assertEqual(kept["read_at"], good["read_at"], "the prior's read time stands")
            self.assertIn("HTTPError 403", kept["error"])
            self.assertIn("wrote the prior read", err)

    def test_fail_with_keeps_the_prior_with_the_reason_and_reads_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "store.json"
            self.run_cli(["--location", LOCATION, "--out", str(out)], FakeGcloud())
            good = json.loads(out.read_text())
            gcloud = FakeGcloud()
            code, err = self.run_cli(["--location", LOCATION, "--prior", str(out), "--out", str(out), "--fail-with", "store.py failed or timed out (exit 124, budget 600s)"], gcloud)
            self.assertEqual(code, 0, err)
            self.assertEqual(gcloud.calls, [], "the store is not touched")
            kept = json.loads(out.read_text())
            self.assertEqual((kept["records"], kept["read_at"]), (good["records"], good["read_at"]))
            self.assertTrue(kept["error"].endswith(": store.py failed or timed out (exit 124, budget 600s)"), kept["error"])
            self.assertIn("wrote the prior read", err)
            # Without a prior there is nothing to keep: exit 1 and no file, as for a failed listing.
            other = pathlib.Path(tmp) / "none.json"
            code, err = self.run_cli(["--location", LOCATION, "--prior", str(pathlib.Path(tmp) / "missing.json"), "--out", str(other), "--fail-with", "no wall clock left"], FakeGcloud())
            self.assertEqual(code, 1)
            self.assertFalse(other.exists())
            self.assertIn("no wall clock left; no prior store.json", err)

    def test_arguments_are_validated(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                store.main(["--location", LOCATION, "--window-days", "0"])
            with self.assertRaises(SystemExit):
                store.main(["--location", LOCATION, "--lead-days", "-1"])
            with self.assertRaises(SystemExit):
                store.main(["--location", LOCATION, "--deadline-s", "-1"])
            with self.assertRaises(SystemExit):
                store.main(["--location", LOCATION, "--max-objects", "0"])


if __name__ == "__main__":
    unittest.main()
