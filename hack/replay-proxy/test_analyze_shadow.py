"""The offline would-hit analysis over synthetic shadow logs."""

import contextlib
import io
import json
import os
import tempfile
import unittest

import analyze_shadow


def line(request_id, ts, kind, build, fingerprint, messages, roles=None, project="p1", tokens=100, unstorable=(),
         pr="", latency_ms=0):
    entry = {
        "event": "replay_shadow_request",
        "request_id": request_id,
        "ts": ts,
        "job": {"kind": kind, "project": project, "build": build, "pr": pr},
        "fingerprint": fingerprint,
        "settings_hash": "s",
        "roles": list(roles or ["user"] * len(messages or [])),
        "prompt_tokens": tokens,
        "completion_tokens": 5,
        "latency_ms": latency_ms,
        "unstorable": list(unstorable),
    }
    if messages is not None:
        entry["message_hashes"] = list(messages)
    return entry


def presubmit(request_id, ts, pr, build, fingerprint, messages, **kwargs):
    return line(request_id, ts, "presubmit", build, fingerprint, messages, pr=pr, **kwargs)


def write(lines):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    with handle:
        for entry in lines:
            handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")
    return handle.name


def report(lines, **kwargs):
    records, skipped = analyze_shadow.load([write(lines)])
    return analyze_shadow.summarize(records, **kwargs), skipped


def hits(lines, estimate="high", **kwargs):
    rep, _ = report(lines, **kwargs)
    return rep["estimates"][estimate]["hits"]


