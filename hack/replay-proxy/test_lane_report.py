"""The replay lane's verdict comparison: per case beside the nightly, and per served conversation."""

import json
import os
import shutil
import tempfile
import unittest

import lane_report
from test_run_attribution import World, created, front, worker

NIGHT = "1790000000000000001"
POD = "inference-replay-shadow-abc12-xyz34"


class LaneReportTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.world = World(root)
        self.keep = root
        self.out = os.path.join(root, "report")
        self.nightly = os.path.join(root, "nightly-runs.json")
        self.ts = 1_800_000_000.0

    def conversation(self, card, served, recorded_from=NIGHT):
        """A worker's two requests: the opening, then the opening plus its answer and a tool result."""
        for messages, is_served in ((worker(card), served[0]),
                                    (worker(card, {"role": "assistant", "content": "ok"},
                                            {"role": "user", "content": "result"}), served[1])):
            entry = self.world.request(messages, f"fp-{card}-{len(messages)}")
            self.ts += 1
            entry.update(mode="replay", ts=self.ts, served=is_served,
                         roles=[m["role"] for m in messages])
            if is_served:
                entry["served_from"] = {"build": recorded_from}

    def report(self, nightly_runs):
        os.makedirs(os.path.join(self.keep, "pods", POD, "logs"), exist_ok=True)
        with open(os.path.join(self.keep, "pods", POD, "logs", "seg.jsonl"), "w") as handle:
            handle.write("\n".join(json.dumps(e) for e in self.world.lines) + "\n")
        with open(self.nightly, "w") as handle:
            json.dump(nightly_runs, handle)
        named = getattr(self, "report_commit", None)
        commit = {"commit": named[0], "commit_source": named[1]} if named else {}
        summary = lane_report.write_report(self.keep, self.world.verdicts, self.nightly, self.out, **commit)
        with open(os.path.join(self.out, "verdicts.json")) as handle:
            return json.load(handle), summary

    def test_a_served_run_that_passed_matches_and_one_that_failed_does_not(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.world.run("case-a", 2, "fail", ["t_00000002"])
        self.conversation("t_00000001", (True, True))
        self.conversation("t_00000002", (True, False))
        report, summary = self.report({NIGHT: [{"case": "case-a", "rep": 1, "outcome": "pass"},
                                               {"case": "case-a", "rep": 2, "outcome": "pass"}]})
        self.assertEqual(report["served_conversations"], {"match": 1, "mismatch": 1, "unknown": 0})
        self.assertEqual(report["mismatched_conversations"][0]["run"], "case-a#2")
        case = report["cases"]["case-a"]
        self.assertEqual((case["lane"]["passes"], case["lane"]["scored"]), (1, 2))
        self.assertEqual(case["nightly"][NIGHT], {"passes": 2, "scored": 2, "outcomes": ["pass", "pass"]})
        self.assertEqual([r["served_token_percent"] for r in case["lane"]["runs"]], [100.0, 50.0])
        self.assertIn("case-a: lane 1/2", summary)
        for name in ("replay.json", "verdicts.json", "summary.txt"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)))

    def test_the_report_names_the_commit_replayed_and_where_it_came_from(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.conversation("t_00000001", (True, True))
        commit = "ab" * 20
        for source, text in (("recording", f"replayed on {commit}: the commit the newest recording night ran"),
                             ("main", f"replayed on {commit}: main, as a fallback"),
                             ("", f"replayed on {commit}: where it came from was not stated")):
            with self.subTest(source=source):
                self.report_commit = (commit, source)
                report, summary = self.report({NIGHT: [{"case": "case-a", "rep": 1, "outcome": "pass"}]})
                self.assertEqual((report["commit"], report["commit_source"]), (commit, source))
                self.assertTrue(summary.startswith(text), summary)
        self.report_commit = ("", "")
        report, summary = self.report({})
        self.assertEqual((report["commit"], report["commit_source"]), ("", ""))
        self.assertTrue(summary.startswith("replayed on an unknown commit"), summary)

    def test_a_served_run_with_no_verdict_is_unknown_not_a_mismatch(self):
        for outcome in ("infra", "blocked", "not_applicable"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.world.run("case-a", 1, outcome, ["t_00000001"])
                self.conversation("t_00000001", (True, True))
                report, _ = self.report({NIGHT: [{"case": "case-a", "rep": 1, "outcome": "pass"}]})
                self.assertEqual(report["served_conversations"], {"match": 0, "mismatch": 0, "unknown": 1})
                self.assertEqual(report["unknown_reasons"], {"no_verdict": 1})

    def test_a_conversation_whose_recording_night_is_in_no_record_is_unknown_and_says_why(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.conversation("t_00000001", (True, True), recorded_from="1790000000000000999")
        report, summary = self.report({NIGHT: []})
        self.assertEqual(report["served_conversations"], {"match": 0, "mismatch": 0, "unknown": 1})
        self.assertEqual(report["unknown_reasons"], {"recording_build_unrecorded": 1})
        self.assertIn("recording_build_unrecorded", summary)

    def test_an_answer_first_written_by_an_older_night_still_gets_a_verdict(self):
        # The fetch reads runs back to the lifecycle (fetch_cache.py), so a night
        # older than the 30-hour object window is still in nightly_runs.
        old_night = "1780000000000000000"
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.conversation("t_00000001", (True, True), recorded_from=old_night)
        report, _ = self.report({NIGHT: [], old_night: [{"case": "case-a", "rep": 1, "outcome": "pass"}]})
        self.assertEqual(report["served_conversations"], {"match": 1, "mismatch": 0, "unknown": 0})
        self.assertEqual(report["cases"]["case-a"]["nightly"], {old_night: {"passes": 1, "scored": 1, "outcomes": ["pass"]}})

    def test_a_front_doors_opening_counts_for_the_run_its_conversation_names(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        for messages, served in ((front(), True), (front({"role": "assistant", "content": "A"}, *created("t_00000001")), False)):
            entry = self.world.request(messages, f"fp-{len(messages)}")
            self.ts += 1
            entry.update(mode="replay", ts=self.ts, served=served, roles=[m["role"] for m in messages])
            if served:
                entry["served_from"] = {"build": NIGHT}
        report, _ = self.report({NIGHT: [{"case": "case-a", "rep": 1, "outcome": "pass"}]})
        run = report["cases"]["case-a"]["lane"]["runs"][0]
        self.assertEqual((run["requests"], run["served_requests"]), (2, 1))
        self.assertEqual(report["unattributed_prompt_tokens"], 0)

    def test_a_conversation_served_nothing_is_not_compared(self):
        self.world.run("case-a", 1, "fail", ["t_00000001"])
        self.conversation("t_00000001", (False, False))
        report, summary = self.report({NIGHT: []})
        self.assertEqual(report["served_conversations"], {"match": 0, "mismatch": 0, "unknown": 0})
        self.assertIn("served nothing", summary)

    def arms(self, live_reps, replayed_from, cases):
        with open(os.path.join(self.world.verdicts, "replay-lane-arms.json"), "w") as handle:
            json.dump({"live_reps": live_reps, "replayed_from": replayed_from, "cases": cases}, handle)

    def test_a_safety_case_reports_its_live_and_replayed_arms_apart(self):
        # Reps 1-3 live (nothing served), 4-6 replayed (served); case-b ran no live arm.
        outcomes = {1: "pass", 2: "fail", 3: "fail", 4: "pass", 5: "pass", 6: "fail"}
        for rep, outcome in outcomes.items():
            card = f"t_0000000{rep}"
            self.world.run("case-a", rep, outcome, [card])
            self.conversation(card, (False, False) if rep <= 3 else (True, rep != 6))
            if rep == 3:
                replayed_from = self.ts + 0.5
        self.world.run("case-b", 1, "pass", ["t_00000009"])
        self.conversation("t_00000009", (True, True))
        self.arms(3, replayed_from, ["case-a"])
        report, summary = self.report({NIGHT: [{"case": "case-a", "rep": 1, "outcome": "pass"}]})
        arms = report["cases"]["case-a"]["arms"]
        self.assertEqual({k: arms["live"][k] for k in ("passes", "scored", "runs", "reps", "served_token_percent")},
                         {"passes": 1, "scored": 3, "runs": 3, "reps": [1, 2, 3], "served_token_percent": 0.0})
        self.assertEqual(arms["live"]["run_served_token_percent"], [0.0, 0.0, 0.0])
        self.assertEqual((arms["replayed"]["passes"], arms["replayed"]["scored"], arms["replayed"]["reps"]),
                         (2, 3, [4, 5, 6]))
        self.assertEqual(arms["replayed"]["run_served_token_percent"], [100.0, 100.0, 50.0])
        self.assertEqual(arms["replayed"]["served_token_percent"], 83.3)
        self.assertNotIn("arms", report["cases"]["case-b"])
        self.assertIn("  case-a: live 1/3, replayed 2/3 (served 83.3%)\n", summary)
        self.assertNotIn("not live", summary)
        # The existing per-case and per-conversation fields are unchanged.
        self.assertEqual((report["cases"]["case-a"]["lane"]["passes"], report["cases"]["case-a"]["lane"]["scored"]), (3, 6))

    def test_replay_json_counts_only_what_was_asked_once_replay_began(self):
        self.world.run("case-a", 1, "fail", ["t_00000001"])
        self.conversation("t_00000001", (False, False))
        replayed_from = self.ts + 0.5
        self.world.run("case-a", 2, "pass", ["t_00000002"])
        self.conversation("t_00000002", (True, True))
        self.arms(1, replayed_from, ["case-a"])
        self.report({NIGHT: []})
        with open(os.path.join(self.out, "replay.json")) as handle:
            replay = json.load(handle)
        self.assertEqual((replay["requests"], replay["served_token_percent"]), (2, 100.0))

    def test_a_live_arm_that_was_served_says_it_was_not_live(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.conversation("t_00000001", (True, False))
        self.world.run("case-a", 2, "pass", ["t_00000002"])
        self.conversation("t_00000002", (True, True))
        self.arms(1, 0, ["case-a"])
        report, summary = self.report({NIGHT: []})
        self.assertEqual(report["cases"]["case-a"]["arms"]["live"]["served_token_percent"], 50.0)
        self.assertIn("case-a: live 1/1, replayed 1/1 (served 100.0%); the live arm was served 50.0%, so it was not live", summary)

    def test_without_the_arms_note_nothing_is_split(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.conversation("t_00000001", (True, True))
        report, summary = self.report({NIGHT: []})
        self.assertNotIn("arms", report["cases"]["case-a"])
        self.assertNotIn("safety cases", summary)


if __name__ == "__main__":
    unittest.main()
