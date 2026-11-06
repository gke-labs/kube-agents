"""The offline would-hit analysis over synthetic shadow logs."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest

import analyze_shadow


def line(request_id, ts, kind, build, fingerprint, messages, roles=None, project="p1", tokens=100, unstorable=(),
         pr="", latency_ms=0, answer_hash="answered"):
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
    if answer_hash is not None:
        entry["answer_hash"] = answer_hash
    if messages is not None:
        entry["message_hashes"] = list(messages)
    return entry


def presubmit(request_id, ts, pr, build, fingerprint, messages, **kwargs):
    return line(request_id, ts, "presubmit", build, fingerprint, messages, pr=pr, **kwargs)


TEMP_DIR = None


def setUpModule():
    global TEMP_DIR
    TEMP_DIR = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def write(lines):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=TEMP_DIR)
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
        self.assertEqual(rep["health"]["presubmit_without_pr"], 1)

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
        self.assertEqual(rep["health"]["unstorable_lines"], 1)
        self.assertEqual(hits(lines, "low", require_storable=False), 1)

    def test_a_cut_short_upstream_answer_is_never_a_recording(self):
        lines = [
            line("n1", 1, "nightly", "n", "A", ["a"], unstorable=["upstream_incomplete"]),
            presubmit("p1", 2, "10", "b1", "A", ["a"]),
        ]
        self.assertEqual(hits(lines, "low"), 0)


# One agent conversation: system prompt, task, then answer / tool result turns.
TURN_ROLES = ["system", "user", "assistant", "tool", "assistant", "tool"]


def conversation(prefix, ts, kind, build, answers, results, pr="", **kwargs):
    """The requests of one conversation, one per turn, as the proxy logs them.

    Request k holds the system prompt and task, then k answer and tool result
    pairs. `answers` and `results` are the per-turn message hashes; the
    fingerprint covers every message, so it differs whenever any does.
    """
    out = []
    messages = ["sys", "task"]
    for turn in range(len(answers) + 1):
        roles = TURN_ROLES[:len(messages)]
        out.append(line(f"{prefix}{turn}", ts + turn, kind, build, "|".join(messages), list(messages), roles=roles,
                        pr=pr, **kwargs))
        if turn < len(answers):
            messages = messages + [answers[turn], results[turn]]
    return out


def hit_ids(lines, estimate, **kwargs):
    records, _ = analyze_shadow.load([write(lines)])
    return [h.request.request_id for _, h in analyze_shadow.iter_hits(records, estimate, **kwargs) if h]


class ReplayedTest(unittest.TestCase):
    def test_answers_that_differ_with_the_same_tool_results_are_hits(self):
        lines = (conversation("a", 1, "presubmit", "b1", ["ans-1", "ans-2"], ["out-1", "out-2"], pr="10")
                 + conversation("b", 10, "presubmit", "b2", ["ANS-1", "ANS-2"], ["out-1", "out-2"], pr="10"))
        # Exact matching stops after the first answer; replay keeps all three.
        self.assertEqual(hit_ids(lines, "high"), ["b0"])
        self.assertEqual(hit_ids(lines, "replayed_high"), ["b0", "b1", "b2"])
        rep, _ = report(lines)
        self.assertEqual(rep["estimates"]["replayed_high"]["hits"], 3)
        # The first build's own requests are half the presubmit tokens, and miss.
        self.assertEqual(rep["estimates"]["replayed_high"]["token_weighted_hit_percent"], 50.0)
        self.assertEqual(rep["estimates"]["replayed_high"]["saved_completion_tokens"], 15)

    def test_a_different_tool_result_misses_from_that_request_on(self):
        lines = (conversation("a", 1, "presubmit", "b1", ["ans-1", "ans-2"], ["out-1", "out-2"], pr="10")
                 + conversation("b", 10, "presubmit", "b2", ["ANS-1", "ANS-2"], ["OTHER", "out-2"], pr="10"))
        self.assertEqual(hit_ids(lines, "replayed_high"), ["b0"])

    def test_replayed_low_takes_nightly_sources_only(self):
        lines = (conversation("n", 1, "nightly", "n1", ["ans-1"], ["out-1"])
                 + conversation("a", 3, "presubmit", "b1", ["ans-x"], ["out-x"], pr="10")
                 + conversation("b", 10, "presubmit", "b2", ["ANS-1"], ["out-1"], pr="10")
                 + conversation("c", 20, "presubmit", "b3", ["ANS-X"], ["out-x"], pr="10"))
        self.assertEqual(hit_ids(lines, "replayed_low"), ["a0", "b0", "b1", "c0"])
        self.assertEqual(hit_ids(lines, "replayed_high"), ["a0", "b0", "b1", "c0", "c1"])

    def test_a_source_replay_could_not_use_never_counts(self):
        cases = {
            # Another pull request, with no merge time given.
            "another pr": conversation("a", 1, "presubmit", "b1", ["ans-1"], ["out-1"], pr="11"),
            # The same build: replay never serves a build its own answers.
            "same build": conversation("a", 1, "presubmit", "b2", ["ans-1"], ["out-1"], pr="10"),
            # An earlier build still running when the request was made.
            "unfinished build": conversation("a", 1, "presubmit", "b1", ["ans-1"], ["out-1"], pr="10")
            + [presubmit("late", 30, "10", "b1", "Z", ["z"])],
            # A nightly whose answer came back after the request.
            "later nightly": conversation("n", 1, "nightly", "n1", ["ans-1"], ["out-1"], latency_ms=60_000),
            # A sighting after the request.
            "later sighting": conversation("a", 40, "presubmit", "b1", ["ans-1"], ["out-1"], pr="10"),
        }
        for name, sources in cases.items():
            with self.subTest(name):
                lines = sources + conversation("b", 20, "presubmit", "b2", ["ANS-1"], ["out-1"], pr="10")
                found = [i for i in hit_ids(lines, "replayed_high") if i.startswith("b")]
                # b0 can match the same build's or a later sighting's first request
                # only if replay could use it; none of these sources qualify.
                self.assertEqual(found, [], name)

    def test_another_prs_last_build_counts_only_once_merged(self):
        lines = (conversation("a", 1, "presubmit", "b1", ["ans-1"], ["out-1"], pr="11")
                 + conversation("b", 5, "presubmit", "b2", ["ANS-1"], ["out-1"], pr="10")    # before PR 11 merged
                 + conversation("c", 20, "presubmit", "b3", ["ANS-X"], ["out-1"], pr="12"))  # after
        self.assertEqual(hit_ids(lines, "replayed_high", merged={"11": 10.0}), ["c0", "c1"])
        self.assertEqual(hit_ids(lines, "replayed_low", merged={"11": 10.0}), [])

    def test_an_unstorable_source_is_not_a_recording(self):
        lines = (conversation("a", 1, "presubmit", "b1", ["ans-1"], ["out-1"], pr="10",
                              unstorable=["fallback_attempted"])
                 + conversation("b", 10, "presubmit", "b2", ["ANS-1"], ["out-1"], pr="10"))
        self.assertEqual(hit_ids(lines, "replayed_high"), [])
        self.assertEqual(hit_ids(lines, "replayed_high", require_storable=False), ["b0", "b1"])

    def test_settings_length_and_roles_must_match(self):
        # Each build's request for the first answer, so the answers are generated ones.
        asked = [presubmit("a-ask", 0.5, "10", "b1", "AQ", ["sys", "task"], roles=["system", "user"]),
                 presubmit("b-ask", 9, "10", "b2", "BQ", ["sys", "task"], roles=["system", "user"])]
        source = presubmit("a", 1, "10", "b1", "A", ["sys", "task", "ans", "out"],
                           roles=["system", "user", "assistant", "tool"])
        cases = {
            "settings": dict(settings_hash="other-tools"),
            "one message fewer": dict(message_hashes=["sys", "task", "ANS"], roles=["system", "user", "assistant"]),
            "a user message where the answer was": dict(roles=["system", "user", "user", "tool"]),
            "the same hash under another role": dict(roles=["system", "tool", "assistant", "tool"]),
            "a different task": dict(message_hashes=["sys", "TASK", "ANS", "out"]),
            "a different system prompt": dict(message_hashes=["SYS", "task", "ANS", "out"]),
        }
        for name, change in cases.items():
            with self.subTest(name):
                request = presubmit("b", 10, "10", "b2", "B", ["sys", "task", "ANS", "out"],
                                    roles=["system", "user", "assistant", "tool"])
                request.update(change)
                self.assertNotIn("b", hit_ids(asked + [source, request], "replayed_high"))
        request = presubmit("b", 10, "10", "b2", "B", ["sys", "task", "ANS", "out"],
                            roles=["system", "user", "assistant", "tool"])
        self.assertEqual(hit_ids(asked + [source, request], "replayed_high"), ["b-ask", "b"])

    def test_an_exact_match_without_message_hashes_still_counts(self):
        lines = [line("n1", 1, "nightly", "n", "A", None), presubmit("p1", 2, "10", "b1", "A", None),
                 presubmit("p2", 3, "10", "b1", "B", None)]
        self.assertEqual(hits(lines, "replayed_low"), 1)
        self.assertEqual(hits(lines, "replayed_high"), 1)

    def test_replayed_is_never_below_its_exact_estimate(self):
        # Same fingerprint, but message hashes that differ (lines from before a
        # hashing change): the exact estimates hit, so the replayed ones must too.
        lines = [line("n1", 1, "nightly", "n", "A", ["x"]), presubmit("p1", 2, "10", "b1", "A", ["y"])]
        for exact, replayed in (("low", "replayed_low"), ("high", "replayed_high")):
            self.assertEqual(hits(lines, exact), 1)
            self.assertEqual(hits(lines, replayed), 1)

    def test_replayed_misses_by_first_differing_message_that_is_not_an_answer(self):
        lines = (conversation("a", 1, "nightly", "n1", ["ans-1", "ans-2"], ["out-1", "out-2"])
                 # A different second tool result: its last request misses at the tool.
                 + conversation("b", 10, "presubmit", "b2", ["ANS-1", "ANS-2"], ["out-1", "OTHER"], pr="10",
                                tokens=300)
                 + [presubmit("c", 20, "10", "b2", "C", ["sys", "TASK"], roles=["system", "user"], tokens=40),
                    presubmit("d", 21, "10", "b2", "D", ["SYS"], roles=["system"], tokens=5)])
        other = presubmit("e", 22, "10", "b2", "E", ["sys"], roles=["system"], tokens=7)
        other["settings_hash"] = "other-tools"
        rep, _ = report(lines + [other, presubmit("f", 23, "10", "b2", "F", None, tokens=2)])
        self.assertEqual(rep["replayed_miss_tokens_by_first_difference"],
                         {"tool": 300, "user": 40, "settings": 7, "system": 5, "unhashed": 2})
        # The exact breakdown names the answer that diverged first; the replayed one looks past it.
        self.assertEqual(rep["miss_tokens_by_first_difference"]["assistant"], 600)

    def test_the_breakdown_takes_the_high_sources(self):
        roles = ["system", "user", "assistant", "tool"]
        cases = {
            # An earlier finished build of the same pull request.
            "earlier build": ("10", {}),
            # Another pull request's last build, once it merged.
            "merge copy": ("11", {"11": 10.0}),
        }
        for name, (source_pr, merged) in cases.items():
            with self.subTest(name):
                lines = [
                    presubmit("a-ask", 0.5, source_pr, "b1", "AQ", ["sys", "task"], roles=roles[:2]),
                    presubmit("a", 1, source_pr, "b1", "A", ["sys", "task", "ans", "out"], roles=roles),
                    presubmit("b-ask", 19, "10", "b2", "AQ", ["sys", "task"], roles=roles[:2]),
                    presubmit("b", 20, "10", "b2", "B", ["sys", "task", "ANS", "OTHER"], roles=roles),
                ]
                rep, _ = report(lines, merged=merged)
                # The source build's own requests have nothing earlier: settings.
                self.assertEqual(rep["replayed_miss_tokens_by_first_difference"], {"tool": 100, "settings": 200})

    def test_a_longer_recording_with_no_request_stopping_here_is_none(self):
        source = line("a", 1, "nightly", "n1", "A", ["sys", "task", "ans", "out"],
                      roles=["system", "user", "assistant", "tool"])
        request = presubmit("b", 10, "10", "b2", "B", ["sys", "task"], roles=["system", "user"])
        rep, _ = report([source, request])
        self.assertEqual(rep["replayed_miss_tokens_by_first_difference"], {"none": 100})

    def test_the_output_names_the_replayed_rows_as_estimates(self):
        lines = (conversation("a", 1, "presubmit", "b1", ["ans-1"], ["out-1"], pr="10")
                 + conversation("b", 10, "presubmit", "b2", ["ANS-1"], ["out-1"], pr="10"))
        rep, skipped = report(lines)
        text = analyze_shadow.render(rep, skipped)
        self.assertIn("replayed_high", text)
        self.assertIn("replayed rows are an estimate", text)
        self.assertIn("by role of first differing message", text)


FEW_SHOT_ROLES = ["system", "user", "assistant", "user"]
ANSWER_ROLES = ["system", "user", "assistant", "tool"]


class GeneratedAnswerTest(unittest.TestCase):
    """Only assistant messages the logs show the model gave in the conversation are held to the recording."""

    def test_a_prompt_borne_assistant_message_that_differs_misses(self):
        # A few-shot example arrives in the first request: no earlier request asked for it.
        for example, expected in (("example-2", []), ("example-1", ["b"])):
            with self.subTest(example):
                lines = [presubmit("a", 1, "10", "b1", "A", ["sys", "task", "example-1", "go"], roles=FEW_SHOT_ROLES),
                         presubmit("b", 10, "10", "b2", "B", ["sys", "task", example, "go"], roles=FEW_SHOT_ROLES)]
                self.assertEqual(hit_ids(lines, "replayed_high"), expected)

    def test_a_sources_prompt_borne_message_is_not_blanked_either(self):
        lines = [
            presubmit("a", 1, "10", "b1", "A", ["sys", "task", "example-1", "out"], roles=ANSWER_ROLES),
            # In b2 the message at position 2 is a generated answer; in the source it was prompt.
            presubmit("b-ask", 9, "10", "b2", "BQ", ["sys", "task"], roles=ANSWER_ROLES[:2]),
            presubmit("b", 10, "10", "b2", "B", ["sys", "task", "ANS", "out"], roles=ANSWER_ROLES),
        ]
        self.assertEqual(hit_ids(lines, "replayed_high"), [])

    def test_a_generated_answer_that_differs_is_still_a_hit(self):
        lines = [
            presubmit("a-ask", 0.5, "10", "b1", "AQ", ["sys", "task"], roles=ANSWER_ROLES[:2]),
            presubmit("a", 1, "10", "b1", "A", ["sys", "task", "ans", "out"], roles=ANSWER_ROLES),
            presubmit("b-ask", 9, "10", "b2", "AQ", ["sys", "task"], roles=ANSWER_ROLES[:2]),
            presubmit("b", 10, "10", "b2", "B", ["sys", "task", "ANS", "out"], roles=ANSWER_ROLES),
        ]
        self.assertEqual(hit_ids(lines, "replayed_high"), ["b-ask", "b"])

    def test_without_evidence_of_the_answer_the_message_keeps_its_hash(self):
        source = [presubmit("a-ask", 0.5, "10", "b1", "AQ", ["sys", "task"], roles=ANSWER_ROLES[:2]),
                  presubmit("a", 1, "10", "b1", "A", ["sys", "task", "ans", "out"], roles=ANSWER_ROLES)]
        request = presubmit("b", 10, "10", "b2", "B", ["sys", "task", "ANS", "out"], roles=ANSWER_ROLES)
        cases = {
            # The request that got the answer is not in the logs.
            "no asking request": [],
            # Same build, length and last message, but an earlier message differs.
            "asked a different conversation": [
                presubmit("b-ask", 9, "10", "b2", "BQ", ["other-sys", "task"], roles=ANSWER_ROLES[:2])],
            "asked in another build": [presubmit("c-ask", 8, "10", "b3", "CQ", ["sys", "task"], roles=ANSWER_ROLES[:2])],
            "answer arrived after the request": [
                presubmit("b-ask", 9, "10", "b2", "BQ", ["sys", "task"], roles=ANSWER_ROLES[:2], latency_ms=5_000)],
            "no answer": [presubmit("b-ask", 9, "10", "b2", "BQ", ["sys", "task"], roles=ANSWER_ROLES[:2],
                                    unstorable=["no_answer"])],
            "client disconnected": [presubmit("b-ask", 9, "10", "b2", "BQ", ["sys", "task"], roles=ANSWER_ROLES[:2],
                                              unstorable=["client_disconnected"])],
            "no answer hash": [presubmit("b-ask", 9, "10", "b2", "BQ", ["sys", "task"], roles=ANSWER_ROLES[:2],
                                         answer_hash=None)],
        }
        for name, asked in cases.items():
            with self.subTest(name):
                self.assertNotIn("b", hit_ids(source + asked + [request], "replayed_high"))


def with_diag(entry, diag):
    entry["diag_message_hashes"] = list(diag)
    return entry


class DriftedEvidenceTest(unittest.TestCase):
    """Evidence of a generated answer compares diagnosis hashes, which don't drift within a conversation."""

    def drifted(self, drifted_prefix):
        # Each build asks for the first answer, then a later request's earlier
        # message hashes have drifted to `drifted_prefix`; its diagnosis hashes have not.
        lines = []
        for build, ts, answer in (("b1", 0, "ans"), ("b2", 10, "ANS")):
            lines.append(with_diag(presubmit(f"{build}-ask", ts, "10", build, f"{build}Q", ["sys", "task"],
                                             roles=ANSWER_ROLES[:2]), ["d-sys", "d-task"]))
            lines.append(with_diag(presubmit(f"{build}-later", ts + 1, "10", build, f"{build}L",
                                             drifted_prefix + [answer, "out"], roles=ANSWER_ROLES),
                                   ["d-sys", "d-task", f"d-{answer}", "d-out"]))
        return hit_ids(lines, "replayed_high")

    def test_a_cidr_first_seen_in_a_later_tool_result_keeps_the_evidence(self):
        # The CIDR in "out" keeps every earlier IP literal, so sys and task hash differently.
        self.assertEqual(self.drifted(["sys-ips-literal", "task-ips-literal"]), ["b2-ask", "b2-later"])

    def test_a_timestamp_crossing_a_bucket_keeps_the_evidence(self):
        # The task's timestamp is now an hour old rather than minutes.
        self.assertEqual(self.drifted(["sys", "task-older-bucket"]), ["b2-ask", "b2-later"])

    def test_message_hashes_decide_when_a_record_lacks_diagnosis_hashes(self):
        lines = [
            presubmit("a-ask", 0, "10", "b1", "AQ", ["sys", "task"], roles=ANSWER_ROLES[:2]),
            with_diag(presubmit("a", 1, "10", "b1", "A", ["sys", "task", "ans", "out"], roles=ANSWER_ROLES),
                      ["d-sys", "d-task", "d-ans", "d-out"]),
            presubmit("b-ask", 10, "10", "b2", "AQ", ["sys", "task"], roles=ANSWER_ROLES[:2]),
            with_diag(presubmit("b", 11, "10", "b2", "B", ["sys", "task", "ANS", "out"], roles=ANSWER_ROLES),
                      ["d-sys", "d-task", "d-ANS", "d-out"]),
        ]
        self.assertEqual(hit_ids(lines, "replayed_high"), ["b-ask", "b"])

    def test_diagnosis_hashes_decide_when_both_records_carry_them(self):
        # Equal message hashes, but the diagnosis hashes show another conversation asked.
        lines = []
        for build, ts, answer in (("b1", 0, "ans"), ("b2", 10, "ANS")):
            lines.append(with_diag(presubmit(f"{build}-ask", ts, "10", build, f"{build}Q", ["sys", "task"],
                                             roles=ANSWER_ROLES[:2]), ["d-sys", "d-other-task"]))
            lines.append(with_diag(presubmit(f"{build}-later", ts + 1, "10", build, f"{build}L",
                                             ["sys", "task", answer, "out"], roles=ANSWER_ROLES),
                                   ["d-sys", "d-task", f"d-{answer}", "d-out"]))
        self.assertEqual(hit_ids(lines, "replayed_high"), ["b2-ask"])


