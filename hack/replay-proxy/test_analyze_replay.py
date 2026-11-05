"""The replay-lane analysis over synthetic replay-mode lines."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest

import analyze_replay

TEMP_DIR = None


def setUpModule():
    global TEMP_DIR
    TEMP_DIR = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def line(request_id, ts, messages, served, tokens=100, build="b1", miss=None, served_from=None, mode="replay"):
    entry = {
        "event": "replay_shadow_request", "mode": mode, "request_id": request_id, "ts": ts,
        "job": {"kind": "nightly", "project": "p", "build": build},
        "fingerprint": "fp-" + request_id, "diag_settings_hash": "s", "diag_message_hashes": list(messages),
        "served": served, "prompt_tokens": tokens,
    }
    if miss is not None:
        entry["miss"] = miss
    if served_from is not None:
        entry["served_from"] = {"build": served_from}
    return entry


def write(entries):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=TEMP_DIR)
    with handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return handle.name


MISS_TOOL = {"role": "tool", "signature": "READY #/# -> READY #/#"}
MISS_USER = {"role": "user", "signature": "card ⟦TASK_#⟧ -> card t_#"}


def entries():
    return [
        # Conversation A: two served, then a tool-result miss, then live.
        line("a1", 1, ["s", "u"], True, served_from="rec-1"),
        line("a2", 2, ["s", "u", "a", "t"], True, served_from="rec-1"),
        line("a3", 3, ["s", "u", "a", "t", "a2", "t2"], False, miss=MISS_TOOL),
        line("a4", 4, ["s", "u", "a", "t", "a2", "t2", "a3", "t3"], False, miss=MISS_TOOL),
        # Conversation B: misses on its first request.
        line("b1", 1.5, ["s", "x"], False, tokens=500, miss=MISS_USER),
        # Conversation C: fully served.
        line("c1", 1.7, ["s", "y"], True, served_from="rec-2"),
        # A shadow line is not part of the lane.
        line("z1", 1.8, ["s", "u"], False, mode="shadow"),
    ]


class ConversationTest(unittest.TestCase):
    def test_a_request_extending_an_earlier_one_joins_its_conversation(self):
        convs = analyze_replay.conversations(analyze_replay.load([write(entries())]))
        self.assertEqual(sorted(len(c.lines) for c in convs), [1, 1, 4])

    def test_parallel_workers_with_the_same_opening_keep_their_own_conversations(self):
        convs = analyze_replay.conversations(analyze_replay.load([write([
            line("a1", 1, ["s", "u"], True), line("b1", 1.1, ["s", "u"], True),
            line("b2", 2, ["s", "u", "ab", "tb"], True), line("a2", 2.1, ["s", "u", "aa", "ta"], True),
            line("b3", 3, ["s", "u", "ab", "tb", "ab2", "tb2"], True),
        ])]))
        # The two openings are indistinguishable, so which worker is which is arbitrary;
        # the two continuations must still land in different conversations.
        self.assertEqual(sorted(len(c.lines) for c in convs), [2, 3])
        self.assertFalse(any({"a2", "b2"} <= {l.request_id for l in c.lines} for c in convs))

    def test_a_branch_from_an_earlier_request_rejoins_its_conversation(self):
        convs = analyze_replay.conversations(analyze_replay.load([write([
            line("a1", 1, ["s", "u"], True), line("a2", 2, ["s", "u", "a", "t"], True),
            line("a3", 3, ["s", "u", "a", "t", "a2", "t2"], True),
            line("a4", 4, ["s", "u", "a", "t", "a2x", "t2x"], True),
        ])]))
        self.assertEqual(len(convs), 1)

    def test_the_same_messages_in_another_build_are_another_conversation(self):
        convs = analyze_replay.conversations(analyze_replay.load([write([
            line("a1", 1, ["s", "u"], True), line("a2", 2, ["s", "u", "a"], True, build="b2"),
        ])]))
        self.assertEqual(len(convs), 2)


class SummaryTest(unittest.TestCase):
    def test_served_rates_and_first_misses(self):
        report = analyze_replay.summarize(analyze_replay.load([write(entries())]))
        self.assertEqual(report["conversations"], 3)
        self.assertEqual(report["prompt_tokens"], 1000)
        self.assertEqual(report["served_token_percent"], 30.0)
        self.assertEqual(report["served_before_first_miss_token_percent"], 30.0)
        self.assertEqual(report["first_miss_tokens_lost_by_role"], {"user": 500, "tool": 200})
        top = report["first_miss_tokens_lost_by_signature"][0]
        self.assertEqual((top["role"], top["tokens_lost"]), ("user", 500))
        a = next(c for c in report["per_conversation"] if c["first_request_id"] == "a1")
        self.assertEqual((a["served_requests_before_first_miss"], a["served_tokens_before_first_miss"]), (2, 200))
        self.assertEqual((a["first_miss_index"], a["first_miss_role"], a["tokens_lost"]), (2, "tool", 200))
        c = next(c for c in report["per_conversation"] if c["first_request_id"] == "c1")
        self.assertNotIn("first_miss_index", c)

    def test_a_hit_after_the_first_miss_counts_as_served_but_not_before_it(self):
        report = analyze_replay.summarize(analyze_replay.load([write([
            line("a1", 1, ["s", "u"], False, miss=MISS_USER),
            line("a2", 2, ["s", "u", "a", "t"], True),
        ])]))
        self.assertEqual(report["served_token_percent"], 50.0)
        self.assertEqual(report["served_before_first_miss_token_percent"], 0.0)

    def test_served_through_answer_slots_is_reported_apart(self):
        lines = [line("x1", 1, ["m1"], True, tokens=300), line("x2", 2, ["m1", "m2"], True, tokens=100)]
        lines[0]["served_via"], lines[1]["served_via"] = "exact", "answer_slots"
        report = analyze_replay.summarize(analyze_replay.load([write(lines)]))
        self.assertEqual(report["served_token_percent"], 100.0)
        self.assertEqual(report["served_via_answer_slots_token_percent"], 25.0)

    def test_verdict_join(self):
        verdicts = {"b1": "pass", "rec-1": "pass", "rec-2": "fail"}
        report = analyze_replay.summarize(analyze_replay.load([write(entries())]), verdicts)
        # A: b1 pass vs rec-1 pass; C: b1 pass vs rec-2 fail; B: nothing served, unknown.
        self.assertEqual(report["verdicts"], {"match": 1, "mismatch": 1, "unknown": 1})

    def test_main_prints_text_and_json(self):
        path = write(entries())
        verdicts = write([])
        with open(verdicts, "w") as handle:
            json.dump({"b1": "pass", "rec-1": "pass"}, handle)
        for args in ([path, "--verdicts", verdicts], [path, "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(analyze_replay.main(args), 0)
            self.assertIn("30.0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
