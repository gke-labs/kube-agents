"""The offline would-hit analysis over synthetic shadow logs."""

import contextlib
import io
import json
import os
import tempfile
import unittest

import analyze_shadow


def line(request_id, ts, kind, build, fingerprint, messages, roles=None, project="p1", tokens=100, unstorable=()):
    return {
        "event": "replay_shadow_request",
        "request_id": request_id,
        "ts": ts,
        "job": {"kind": kind, "project": project, "build": build},
        "fingerprint": fingerprint,
        "settings_hash": "s",
        "message_hashes": list(messages),
        "roles": list(roles or ["user"] * len(messages)),
        "prompt_tokens": tokens,
        "completion_tokens": 5,
        "latency_ms": 1000,
        "unstorable": list(unstorable),
    }


def write(lines):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    with handle:
        for entry in lines:
            handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")
    return handle.name


def report(lines, **kwargs):
    records, skipped = analyze_shadow.load([write(lines)])
    return analyze_shadow.summarize(records, **kwargs), skipped


class EstimateTest(unittest.TestCase):
    def test_low_counts_nightly_sightings_and_high_adds_other_presubmits(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["a"]),
            line("p1", 2, "presubmit", "b1", "B", ["b"], tokens=300),
            line("p2", 3, "presubmit", "b2", "A", ["a"], tokens=100),  # nightly hit
            line("p3", 4, "presubmit", "b2", "B", ["b"], tokens=300),  # presubmit hit (other build)
            line("p4", 5, "presubmit", "b2", "C", ["c"], tokens=300),  # miss
        ])
        self.assertEqual(rep["presubmit_prompt_tokens"], 1000)
        self.assertEqual(rep["estimates"]["low"]["hits"], 1)
        self.assertEqual(rep["estimates"]["low"]["token_weighted_hit_percent"], 10.0)
        self.assertEqual(rep["estimates"]["high"]["hits"], 2)
        self.assertEqual(rep["estimates"]["high"]["token_weighted_hit_percent"], 40.0)
        self.assertEqual(rep["estimates"]["high"]["saved_completion_tokens"], 10)
        self.assertEqual(rep["estimates"]["high"]["saved_model_ms"], 2000)

    def test_a_build_never_hits_its_own_answers(self):
        rep, _ = report([
            line("p1", 1, "presubmit", "b1", "A", ["a"]),
            line("p2", 2, "presubmit", "b1", "A", ["a"]),
        ])
        self.assertEqual(rep["estimates"]["high"]["hits"], 0)

    def test_later_sightings_never_count(self):
        rep, _ = report([
            line("p1", 2, "presubmit", "b1", "A", ["a"]),
            line("n1", 3, "nightly", "n", "A", ["a"]),
        ])
        self.assertEqual(rep["estimates"]["high"]["hits"], 0)

    def test_nightly_requests_are_not_in_the_denominator(self):
        rep, _ = report([line("n1", 1, "nightly", "n", "A", ["a"], tokens=10_000)])
        self.assertEqual(rep["presubmit_prompt_tokens"], 0)
        self.assertEqual(rep["estimates"]["high"]["token_weighted_hit_percent"], 0.0)

    def test_cross_project_share(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["a"], project="p9"),
            line("n2", 1, "nightly", "n", "B", ["b"], project="p1"),
            line("p1", 2, "presubmit", "b1", "A", ["a"], tokens=300),
            line("p2", 3, "presubmit", "b1", "B", ["b"], tokens=100),
        ])
        self.assertEqual(rep["estimates"]["low"]["cross_project_hit_percent"], 75.0)

    def test_unstorable_sightings_are_not_recordings(self):
        lines = [
            line("n1", 1, "nightly", "n", "A", ["a"], unstorable=["fallback_attempted"]),
            line("p1", 2, "presubmit", "b1", "A", ["a"]),
        ]
        rep, _ = report(lines)
        self.assertEqual(rep["estimates"]["low"]["hits"], 0)
        self.assertEqual(rep["unstorable_sightings"], 1)
        rep, _ = report(lines, require_storable=False)
        self.assertEqual(rep["estimates"]["low"]["hits"], 1)

    def test_simulated_estimate_treats_history_answers_as_replayed(self):
        # Same input, but the model worded its first answer differently in the
        # second run: no exact hit, a simulated one.
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A2", ["u", "ans-x", "tool"], roles=["user", "assistant", "tool"]),
            line("p1", 2, "presubmit", "b1", "B2", ["u", "ans-y", "tool"], roles=["user", "assistant", "tool"]),
        ])
        self.assertEqual(rep["estimates"]["high"]["hits"], 0)
        self.assertEqual(rep["estimates"]["simulated"]["hits"], 1)

    def test_simulated_estimate_still_misses_on_a_different_tool_result(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A2", ["u", "ans-x", "tool-1"], roles=["user", "assistant", "tool"]),
            line("p1", 2, "presubmit", "b1", "B2", ["u", "ans-y", "tool-2"], roles=["user", "assistant", "tool"]),
        ])
        self.assertEqual(rep["estimates"]["simulated"]["hits"], 0)