class EarliestSourceTest(unittest.TestCase):
    """Two sources in one earlier build under the same key: the earlier one is used."""

    def test_the_hit_is_served_from_the_earliest_source_of_the_build(self):
        lines = [
            presubmit("a1", 1, "10", "b1", "A", ["a"]),
            presubmit("a2", 2, "10", "b1", "A", ["a"]),
            presubmit("p", 10, "10", "b2", "A", ["a"]),
        ]
        records, _ = analyze_shadow.load([write(lines)])
        recordings = [h.recording.request_id for r, h in analyze_shadow.iter_hits(records, "high") if h]
        self.assertEqual(recordings, ["a1"])

    def test_the_closest_request_is_the_earliest_source_of_the_build(self):
        rep, _ = report([
            presubmit("a1", 1, "10", "b1", "A", ["s", "u", "t1"]),
            presubmit("a2", 2, "10", "b1", "B", ["s", "u", "t2"]),
            presubmit("p", 10, "10", "b2", "C", ["s", "u", "t3"], tokens=500),
        ])
        top = rep["top_misses"][0]
        self.assertEqual(top["request_id"], "p")
        self.assertEqual(top["closest_request_id"], "a1")
        self.assertEqual(top["matched_messages"], 2)


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

    def test_a_settings_difference_is_reported_as_settings(self):
        first = line("n1", 1, "nightly", "n", "A", ["s", "u"], roles=["system", "user"])
        second = presubmit("p1", 2, "10", "b1", "B", ["s", "u"], roles=["system", "user"])
        second["settings_hash"] = "other-tools"
        rep, _ = report([first, second])
        self.assertEqual(rep["top_misses"][0]["first_difference_role"], "settings")
        self.assertIsNone(rep["top_misses"][0]["closest_request_id"])

    def test_closest_search_uses_diagnosis_hashes_when_logged(self):
        # message_hashes drifted (a later CIDR changed message 1's IPs); the
        # diagnosis hashes did not, so the search still finds two matching messages.
        first = line("n1", 1, "nightly", "n", "A", ["s", "u1"], roles=["system", "user"])
        first["diag_message_hashes"] = ["ds", "du"]
        second = presubmit("p1", 2, "10", "b1", "B", ["s", "u1-drifted", "t"], roles=["system", "user", "tool"])
        second["diag_message_hashes"] = ["ds", "du", "dt"]
        rep, _ = report([first, second])
        self.assertEqual(rep["top_misses"][0]["matched_messages"], 2)
        self.assertEqual(rep["top_misses"][0]["first_difference_role"], "tool")
        # Matching itself still uses message_hashes and the fingerprint.
        self.assertEqual(rep["estimates"]["replayed_high"]["hits"], 0)

    def test_closest_search_uses_the_diag_settings_hash(self):
        # A later message's CIDR changed settings_hash (tools mention an IP);
        # diag_settings_hash did not, so the miss is the new message, not "settings".
        first = line("n1", 1, "nightly", "n", "A", ["s", "u"], roles=["system", "user"])
        first.update(diag_message_hashes=["ds", "du"], diag_settings_hash="dset")
        second = presubmit("p1", 2, "10", "b1", "B", ["s", "u", "c"], roles=["system", "user", "user"])
        second.update(settings_hash="drifted", diag_message_hashes=["ds", "du", "dc"], diag_settings_hash="dset")
        rep, _ = report([first, second])
        miss = rep["top_misses"][0]
        self.assertEqual(miss["closest_request_id"], "n1")
        self.assertEqual(miss["matched_messages"], 2)
        self.assertEqual(miss["first_difference_role"], "user")

    def test_estimated_tokens_weigh_in_and_are_reported(self):
        lines = [
            line("n1", 1, "nightly", "n", "A", ["a"]),
            presubmit("p1", 2, "10", "b1", "A", ["a"], tokens=300),
            presubmit("p2", 3, "10", "b1", "B", ["b"], tokens=100),
        ]
        lines[1]["tokens_estimated"] = True
        rep, _ = report(lines)
        self.assertEqual(rep["estimates"]["low"]["token_weighted_hit_percent"], 75.0)
        self.assertEqual(rep["health"]["estimated_token_percent"], 60.0)  # of all lines, nightly included

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