class EstimateTest(unittest.TestCase):
    def test_low_counts_nightly_and_high_adds_earlier_builds_of_the_same_pr(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["a"]),
            presubmit("p1", 2, "10", "b1", "B", ["b"], tokens=300),
            presubmit("p2", 3, "10", "b2", "A", ["a"], tokens=100),  # nightly hit
            presubmit("p3", 4, "10", "b2", "B", ["b"], tokens=300),  # same PR, earlier build
            presubmit("p4", 5, "10", "b2", "C", ["c"], tokens=300),  # miss
        ])
        self.assertEqual(rep["presubmit_prompt_tokens"], 1000)
        self.assertEqual(rep["estimates"]["low"]["hits"], 1)
        self.assertEqual(rep["estimates"]["low"]["token_weighted_hit_percent"], 10.0)
        self.assertEqual(rep["estimates"]["high"]["hits"], 2)
        self.assertEqual(rep["estimates"]["high"]["token_weighted_hit_percent"], 40.0)
        self.assertEqual(rep["estimates"]["high"]["saved_completion_tokens"], 10)

    def test_another_pr_never_counts_without_merge_times(self):
        lines = [presubmit("p1", 1, "10", "b1", "A", ["a"]), presubmit("p2", 5, "11", "b2", "A", ["a"])]
        self.assertEqual(hits(lines), 0)

    def test_another_pr_counts_only_after_it_merged(self):
        lines = [
            presubmit("p1", 1, "10", "b1", "A", ["a"]),
            presubmit("p2", 5, "11", "b2", "A", ["a"]),   # before PR 10 merged
            presubmit("p3", 20, "12", "b3", "A", ["a"]),  # after
        ]
        rep, _ = report(lines, merged={"10": 10.0})
        self.assertEqual(rep["estimates"]["high"]["hits"], 1)
        self.assertEqual(rep["estimates"]["low"]["hits"], 0)
        self.assertEqual(hits(lines, merged={"10": 10.0}, top=0), 1)

    def test_merge_copy_takes_only_the_last_build_before_the_merge(self):
        lines = [
            presubmit("p1", 1, "10", "b1", "OLD", ["o"]),  # PR 10's first build
            presubmit("p2", 3, "10", "b2", "NEW", ["n"]),  # its last build before merging at 10
            presubmit("p3", 20, "12", "b3", "OLD", ["o"]),
            presubmit("p4", 21, "12", "b3", "NEW", ["n"]),
        ]
        rep, _ = report(lines, merged={"10": 10.0})
        self.assertEqual(rep["estimates"]["high"]["hits"], 1)
        hit_ids = [h.request.request_id for _, h in analyze_shadow.iter_hits(
            analyze_shadow.load([write(lines)])[0], "high", merged={"10": 10.0}) if h]
        self.assertEqual(hit_ids, ["p4"])

    def test_a_build_finishing_after_the_merge_is_not_copied(self):
        lines = [
            presubmit("p1", 1, "10", "b1", "A", ["a"]),
            presubmit("p2", 8, "10", "b2", "B", ["b"]),
            presubmit("p3", 12, "10", "b2", "Z", ["z"]),  # b2 ends after the merge at 10
            presubmit("p4", 20, "12", "b3", "A", ["a"]),
            presubmit("p5", 21, "12", "b3", "B", ["b"]),
        ]
        hit_ids = [h.request.request_id for _, h in analyze_shadow.iter_hits(
            analyze_shadow.load([write(lines)])[0], "high", merged={"10": 10.0}) if h]
        self.assertEqual(hit_ids, ["p4"])

    def test_presubmit_lines_without_a_pr_are_counted(self):
        rep, _ = report([presubmit("p1", 1, "", "b1", "A", ["a"]), presubmit("p2", 2, "10", "b2", "A", ["a"]),
                         line("n1", 3, "nightly", "n", "A", ["a"])])
        self.assertEqual(rep["presubmit_without_pr"], 1)

    def test_a_sighting_after_the_merge_is_not_copied(self):
        lines = [
            presubmit("p1", 15, "10", "b1", "A", ["a"]),
            presubmit("p2", 20, "12", "b3", "A", ["a"]),
        ]
        self.assertEqual(hits(lines, merged={"10": 10.0}), 0)

    def test_a_build_never_hits_its_own_answers(self):
        lines = [presubmit("p1", 1, "10", "b1", "A", ["a"]), presubmit("p2", 2, "10", "b1", "A", ["a"])]
        self.assertEqual(hits(lines), 0)

    def test_an_earlier_build_counts_only_once_finished(self):
        lines = [
            presubmit("p1", 1, "10", "b1", "A", ["a"]),
            presubmit("p2", 3, "10", "b2", "A", ["a"]),  # b1 still running
            presubmit("p3", 4, "10", "b1", "Z", ["z"]),
            presubmit("p4", 6, "10", "b2", "A", ["a"]),  # b1 finished at 4
        ]
        rep, _ = report(lines)
        self.assertEqual(rep["estimates"]["high"]["hits"], 1)

    def test_a_sighting_is_available_only_once_its_answer_finished(self):
        lines = [
            line("n1", 1, "nightly", "n", "A", ["a"], latency_ms=10_000),
            presubmit("p1", 5, "10", "b1", "A", ["a"]),   # answer not back until 11
            presubmit("p2", 12, "10", "b1", "A", ["a"]),
        ]
        self.assertEqual(hits(lines, "low"), 1)

    def test_later_sightings_never_count(self):
        lines = [presubmit("p1", 2, "10", "b1", "A", ["a"]), line("n1", 3, "nightly", "n", "A", ["a"])]
        self.assertEqual(hits(lines), 0)

    def test_nightly_requests_are_not_in_the_denominator(self):
        rep, _ = report([line("n1", 1, "nightly", "n", "A", ["a"], tokens=10_000)])
        self.assertEqual(rep["presubmit_prompt_tokens"], 0)
        self.assertEqual(rep["estimates"]["high"]["token_weighted_hit_percent"], 0.0)

    def test_cross_project_share(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["a"], project="p9"),
            line("n2", 1, "nightly", "n", "B", ["b"], project="p1"),
            presubmit("p1", 2, "10", "b1", "A", ["a"], tokens=300),
            presubmit("p2", 3, "10", "b1", "B", ["b"], tokens=100),
        ])
        self.assertEqual(rep["estimates"]["low"]["cross_project_hit_percent"], 75.0)

    def test_unstorable_sightings_are_not_recordings(self):
        lines = [
            line("n1", 1, "nightly", "n", "A", ["a"], unstorable=["fallback_attempted"]),
            presubmit("p1", 2, "10", "b1", "A", ["a"]),
        ]
        rep, _ = report(lines)
        self.assertEqual(rep["estimates"]["low"]["hits"], 0)
        self.assertEqual(rep["unstorable_sightings"], 1)
        self.assertEqual(hits(lines, "low", require_storable=False), 1)

    def test_a_cut_short_upstream_answer_is_never_a_recording(self):
        lines = [
            line("n1", 1, "nightly", "n", "A", ["a"], unstorable=["upstream_incomplete"]),
            presubmit("p1", 2, "10", "b1", "A", ["a"]),
        ]
        self.assertEqual(hits(lines, "low"), 0)

    def test_simulated_estimate_ignores_earlier_assistant_messages(self):
        roles = ["user", "assistant", "tool"]
        lines = [
            line("n1", 1, "nightly", "n", "A2", ["u", "ans-x", "tool"], roles=roles),
            presubmit("p1", 2, "10", "b1", "B2", ["u", "ans-y", "tool"], roles=roles),
        ]
        self.assertEqual(hits(lines, "high"), 0)
        self.assertEqual(hits(lines, "simulated"), 1)

    def test_simulated_estimate_follows_the_high_sources(self):
        roles = ["user", "assistant", "tool"]
        lines = [
            presubmit("p1", 1, "10", "b1", "A2", ["u", "ans-x", "tool"], roles=roles),
            presubmit("p2", 5, "11", "b2", "B2", ["u", "ans-y", "tool"], roles=roles),
        ]
        self.assertEqual(hits(lines, "simulated"), 0)

    def test_simulated_estimate_still_misses_on_a_different_tool_result(self):
        roles = ["user", "assistant", "tool"]
        lines = [
            line("n1", 1, "nightly", "n", "A2", ["u", "ans-x", "tool-1"], roles=roles),
            presubmit("p1", 2, "10", "b1", "B2", ["u", "ans-y", "tool-2"], roles=roles),
        ]
        self.assertEqual(hits(lines, "simulated"), 0)

    def test_simulated_falls_back_to_the_fingerprint_without_message_hashes(self):
        lines = [line("n1", 1, "nightly", "n", "A", None), presubmit("p1", 2, "10", "b1", "A", None),
                 presubmit("p2", 3, "10", "b1", "B", None)]
        self.assertEqual(hits(lines, "simulated"), 1)

    def test_simulated_is_never_below_high(self):
        # Same fingerprint, but message hashes that differ (lines from before a
        # hashing change): high hits, so simulated must too.
        lines = [line("n1", 1, "nightly", "n", "A", ["x"]), presubmit("p1", 2, "10", "b1", "A", ["y"])]
        self.assertEqual(hits(lines, "high"), 1)
        self.assertEqual(hits(lines, "simulated"), 1)