class MissTest(unittest.TestCase):
    def test_closest_earlier_request_by_longest_prefix_ranked_by_tokens(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["s", "u", "t1"], roles=["system", "user", "tool"]),
            line("n2", 1.5, "nightly", "n", "A0", ["s", "x"], roles=["system", "user"]),
            line("p1", 2, "presubmit", "b1", "B", ["s", "u", "t2", "u2"], roles=["system", "user", "tool", "user"], tokens=500),
            line("p2", 3, "presubmit", "b1", "C", ["s", "y"], roles=["system", "user"], tokens=50),
        ])
        top = rep["top_misses"]
        self.assertEqual([m["request_id"] for m in top], ["p1", "p2"])
        self.assertEqual(top[0]["closest_request_id"], "n1")
        self.assertEqual(top[0]["matched_messages"], 2)
        self.assertEqual(top[0]["first_difference_role"], "tool")
        self.assertEqual(top[1]["matched_messages"], 1)
        self.assertEqual(rep["miss_tokens_by_first_difference"], {"tool": 500, "user": 50})

    def test_closest_skips_the_same_build(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["s"]),
            line("p1", 2, "presubmit", "b1", "B", ["s", "u"]),
            line("p2", 3, "presubmit", "b1", "C", ["s", "u", "v"]),
        ])
        p2 = next(m for m in rep["top_misses"] if m["request_id"] == "p2")
        self.assertEqual(p2["closest_request_id"], "n1")
        self.assertEqual(p2["matched_messages"], 1)


class LoadTest(unittest.TestCase):
    def test_skips_other_lines_and_untagged_jobs(self):
        records, skipped = analyze_shadow.load([write([
            "not json",
            {"event": "replay_shadow_start"},
            line("x", 1, "", "b", "A", ["a"]),
            {"event": "replay_shadow_request", "ts": 1, "error": "boom", "job": {"kind": "presubmit"}},
            line("p", 2, "presubmit", "b", "A", ["a"]),
        ])])
        self.assertEqual([r.request_id for r in records], ["p"])
        self.assertEqual(skipped, {"not_json": 1, "other_event": 1, "untagged": 1, "no_fingerprint": 1})

    def test_records_from_several_files_are_merged_in_time_order(self):
        a = write([line("p2", 5, "presubmit", "b2", "A", ["a"])])
        b = write([line("n1", 1, "nightly", "n", "A", ["a"])])
        records, _ = analyze_shadow.load([a, b])
        self.assertEqual([r.request_id for r in records], ["n1", "p2"])

    def test_main_prints_text_and_json(self):
        path = write([line("n1", 1, "nightly", "n", "A", ["a"]), line("p1", 2, "presubmit", "b", "A", ["a"])])
        for args in ([path], [path, "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(analyze_shadow.main(args), 0)
            self.assertIn("100.0", out.getvalue())
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