def write_store(requests):
    """A REPLAY_STORE_DIR holding `requests`: request_id -> list of message texts."""
    root = tempfile.mkdtemp(dir=TEMP_DIR)
    os.makedirs(os.path.join(root, "blobs"))
    os.makedirs(os.path.join(root, "requests"))
    for request_id, messages in requests.items():
        blobs = []
        for text in messages:
            # As request_store.py writes it: the message as compact JSON.
            blob = json.dumps({"content": text, "role": "tool"}, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(blob.encode()).hexdigest()
            with open(os.path.join(root, "blobs", digest + ".json"), "w") as handle:
                handle.write(blob)
            blobs.append(digest)
        with open(os.path.join(root, "requests", request_id + ".json"), "w") as handle:
            json.dump({"request_id": request_id, "messages": blobs}, handle)
    return root


class ClosestByTextTest(unittest.TestCase):
    LINES = [
        line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"], roles=["system", "user", "tool"]),
        line("n2", 1.5, "nightly", "n", "B", ["s", "w", "tb"], roles=["system", "user", "tool"]),
        presubmit("p1", 2, "10", "b1", "C", ["s", "w", "tc"], roles=["system", "user", "tool"]),
    ]
    TEXTS = {
        "n1": ["sys", "work kanban task", "card: list the namespaces on cluster alpha"],
        "n2": ["sys", "work kanban task", "card: check kube-system pods on cluster beta and report restarts"],
        "p1": ["sys", "work kanban task", "card: check kube-system pods on cluster beta and report restart counts"],
    }

    def test_without_the_store_the_earliest_request_sharing_the_prefix_is_closest(self):
        miss = report(self.LINES)[0]["top_misses"][0]
        self.assertEqual((miss["closest_request_id"], miss["matched_messages"]), ("n1", 2))

    def test_with_the_store_the_one_whose_next_message_is_most_alike_is_closest(self):
        texts = analyze_shadow.StoredText(write_store(self.TEXTS))
        miss = report(self.LINES, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_request_id"], miss["matched_messages"]), ("n2", 2))

    def test_a_request_replay_could_not_have_used_is_never_closest(self):
        # n2 finished after p1 started, so it was not available to p1.
        lines = [dict(l) for l in self.LINES]
        lines[1]["ts"] = 3
        texts = analyze_shadow.StoredText(write_store(self.TEXTS))
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual(miss["closest_request_id"], "n1")

    def test_multi_line_output_is_compared_line_by_line_not_as_json(self):
        # The stored blob is JSON, so read raw, a word at a line break glues to
        # the next line's first word. n1 lists the same pods in another order
        # (identical words); n2 shares the first two lines and nothing else.
        texts = {
            "n1": ["sys", "work", "web-c 1/1\nweb-a 1/1\nweb-b 2/2"],
            "n2": ["sys", "work", "web-a 1/1\nweb-b 2/2\napi-x 0/3"],
            "p1": ["sys", "work", "web-a 1/1\nweb-b 2/2\nweb-c 1/1"],
        }
        lines = [dict(l) for l in self.LINES]
        lines[0]["ts"], lines[1]["ts"] = 1.5, 1  # n2 is the earliest, so the fallback would pick it
        miss = report(lines, texts=analyze_shadow.StoredText(write_store(texts)))[0]["top_misses"][0]
        self.assertEqual(miss["closest_request_id"], "n1")

    def test_tool_call_arguments_count_as_words(self):
        message = {"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c1", "function": {"name": "kubectl", "arguments": "get pods -n web"}}]}
        self.assertEqual(analyze_shadow.message_text(message), "kubectl\nget pods -n web")

    def test_missing_text_falls_back_to_the_earliest(self):
        texts = analyze_shadow.StoredText(write_store({"n1": self.TEXTS["n1"]}))
        miss = report(self.LINES, texts=texts)[0]["top_misses"][0]
        self.assertEqual(miss["closest_request_id"], "n1")