class MissTest(unittest.TestCase):
    def test_closest_earlier_request_by_longest_prefix_ranked_by_tokens(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["s", "u", "t1"], roles=["system", "user", "tool"]),
            line("n2", 1.5, "nightly", "n", "A0", ["s", "x"], roles=["system", "user"]),
            presubmit("p1", 2, "10", "b1", "B", ["s", "u", "t2", "u2"], roles=["system", "user", "tool", "user"],
                      tokens=500),
            presubmit("p2", 3, "10", "b1", "C", ["s", "y"], roles=["system", "user"], tokens=50),
        ])
        top = rep["top_misses"]
        self.assertEqual([m["request_id"] for m in top], ["p1", "p2"])
        self.assertEqual(top[0]["closest_request_id"], "n1")
        self.assertEqual(top[0]["matched_messages"], 2)
        self.assertEqual(top[0]["first_difference_role"], "tool")
        self.assertEqual(top[1]["matched_messages"], 1)
        self.assertEqual(rep["miss_tokens_by_first_difference"], {"tool": 500, "user": 50})

    def test_closest_skips_the_same_build_and_other_prs(self):
        rep, _ = report([
            line("n1", 1, "nightly", "n", "A", ["s"]),
            presubmit("p0", 1.5, "99", "b9", "D", ["s", "u", "v"]),
            presubmit("p1", 2, "10", "b1", "B", ["s", "u"]),
            presubmit("p2", 3, "10", "b1", "C", ["s", "u", "v"]),
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
            presubmit("p", 2, "10", "b", "A", ["a"]),
        ])])
        self.assertEqual([r.request_id for r in records], ["p"])
        self.assertEqual(records[0].pr, "10")
        self.assertEqual(skipped, {"not_json": 1, "other_event": 1, "untagged": 1, "no_fingerprint": 1})

    def test_records_from_several_files_are_merged_in_time_order(self):
        a = write([presubmit("p2", 5, "10", "b2", "A", ["a"])])
        b = write([line("n1", 1, "nightly", "n", "A", ["a"])])
        records, _ = analyze_shadow.load([a, b])
        self.assertEqual([r.request_id for r in records], ["n1", "p2"])

    def test_main_prints_text_and_json_and_reads_merge_times(self):
        path = write([presubmit("p1", 1, "10", "b", "A", ["a"]), presubmit("p2", 5, "11", "c", "A", ["a"])])
        merged = write([])
        with open(merged, "w") as handle:
            json.dump({"10": 2}, handle)
        for args in ([path, "--merged", merged], [path, "--merged", merged, "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(analyze_shadow.main(args), 0)
            self.assertIn("50.0", out.getvalue())
        os.unlink(path)
        os.unlink(merged)


if __name__ == "__main__":
    unittest.main()
