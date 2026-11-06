"""The replay lane's verdict comparison: per case beside the nightly, and per served conversation."""

import json
import os
import shutil
import tempfile
import unittest

import lane_report
from test_run_attribution import World, worker

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
        summary = lane_report.write_report(self.keep, self.world.verdicts, self.nightly, self.out)
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

    def test_a_conversation_whose_recording_left_no_record_is_unknown(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.conversation("t_00000001", (True, True), recorded_from="1790000000000000999")
        report, _ = self.report({NIGHT: []})
        self.assertEqual(report["served_conversations"], {"match": 0, "mismatch": 0, "unknown": 1})

    def test_a_conversation_served_nothing_is_not_compared(self):
        self.world.run("case-a", 1, "fail", ["t_00000001"])
        self.conversation("t_00000001", (False, False))
        report, summary = self.report({NIGHT: []})
        self.assertEqual(report["served_conversations"], {"match": 0, "mismatch": 0, "unknown": 0})
        self.assertIn("served nothing", summary)


if __name__ == "__main__":
    unittest.main()