class ClosestByTextRulesTest(unittest.TestCase):
    """The text ranking never reports a request replay could not have served."""

    TEXTS = {
        "n1": ["sys", "work", "card: list the namespaces on cluster alpha"],
        "x": ["sys", "work", "card: check kube-system pods on cluster beta and report restarts"],
        "p1": ["sys", "work", "card: check kube-system pods on cluster beta and report restart counts"],
    }

    def closest(self, candidate, extra=(), merged=None):
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]), candidate, *extra,
                 presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        texts = analyze_shadow.StoredText(write_store(self.TEXTS))
        rep, _ = report(lines, texts=texts, merged=merged or {}, top=100)
        return next(m for m in rep["top_misses"] if m["request_id"] == "p1")["closest_request_id"]

    def test_an_earlier_finished_build_of_the_same_pr_is_usable(self):
        self.assertEqual(self.closest(presubmit("x", 2, "10", "b1", "X", ["s", "w", "tx"])), "x")

    def test_an_unfinished_build_of_the_same_pr_is_not(self):
        still_running = presubmit("x2", 12, "10", "b1", "Y", ["q"])
        self.assertEqual(self.closest(presubmit("x", 2, "10", "b1", "X", ["s", "w", "tx"]), [still_running]), "n1")

    def test_another_prs_build_is_usable_only_as_the_merge_copy_after_it_merged(self):
        other = presubmit("x", 2, "11", "c1", "X", ["s", "w", "tx"])
        self.assertEqual(self.closest(other), "n1")
        self.assertEqual(self.closest(other, merged={"11": 5.0}), "x")
        self.assertEqual(self.closest(other, merged={"11": 20.0}), "n1")

    def test_the_most_recent_requests_are_compared_when_there_are_more_than_the_cap(self):
        count = analyze_shadow.MAX_PREFIX_CANDIDATES + 6
        lines = [line(f"n{i}", 1 + i, "nightly", "n", f"A{i}", ["s", "w", f"t{i}"]) for i in range(count)]
        lines.append(presubmit("p1", 1000, "10", "b2", "C", ["s", "w", "tc"]))
        texts = {f"n{i}": ["sys", "work", f"card number {i} about something else"] for i in range(count)}
        texts[f"n{count - 1}"] = texts["p1"] = ["sys", "work", "card: check kube-system pods on cluster beta"]
        rep, _ = report(lines, texts=analyze_shadow.StoredText(write_store(texts)))
        self.assertEqual(rep["top_misses"][0]["closest_request_id"], f"n{count - 1}")

    def test_an_unreadable_stored_file_is_skipped_and_the_readable_one_ranked(self):
        root = write_store(self.TEXTS)
        with open(os.path.join(root, "requests", "x.json"), "wb") as handle:
            handle.write(b"\xff\xfe{")
        texts = analyze_shadow.StoredText(root)
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]),
                 line("x", 1.5, "nightly", "n", "X", ["s", "w", "tx"]),
                 presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_request_id"], miss["closest_by"]), ("n1", "text"))

    def test_an_unreadable_newest_copy_does_not_hide_an_older_readable_one(self):
        # n_new carries the same next message as n_old but its text was never
        # stored; the unrelated n_other sits between them.
        lines = [line("n_old", 1, "nightly", "n", "A", ["s", "w", "tx"]),
                 line("n_other", 1.5, "nightly", "n", "B", ["s", "w", "ty"]),
                 line("n_new", 1.8, "nightly", "n", "A2", ["s", "w", "tx"]),
                 presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        texts = analyze_shadow.StoredText(write_store({
            "n_old": self.TEXTS["x"],
            "n_other": ["sys", "work", "card: open a pull request bumping a node pool version for cluster beta"],
            "p1": self.TEXTS["p1"],
        }))
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_request_id"], miss["closest_by"]), ("n_old", "text"))

    def test_the_report_says_how_each_closest_request_was_chosen(self):
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]),
                 line("x", 1.5, "nightly", "n", "X", ["s", "w", "tx"]),
                 presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"], tokens=300),
                 presubmit("p2", 11, "10", "b2", "D", ["s", "w", "td"], tokens=200)]
        texts = analyze_shadow.StoredText(write_store({
            "n1": self.TEXTS["n1"], "x": self.TEXTS["x"], "p1": self.TEXTS["p1"]}))
        rep, _ = report(lines, texts=texts)
        by = {m["request_id"]: (m["closest_by"], m["unranked_reason"]) for m in rep["top_misses"]}
        self.assertEqual(by["p1"], ("text", None))
        self.assertEqual(by["p2"], ("earliest", "own_text_missing"))
        self.assertEqual(rep["closest_by_text"]["ranked_by_text"], 1)
        self.assertEqual(rep["closest_by_text"]["earliest_because"]["own_text_missing"], 1)
        self.assertNotIn("closest_by", report(lines)[0]["top_misses"][0], "without --store nothing is claimed")

    def test_no_readable_candidate_is_reported_as_such(self):
        texts = analyze_shadow.StoredText(write_store({"p1": self.TEXTS["p1"]}))
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]),
                 presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_request_id"], miss["closest_by"], miss["unranked_reason"]),
                         ("n1", "earliest", "no_candidate_text"))

    def test_all_messages_matching_is_reported_as_nothing_to_rank(self):
        texts = analyze_shadow.StoredText(write_store(self.TEXTS))
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w"]), presubmit("p1", 10, "10", "b2", "B", ["s", "w"])]
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_by"], miss["unranked_reason"]), ("earliest", "all_messages_matched"))

    def test_the_text_output_counts_the_ranked_misses(self):
        root = write_store(self.TEXTS)
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]),
                 line("x", 1.5, "nightly", "n", "X", ["s", "w", "tx"]),
                 presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            analyze_shadow.main([write(lines), "--store", root])
        self.assertIn("closest request ranked by text for 1 of 1 misses", out.getvalue())
        self.assertRegex(out.getvalue(), r"\(nightly, by text 0\.\d+\)")

    def test_no_shared_word_is_no_match(self):
        # The only candidate with text shares no word with the miss: no text match, whatever its recency.
        texts = analyze_shadow.StoredText(write_store({
            "n1": ["sys", "work", "alpha beta gamma"], "p1": ["sys", "work", "delta epsilon zeta"]}))
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]), presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_by"], miss["unranked_reason"], miss["closest_score"]),
                         ("earliest", "no_shared_words", None))

    def test_an_empty_next_message_is_not_ranked(self):
        # Two empty messages would score a perfect 1.0 against each other.
        texts = analyze_shadow.StoredText(write_store({"n1": ["sys", "work", ""], "p1": ["sys", "work", ""]}))
        lines = [line("n1", 1, "nightly", "n", "A", ["s", "w", "ta"]), presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual((miss["closest_by"], miss["unranked_reason"]), ("earliest", "own_text_empty"))

    def test_a_text_pick_carries_its_score(self):
        texts = analyze_shadow.StoredText(write_store(self.TEXTS))
        lines = [line("x", 1, "nightly", "n", "X", ["s", "w", "tx"]), presubmit("p1", 10, "10", "b2", "C", ["s", "w", "tc"])]
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual(miss["closest_by"], "text")
        self.assertGreater(miss["closest_score"], 0.5)

    def test_an_empty_store_path_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            analyze_shadow.main([write([line("n1", 1, "nightly", "n", "A", ["a"])]), "--store", ""])

    def test_a_store_without_blobs_is_refused(self):
        root = write_store(self.TEXTS)
        shutil.rmtree(os.path.join(root, "blobs"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            analyze_shadow.main([write([line("n1", 1, "nightly", "n", "A", ["a"])]), "--store", root])

    def test_a_store_without_requests_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            analyze_shadow.main([write([line("n1", 1, "nightly", "n", "A", ["a"])]), "--store", TEMP_DIR + "/typo"])


class HealthTest(unittest.TestCase):
    def test_health_counts(self):
        records, skipped = analyze_shadow.load([write([
            line("n1", 1, "nightly", "n", "A", ["a"]),
            presubmit("p1", 2, "", "b1", "A", None, unstorable=["upstream_incomplete"]),
            presubmit("p2", 3, "10", "b1", "B", ["b"], unstorable=["observe_failed"]),
            presubmit("p3", 4, "10", "b1", "C", ["c"], unstorable=["fallback_attempted", "repeats_swapped_number"]),
            presubmit("p4", 4, "10", "b1", "D", ["d"], unstorable=["observe_too_large"]),
            {"event": "replay_shadow_request", "ts": 5, "error": "RuntimeError", "job": {"kind": "presubmit"}},
            {"event": "replay_shadow_request", "ts": 6, "error": "analysis_dropped", "job": {"kind": "presubmit"}},
        ])])
        rep = analyze_shadow.summarize(records, skipped=skipped)
        h = rep["health"]
        self.assertEqual(h["lines_by_kind"], {"nightly": 1, "presubmit": 4})
        self.assertEqual(h["observe_too_large"], 1)
        self.assertEqual(h["error_lines"], 1)
        self.assertEqual(h["analysis_dropped"], 1)
        self.assertEqual(h["presubmit_without_pr"], 1)
        self.assertEqual(h["without_message_hashes"], 1)
        self.assertEqual(h["upstream_incomplete"], 1)
        self.assertEqual(h["observe_failed"], 1)
        self.assertEqual(h["unstorable_percent_by_reason"]["fallback_attempted"], 20.0)
        self.assertIn("health (check after the first day)", analyze_shadow.render(rep, skipped))


class LoadTest(unittest.TestCase):
    def test_skips_other_lines_and_untagged_jobs(self):
        records, skipped = analyze_shadow.load([write([
            "not json",
            {"event": "replay_shadow_start"},
            line("x", 1, "", "b", "A", ["a"]),
            {"event": "replay_shadow_request", "ts": 1, "error": "RuntimeError", "job": {"kind": "presubmit"}},
            {"event": "replay_shadow_request", "ts": 1, "job": {"kind": "presubmit"}},
            presubmit("p", 2, "10", "b", "A", ["a"]),
        ])])
        self.assertEqual([r.request_id for r in records], ["p"])
        self.assertEqual(records[0].pr, "10")
        self.assertEqual(skipped, {"not_json": 1, "other_event": 1, "untagged": 1, "error": 1, "analysis_dropped": 0, "no_fingerprint": 1})

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
            self.assertIn("replayed_miss_tokens_by_first_difference" if "--json" in args else "replayed_low",
                          out.getvalue())


if __name__ == "__main__":
    unittest.main()
