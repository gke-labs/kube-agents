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


TEMP_DIR = None


def setUpModule():
    global TEMP_DIR
    TEMP_DIR = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def write_file(lines, kind):
    """One segment under a fresh replay-shadow/<kind>/<day>/<build>/logs/, as the collector files it."""
    logs = os.path.join(tempfile.mkdtemp(dir=TEMP_DIR), "replay-shadow", kind, "2026-11-05", "1", "logs")
    os.makedirs(logs)
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=logs)
    with handle:
        for entry in lines:
            handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")
    return handle.name


def write(lines):
    """Each line into the folder its own tag names, as an honest run files it; untagged ones under presubmit/."""
    by_kind = {}
    for entry in lines:
        tag = (entry.get("job") or {}).get("kind") if isinstance(entry, dict) else None
        by_kind.setdefault(tag if tag in analyze_shadow.JOB_KINDS else "presubmit", []).append(entry)
    return [write_file(kind_lines, kind) for kind, kind_lines in by_kind.items()] or [write_file([], "presubmit")]


def report(lines, **kwargs):
    records, skipped = analyze_shadow.load(write(lines))
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
            analyze_shadow.load(write(lines))[0], "high", merged={"10": 10.0}) if h]
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
            analyze_shadow.load(write(lines))[0], "high", merged={"10": 10.0}) if h]
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

    def test_a_nightly_untrusted_line_is_never_a_source(self):
        # Written by an account presubmits also run as, so pull-request code could have forged it.
        for pr in ("", "10"):
            with self.subTest(pr=pr):
                lines = [line("u1", 1, "nightly-untrusted", "u", "A", ["a", "b"], pr=pr),
                         presubmit("p1", 2, "10", "b1", "A", ["a", "b"])]
                rep, skipped = report(lines, merged={"10": 1.5})
                for estimate in analyze_shadow.ESTIMATES:
                    self.assertEqual(rep["estimates"][estimate]["hits"], 0, estimate)
                self.assertIsNone(rep["top_misses"][0]["closest_request_id"])
                self.assertEqual((rep["nightly_requests"], rep["nightly_untrusted_requests"]), (0, 1))
                self.assertEqual(rep["health"]["lines_by_kind"]["nightly-untrusted"], 1)
                self.assertEqual(skipped["untagged"], 0)

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
        self.assertEqual(rep["estimates"]["simulated"]["hits"], 0)

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

    def test_a_nightly_untrusted_request_is_never_closest(self):
        # n1 is a real source for the prefix, so the search reaches the text ranking,
        # where the untrusted n2 has the most alike next message. Its tags are the
        # proxy's, so it can claim to be an earlier build of p1's pull request.
        texts = analyze_shadow.StoredText(write_store(self.TEXTS))
        lines = [dict(l) for l in self.LINES]
        lines[1] = dict(lines[1], job={"kind": "nightly-untrusted", "project": "p1", "build": "b0", "pr": "10"})
        miss = report(lines, texts=texts)[0]["top_misses"][0]
        self.assertEqual(miss["closest_request_id"], "n1")

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
            analyze_shadow.main([*write(lines), "--store", root])
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
            analyze_shadow.main([*write([line("n1", 1, "nightly", "n", "A", ["a"])]), "--store", ""])

    def test_a_store_without_blobs_is_refused(self):
        root = write_store(self.TEXTS)
        shutil.rmtree(os.path.join(root, "blobs"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            analyze_shadow.main([*write([line("n1", 1, "nightly", "n", "A", ["a"])]), "--store", root])

    def test_a_store_without_requests_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            analyze_shadow.main([*write([line("n1", 1, "nightly", "n", "A", ["a"])]), "--store", TEMP_DIR + "/typo"])


class HealthTest(unittest.TestCase):
    def test_health_counts(self):
        records, skipped = analyze_shadow.load(write([
            line("n1", 1, "nightly", "n", "A", ["a"]),
            presubmit("p1", 2, "", "b1", "A", None, unstorable=["upstream_incomplete"]),
            presubmit("p2", 3, "10", "b1", "B", ["b"], unstorable=["observe_failed"]),
            presubmit("p3", 4, "10", "b1", "C", ["c"], unstorable=["fallback_attempted", "repeats_swapped_number"]),
            presubmit("p4", 4, "10", "b1", "D", ["d"], unstorable=["observe_too_large"]),
            {"event": "replay_shadow_request", "ts": 5, "error": "RuntimeError", "job": {"kind": "presubmit"}},
            {"event": "replay_shadow_request", "ts": 6, "error": "analysis_dropped", "job": {"kind": "presubmit"}},
        ]))
        rep = analyze_shadow.summarize(records, skipped=skipped)
        h = rep["health"]
        self.assertEqual(h["lines_by_kind"], {"nightly": 1, "nightly-untrusted": 0, "presubmit": 4})
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
        records, skipped = analyze_shadow.load(write([
            "not json",
            {"event": "replay_shadow_start"},
            line("x", 1, "", "b", "A", ["a"]),
            {"event": "replay_shadow_request", "ts": 1, "error": "RuntimeError", "job": {"kind": "presubmit"}},
            {"event": "replay_shadow_request", "ts": 1, "job": {"kind": "presubmit"}},
            presubmit("p", 2, "10", "b", "A", ["a"]),
        ]))
        self.assertEqual([r.request_id for r in records], ["p"])
        self.assertEqual(records[0].pr, "10")
        self.assertEqual(skipped, {"not_json": 1, "other_event": 1, "untagged": 1, "error": 1, "analysis_dropped": 0, "no_fingerprint": 1,
                                   "kind_mismatch": 0, "off_layout_files": 0})

    def test_records_from_several_files_are_merged_in_time_order(self):
        a = write([presubmit("p2", 5, "10", "b2", "A", ["a"])])
        b = write([line("n1", 1, "nightly", "n", "A", ["a"])])
        records, _ = analyze_shadow.load(a + b)
        self.assertEqual([r.request_id for r in records], ["n1", "p2"])

    def test_main_prints_text_and_json_and_reads_merge_times(self):
        path = write([presubmit("p1", 1, "10", "b", "A", ["a"]), presubmit("p2", 5, "11", "c", "A", ["a"])])
        merged = os.path.join(tempfile.mkdtemp(dir=TEMP_DIR), "merged.json")
        with open(merged, "w") as handle:
            json.dump({"10": 2}, handle)
        for args in ([*path, "--merged", merged], [*path, "--merged", merged, "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(analyze_shadow.main(args), 0)
            self.assertIn("50.0", out.getvalue())


class FolderKindTest(unittest.TestCase):
    """The folder a line was filed under is its kind; its own tag is only checked against it."""

    def test_a_presubmit_line_tagged_nightly_is_never_a_nightly_source(self):
        forged = line("f1", 1, "nightly", "n", "A", ["a"], tokens=10_000)
        files = [write_file([forged], "presubmit"), write_file([presubmit("p1", 2, "10", "b1", "A", ["a"])], "presubmit")]
        records, skipped = analyze_shadow.load(files)
        self.assertEqual([r.request_id for r in records], ["p1"])
        self.assertEqual(skipped["kind_mismatch"], 1)
        rep = analyze_shadow.summarize(records, skipped=skipped, merged={"10": 1.5})
        for estimate in analyze_shadow.ESTIMATES:
            self.assertEqual(rep["estimates"][estimate]["hits"], 0, estimate)
        self.assertEqual((rep["nightly_requests"], rep["health"]["kind_mismatch_lines"]), (0, 1))

    def test_a_nightly_untrusted_line_tagged_nightly_is_dropped(self):
        records, skipped = analyze_shadow.load([write_file([line("f1", 1, "nightly", "n", "A", ["a"])], "nightly-untrusted")])
        self.assertEqual((records, skipped["kind_mismatch"]), ([], 1))

    def test_the_kind_is_the_top_level_folder_and_a_nested_kind_folder_is_skipped(self):
        # IAM governs only the replay-shadow/<kind>/ prefix: a presubmit names everything below it,
        # so it uploads replay-shadow/presubmit/<day>/nightly/<day>/<build>/logs/seg.jsonl tagged nightly.
        week = tempfile.mkdtemp(dir=TEMP_DIR)
        honest = os.path.join(week, "presubmit", "2026-11-05", "1790", "logs")
        forged = os.path.join(week, "presubmit", "2026-11-05", "nightly", "2026-11-05", "1790", "logs")
        for directory, entry in ((honest, presubmit("p1", 2, "10", "b1", "A", ["a"])),
                                 (forged, line("f1", 1, "nightly", "n", "A", ["a"], tokens=10_000))):
            os.makedirs(directory)
            with open(os.path.join(directory, "seg.jsonl"), "w") as handle:
                handle.write(json.dumps(entry) + "\n")
        for argument in (week, os.path.join(week, "presubmit")):
            with self.subTest(argument=argument):
                records, skipped = analyze_shadow.load([argument])
                self.assertEqual([(r.request_id, r.kind) for r in records], [("p1", "presubmit")])
                self.assertEqual(skipped["off_layout_files"], 1)
                rep = analyze_shadow.summarize(records, skipped=skipped)
                for estimate in analyze_shadow.ESTIMATES:
                    self.assertEqual(rep["estimates"][estimate]["hits"], 0, estimate)
                self.assertEqual(rep["health"]["off_layout_files"], 1)
        # Named outright, the forged file is refused, not trusted.
        with self.assertRaises(ValueError):
            analyze_shadow.load([os.path.join(forged, "seg.jsonl")])

    def test_a_kind_folder_its_parent_or_a_file_inside_one_all_work(self):
        path = write_file([line("n1", 1, "nightly", "n", "A", ["a"])], "nightly")
        kind_folder = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        for argument in (path, kind_folder, os.path.dirname(kind_folder), os.path.dirname(os.path.dirname(kind_folder))):
            with self.subTest(argument=argument):
                records, skipped = analyze_shadow.load([argument])
                self.assertEqual([(r.request_id, r.kind) for r in records], [("n1", "nightly")])
                self.assertEqual(skipped["off_layout_files"], 0)

    def test_a_file_off_the_layout_inside_a_kind_folder_is_skipped(self):
        path = write_file([presubmit("p1", 2, "10", "b1", "A", ["a"])], "presubmit")
        kind_folder = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        for relative in ("stray.jsonl", "2026-11-05/stray.jsonl", "not-a-day/1/logs/seg.jsonl",
                         "2026-11-05/1/notlogs/seg.jsonl", "2026-11-05/replay-shadow/logs/seg.jsonl"):
            with self.subTest(relative=relative):
                stray = os.path.join(kind_folder, relative)
                os.makedirs(os.path.dirname(stray), exist_ok=True)
                with open(stray, "w") as handle:
                    handle.write(json.dumps(presubmit("s1", 1, "10", "b0", "A", ["a"])) + "\n")
                records, skipped = analyze_shadow.load([kind_folder])
                self.assertEqual([r.request_id for r in records], ["p1"])
                self.assertEqual(skipped["off_layout_files"], 1)
                os.remove(stray)

    def test_a_run_with_nothing_at_the_layout_is_refused(self):
        loose = tempfile.mkdtemp(dir=TEMP_DIR)
        with open(os.path.join(loose, "seg.jsonl"), "w") as handle:
            handle.write(json.dumps(presubmit("p1", 2, "10", "b1", "A", ["a"])) + "\n")
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(ValueError, "no log segment is at the bucket's layout"):
            analyze_shadow.load([loose])
        records, _ = analyze_shadow.load([loose], kind="presubmit")
        self.assertEqual([r.request_id for r in records], ["p1"])
        # No segments at all is an empty run, not a mistake.
        records, skipped = analyze_shadow.load([tempfile.mkdtemp(dir=TEMP_DIR)])
        self.assertEqual((records, skipped["off_layout_files"]), ([], 0))

    def test_a_planted_folder_with_nothing_at_the_layout_does_not_stop_the_recipe(self):
        # The reviewer's reproduction: week/nightly-untrusted holds only 2026-11-05/x.jsonl.
        week = tempfile.mkdtemp(dir=TEMP_DIR)
        for kind, relative, entry in (
                ("nightly", "2026-11-05/1790/logs/seg.jsonl", line("n1", 1, "nightly", "n", "A", ["a"])),
                ("presubmit", "2026-11-05/1791/logs/seg.jsonl", presubmit("p1", 2, "10", "b1", "A", ["a"])),
                ("nightly-untrusted", "2026-11-05/x.jsonl", line("u1", 1, "nightly-untrusted", "u", "A", ["a"]))):
            target = os.path.join(week, kind, relative)
            os.makedirs(os.path.dirname(target))
            with open(target, "w") as handle:
                handle.write(json.dumps(entry) + "\n")
        folders = [os.path.join(week, kind) for kind in ("nightly", "nightly-untrusted", "presubmit")]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(analyze_shadow.main([*folders, "--json"]), 0)
        rep = json.loads(out.getvalue())
        self.assertEqual(rep["health"]["off_layout_files"], 1)
        self.assertEqual((rep["requests"], rep["estimates"]["low"]["hits"]), (2, 1))
        self.assertIn(f"note: {folders[1]} holds no log segment at the bucket's layout", err.getvalue())

    def test_a_named_job_manifest_is_not_read_as_a_log(self):
        path = write_file([presubmit("p1", 2, "10", "b1", "A", ["a"])], "presubmit")
        manifest = os.path.join(os.path.dirname(os.path.dirname(path)), "job-1762300000000.json")
        with open(manifest, "w") as handle:
            json.dump({"kind": "presubmit", "build": "1"}, handle)
        self.assertEqual(analyze_shadow.filed_kind(manifest), "presubmit")
        for kind in (None, "presubmit"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "is not a log segment"):
                    analyze_shadow.load([manifest], kind=kind)

    def test_a_folder_inside_a_kind_folder_keeps_that_kind(self):
        path = write_file([presubmit("p1", 2, "10", "b1", "A", ["a"])], "presubmit")
        records, _ = analyze_shadow.load([os.path.dirname(os.path.dirname(os.path.dirname(path)))])
        self.assertEqual([(r.request_id, r.kind) for r in records], [("p1", "presubmit")])

    def test_filed_kind_reads_segments_and_job_manifests_at_the_layout_only(self):
        top = os.path.join(os.sep, "data", "week")
        cases = {
            "presubmit/2026-11-05/1790/logs/p-1-000001.jsonl": "presubmit",
            "nightly-untrusted/2026-11-05/1790/job-1762300000000.json": "nightly-untrusted",
            "replay-shadow/nightly/2026-11-05/1790/job-1762300000000.json": "nightly",
            "presubmit/2026-11-05/1790/logs/job-1762300000000.json": None,
            "presubmit/2026-11-05/1790/job-1.jsonl": None,
            "presubmit/2026-11-05/1790/job-x.json": None,
            "presubmit/2026-11-05/nightly/job-1762300000000.json": None,
            "presubmit/2026-11-05/nightly/2026-11-05/1790/job-1762300000000.json": None,
            "presubmit/2026-11-05/nightly/2026-11-05/1790/logs/seg.jsonl": None,
            "presubmit/2026-11-05/replay-shadow/logs/seg.jsonl": None,
        }
        for relative, kind in cases.items():
            with self.subTest(relative=relative):
                self.assertEqual(analyze_shadow.filed_kind(os.path.join(top, relative)), kind)

    def test_a_missing_path_is_named_with_or_without_a_kind(self):
        missing = os.path.join(TEMP_DIR, "no-such", "presubmit")
        for kind in (None, "presubmit"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, f"{missing} does not exist"):
                    analyze_shadow.load([missing], kind=kind)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
                    analyze_shadow.main([missing] + (["--kind", kind] if kind else []))
                self.assertEqual(exit_.exception.code, 2)
                self.assertIn(f"{missing} does not exist", err.getvalue())

    def test_a_directory_is_searched_for_segments(self):
        path = write_file([presubmit("p1", 2, "10", "b1", "A", ["a"])], "presubmit")
        with open(os.path.join(os.path.dirname(path), "job-1.json"), "w") as handle:
            handle.write("{}")
        records, skipped = analyze_shadow.load([os.path.dirname(path)])
        self.assertEqual([r.request_id for r in records], ["p1"])
        self.assertEqual(skipped["not_json"], 0)

    def test_a_file_under_no_kind_folder_is_refused_unless_a_kind_is_given(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=TEMP_DIR)
        with handle:
            handle.write(json.dumps(line("n1", 1, "nightly", "n", "A", ["a"])) + "\n")
        with self.assertRaises(ValueError):
            analyze_shadow.load([handle.name])
        records, _ = analyze_shadow.load([handle.name], kind="nightly")
        self.assertEqual([r.kind for r in records], ["nightly"])
        records, skipped = analyze_shadow.load([handle.name], kind="presubmit")
        self.assertEqual((records, skipped["kind_mismatch"]), ([], 1))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            analyze_shadow.main([handle.name])
        self.assertIn("--kind", err.getvalue())


if __name__ == "__main__":
    unittest.main()
