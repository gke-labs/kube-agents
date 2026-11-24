"""The replay lane's download: what recent records confirmed, at this version, and nothing outside the layout."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import fetch_cache as FC

BUCKET = "replay-cache-bucket"
VERSION = "f246cb61aa625224"
OTHER_VERSION = "0123456789abcdef"
NOW = 1_800_000_000.0
HOUR = 3600


def fp(n):
    return f"{n:064x}"


class FakeBucket:
    """`gcloud storage ls -l` and `cp -I` over a directory; each object carries a creation time."""

    def __init__(self, root):
        self.root = root
        self.created = {}
        self.extra_listing = ""
        # The listing's outcome when not a plain success: (status, stderr), or None for a timed-out call.
        self.ls_result = ("ok",)
        self.copied = []

    def put(self, key, content, created=NOW):
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
        self.created[key] = created

    def __call__(self, kind, cmd, timeout, input=None, env=None):
        prefix = f"gs://{BUCKET}/"
        if cmd[:4] == ["gcloud", "storage", "ls", "-l"]:
            if self.ls_result is None:
                return None
            if self.ls_result[0] != "ok":
                return subprocess.CompletedProcess(cmd, self.ls_result[0], b"", self.ls_result[1])
            listed = cmd[4][len(prefix):].rstrip("*")
            lines = []
            for key, created in sorted(self.created.items()):
                if key.startswith(listed):
                    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
                    path = os.path.join(self.root, key)
                    size = os.path.getsize(path) if os.path.isfile(path) else 0
                    lines.append(f"{size:>10}  {stamp}  {prefix}{key}")
            lines.append(f"TOTAL: {len(lines)} objects")
            return subprocess.CompletedProcess(cmd, 0, ("\n".join(lines) + "\n" + self.extra_listing).encode(), b"")
        assert cmd[:3] == ["gcloud", "storage", "cp"] and "-I" in cmd, cmd
        dest = cmd[-1]
        os.makedirs(dest, exist_ok=True)
        for url in filter(None, input.decode().split("\n")):
            self.copied.append(url[len(prefix):])
            src = os.path.join(self.root, url[len(prefix):])
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(dest, os.path.basename(url)))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


class FetchTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.bucket = FakeBucket(os.path.join(root, "bucket"))
        self.dest = os.path.join(root, "cache")
        self.runs_out = os.path.join(root, "runs.json")
        self.sizes = {}
        self.targets = {}

    def fetcher(self, **overrides):
        settings = dict(bucket=BUCKET, version=VERSION, run=self.bucket, clock=lambda: NOW)
        settings.update(overrides)
        return FC.Fetcher(**settings)

    NEXT_GITHUB = {"name": "ci-kube-agents-eval-nightly", "mode": "next", "forge": "github"}

    def record(self, build, ms, objects, runs=(), created=NOW, job=NEXT_GITHUB, produced=None, counts=None):
        """A publish record; `objects` maps a version to names, or to (name, bytes, target) entries."""
        described = {}
        for version, names in objects.items():
            described[version] = []
            for item in names:
                name, size, target = (item, self.sizes.get(item, 1), self.targets.get(item, "")) \
                    if isinstance(item, str) else item
                entry = {"name": name, "bytes": size}
                if name.startswith("slots/"):
                    entry["target"] = target
                described[version].append(entry)
        self.bucket.put(f"replay-cache/builds/{build}/publish-{ms}.json",
                        json.dumps({"build": build, "objects": described, "runs": list(runs),
                                    **({"job": job} if job is not None else {}),
                                    **({"produced": produced} if produced is not None else {}),
                                    **({"produced_counts": counts} if counts is not None else {})}), created)

    def objects(self, version, *names, content=None):
        for name in names:
            body = content if content is not None else json.dumps(name)
            if name.startswith("slots/"):
                body = json.dumps(self.targets.get(name, fp(1)))
            self.bucket.put(f"replay-cache/{version}/{name}", body)
            self.sizes[name] = len(body)

    def test_fetches_what_recent_records_confirmed_at_this_version(self):
        names = [f"answers/{fp(1)}.json", f"slots/{fp(2)}.json", f"variants/{fp(3)}-{'a' * 16}.json",
                 f"messages/{'b' * 16}.json"]
        self.targets[names[1]] = fp(1)
        self.objects(VERSION, *names, f"answers/{fp(9)}.json")
        self.objects(OTHER_VERSION, f"answers/{fp(1)}.json")
        self.record("101", 1, {VERSION: names[:2], OTHER_VERSION: [f"answers/{fp(1)}.json"]},
                    runs=[{"case": "a", "rep": 1, "outcome": "pass"}])
        self.record("101", 2, {VERSION: names[2:]}, runs=[{"case": "a", "rep": 1, "outcome": "pass"},
                                                          {"case": "b", "rep": 1, "outcome": "fail"}])
        result = self.fetcher().fetch(self.dest, self.runs_out)
        self.assertEqual((result.records, result.objects, result.downloaded), (2, 4, 4))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "answers", f"{fp(1)}.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "variants", fp(3), f"{'a' * 16}.json")),
                        "variants nest again, as replay_cache.py reads them")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "answers", f"{fp(9)}.json")), "no record named it")
        with open(self.runs_out) as handle:
            self.assertEqual(len(json.load(handle)["101"]), 2, "the fullest record's runs")

    def test_an_alternate_comes_in_only_from_a_recent_record_whose_night_produced_it(self):
        import replay_cache
        old, new, slot = f"alts/{fp(1)}-{fp(7)}.json", f"alts/{fp(1)}-{fp(8)}.json", f"slots/{fp(2)}.json"
        self.targets[slot] = fp(1)
        self.objects(VERSION, old, new, slot)
        self.record("100", 1, {VERSION: [old]}, created=NOW - 40 * HOUR, produced={VERSION: {fp(1): [fp(7)]}})
        # Tonight's record names the old answer too (found in the bucket), but its runs produced only the new one.
        self.record("101", 1, {VERSION: [old, new, slot]}, produced={VERSION: {fp(1): [fp(8)]}})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual(os.listdir(os.path.join(self.dest, "alts", fp(1))), [f"{fp(8)}.json"],
                         "alternates nest again, one directory per key, as replay_cache.py reads them")
        self.assertEqual(result.skipped, {"not_produced": 1})
        self.assertEqual(result.by_kind, {"alts": 1, "slots": 1}, "the slot's key has an answer")
        self.assertEqual([name for name, _ in replay_cache.AnswerCache(self.dest).answers(fp(1))], [fp(8)])
        self.assertTrue(FC.answers_arrived(self.dest))

    def weights(self, fingerprint):
        import replay_cache
        return replay_cache.AnswerCache(self.dest).weights(fingerprint)

    def test_counts_sum_across_builds_and_a_builds_newest_record_holds_its_count(self):
        a, b = f"alts/{fp(1)}-{fp(7)}.json", f"alts/{fp(1)}-{fp(8)}.json"
        self.objects(VERSION, a, b)
        produced = {VERSION: {fp(1): [fp(7), fp(8)]}}
        # Build 100 counted again on its second call: 4 and 1, not 2+4 and 1+1.
        self.record("100", 1, {VERSION: [a, b]}, created=NOW - 10 * HOUR, produced=produced,
                    counts={VERSION: {fp(1): {fp(7): 2, fp(8): 1}}})
        self.record("100", 2, {VERSION: []}, created=NOW - 9 * HOUR, produced={VERSION: {}},
                    counts={VERSION: {fp(1): {fp(7): 4, fp(8): 1}}})
        # Build 101, another night's runs: 5 more of the first.
        self.record("101", 1, {VERSION: [a]}, produced={VERSION: {fp(1): [fp(7)]}},
                    counts={VERSION: {fp(1): {fp(7): 5}}})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual(self.weights(fp(1)), {fp(7): 9, fp(8): 1})
        self.assertEqual(result.by_kind, {"alts": 2, "weights": 1})

    def test_a_build_whose_records_give_no_count_adds_one_per_answer(self):
        a, b = f"alts/{fp(1)}-{fp(7)}.json", f"alts/{fp(1)}-{fp(8)}.json"
        self.objects(VERSION, a, b)
        self.record("100", 1, {VERSION: [a, b]}, created=NOW - 10 * HOUR, produced={VERSION: {fp(1): [fp(7), fp(8)]}})
        self.record("101", 1, {VERSION: [a]}, produced={VERSION: {fp(1): [fp(7)]}},
                    counts={VERSION: {fp(1): {fp(7): 3}}})
        self.fetcher().fetch(self.dest)
        self.assertEqual(self.weights(fp(1)), {fp(7): 1 + 3, fp(8): 1})

    def test_a_build_whose_first_record_aged_out_still_loads_and_weighs_its_counted_answer(self):
        # Build 100 listed fp(7) as produced in its first record, now past the
        # window; its newer in-window record only counts it. Build 101 gave fp(8).
        a, b = f"alts/{fp(1)}-{fp(7)}.json", f"alts/{fp(1)}-{fp(8)}.json"
        self.objects(VERSION, a, b)
        self.record("100", 1, {VERSION: [a]}, created=NOW - 40 * HOUR, produced={VERSION: {fp(1): [fp(7)]}},
                    counts={VERSION: {fp(1): {fp(7): 1}}})
        self.record("100", 2, {VERSION: []}, created=NOW - 10 * HOUR, produced={VERSION: {}},
                    counts={VERSION: {fp(1): {fp(7): 4}}})
        self.record("101", 1, {VERSION: [b]}, produced={VERSION: {fp(1): [fp(8)]}},
                    counts={VERSION: {fp(1): {fp(8): 1}}})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual(sorted(os.listdir(os.path.join(self.dest, "alts", fp(1)))), [f"{fp(7)}.json", f"{fp(8)}.json"])
        self.assertEqual(self.weights(fp(1)), {fp(7): 4, fp(8): 1})
        self.assertEqual(result.skipped, {})

    def test_an_answer_whose_newest_count_is_zero_in_every_producing_build_is_refused(self):
        # An interval pass put fp(7) up; the final pass found a disagreeing
        # producer and counts it 0. fp(8) keeps the key answered.
        a, b = f"alts/{fp(1)}-{fp(7)}.json", f"alts/{fp(1)}-{fp(8)}.json"
        self.objects(VERSION, a, b)
        self.record("100", 1, {VERSION: [a, b]}, created=NOW - 10 * HOUR, produced={VERSION: {fp(1): [fp(7), fp(8)]}},
                    counts={VERSION: {fp(1): {fp(7): 1, fp(8): 2}}})
        self.record("100", 2, {VERSION: []}, created=NOW - 9 * HOUR, produced={VERSION: {}},
                    counts={VERSION: {fp(1): {fp(7): 0, fp(8): 2}}})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual(os.listdir(os.path.join(self.dest, "alts", fp(1))), [f"{fp(8)}.json"])
        self.assertEqual(result.skipped, {"zero_count": 1})
        self.assertFalse(any(c.endswith(a) for c in self.bucket.copied), "never downloaded")

    def test_a_zero_count_in_one_build_is_outweighed_by_another_builds_positive_or_missing_count(self):
        a = f"alts/{fp(1)}-{fp(7)}.json"
        b = f"alts/{fp(1)}-{fp(8)}.json"
        self.objects(VERSION, a, b)
        self.record("100", 1, {VERSION: [a, b]}, created=NOW - 10 * HOUR, produced={VERSION: {fp(1): [fp(7), fp(8)]}},
                    counts={VERSION: {fp(1): {fp(7): 0, fp(8): 1}}})
        self.record("101", 1, {VERSION: [a]}, produced={VERSION: {fp(1): [fp(7)]}},
                    counts={VERSION: {fp(1): {fp(7): 3}}})
        self.fetcher().fetch(self.dest)
        self.assertEqual(self.weights(fp(1)), {fp(7): 3, fp(8): 1}, "build 100 adds nothing, build 101 its 3")
        self.record("102", 1, {VERSION: [a]}, produced={VERSION: {fp(1): [fp(7)]}})
        shutil.rmtree(self.dest, ignore_errors=True)
        self.fetcher().fetch(self.dest)
        self.assertEqual(self.weights(fp(1)), {fp(7): 3 + 1, fp(8): 1}, "a build with no count adds 1")

    def test_a_key_with_one_answer_gets_no_weights_file(self):
        a = f"alts/{fp(1)}-{fp(7)}.json"
        self.objects(VERSION, a)
        self.record("101", 1, {VERSION: [a]}, produced={VERSION: {fp(1): [fp(7)]}},
                    counts={VERSION: {fp(1): {fp(7): 3}}})
        result = self.fetcher().fetch(self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "weights")))
        self.assertEqual(result.by_kind, {"alts": 1})

    def weighed_keys(self, keys, extra=()):
        """`keys` keys of two counted alternates each, plus `extra` names, in one record; the alternates' and weights' cost."""
        alts = [f"alts/{fp(k)}-{fp(a)}.json" for k in range(1, keys + 1) for a in (7, 8)]
        self.objects(VERSION, *alts, *extra)
        counts = {fp(k): {fp(7): 3, fp(8): 1} for k in range(1, keys + 1)}
        self.record("101", 1, {VERSION: [*alts, *extra]}, produced={VERSION: {k: sorted(c) for k, c in counts.items()}},
                    counts={VERSION: counts})
        alts_cost = sum(FC.disk_cost(self.sizes[a]) for a in alts) + keys * FC.DISK_BLOCK_BYTES
        return alts_cost, sum(FC.weights_cost(c) for c in counts.values())

    def test_weights_are_charged_before_slots_variants_and_messages(self):
        slot = f"slots/{fp(20)}.json"
        self.targets[slot] = fp(1)
        variant = f"variants/{fp(21)}-{'a' * 16}.json"
        messages = [f"messages/{n:016x}.json" for n in range(1, 6)]
        alts_cost, weights_cost = self.weighed_keys(3, (slot, variant, *messages))
        result = self.fetcher(max_bytes=alts_cost + weights_cost).fetch(self.dest)
        self.assertEqual(result.by_kind, {"alts": 6, "weights": 3})
        self.assertEqual(result.left_out, {"slots": 1, "variants": 1, "messages": 5})
        for key in (1, 2, 3):
            self.assertEqual(self.weights(fp(key)), {fp(7): 3, fp(8): 1})

    def test_weights_are_left_out_only_when_the_alternates_leave_no_room_for_them(self):
        message = f"messages/{'b' * 16}.json"
        alts_cost, weights_cost = self.weighed_keys(1, (message,))
        for room, by_kind, left_out in ((weights_cost, {"alts": 2, "weights": 1}, {"messages": 1}),
                                        (weights_cost - 1, {"alts": 2}, {"weights": 1, "messages": 1})):
            with self.subTest(room=room):
                shutil.rmtree(self.dest, ignore_errors=True)
                result = self.fetcher(max_bytes=alts_cost + room).fetch(self.dest)
                self.assertEqual((result.by_kind, result.left_out), (by_kind, left_out))

    def test_weights_are_written_before_lower_kinds_when_an_alternate_costs_more_than_planned(self):
        # Build 100's record confirming fp(7) aged out, so fp(7) is planned at
        # no size; its real copy then takes the room the message was planned in.
        a, b, message = f"alts/{fp(1)}-{fp(7)}.json", f"alts/{fp(1)}-{fp(8)}.json", f"messages/{'b' * 16}.json"
        self.objects(VERSION, a, content="x" * (2 * FC.DISK_BLOCK_BYTES))
        self.objects(VERSION, b, message)
        self.record("100", 1, {VERSION: [a]}, created=NOW - 40 * HOUR, produced={VERSION: {fp(1): [fp(7)]}})
        self.record("100", 2, {VERSION: []}, created=NOW - 10 * HOUR, counts={VERSION: {fp(1): {fp(7): 4}}})
        self.record("101", 1, {VERSION: [b, message]}, produced={VERSION: {fp(1): [fp(8)]}})
        weights = FC.weights_cost({fp(7): 4, fp(8): 1})
        room = FC.disk_cost(self.sizes[a]) + FC.disk_cost(self.sizes[b]) + FC.DISK_BLOCK_BYTES + weights
        result = self.fetcher(max_bytes=room).fetch(self.dest)
        self.assertEqual(result.by_kind, {"alts": 2, "weights": 1})
        self.assertEqual(result.removed, {"messages": 1})
        self.assertEqual(self.weights(fp(1)), {fp(7): 4, fp(8): 1})

    def test_produced_counts_outside_their_shape_refuse_the_whole_download(self):
        message = f"messages/{'b' * 16}.json"
        for counts in ([1], {VERSION: [1]}, {VERSION: {"../x": {fp(7): 1}}}, {VERSION: {fp(1): [fp(7)]}},
                       {VERSION: {fp(1): {"../y": 1}}}, {VERSION: {fp(1): {fp(7): -1}}},
                       {VERSION: {fp(1): {fp(7): True}}}, {VERSION: {fp(1): {fp(7): "2"}}}):
            with self.subTest(counts=counts):
                shutil.rmtree(self.bucket.root, ignore_errors=True)
                self.bucket.created.clear()
                self.objects(VERSION, message)
                self.record("101", 1, {VERSION: [message]}, counts=counts)
                with self.assertRaises(FC.Refused):
                    self.fetcher().fetch(self.dest)

    def test_a_slot_whose_keys_alternates_were_all_left_out_is_removed(self):
        alt, slot = f"alts/{fp(1)}-{fp(7)}.json", f"slots/{fp(2)}.json"
        self.targets[slot] = fp(1)
        self.objects(VERSION, alt, slot)
        self.record("101", 1, {VERSION: [alt, slot]}, produced={VERSION: {}})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual((result.by_kind, result.left_out), ({}, {"slots": 1}))
        self.assertFalse(FC.answers_arrived(self.dest))

    def test_a_produced_list_outside_the_layout_refuses_the_whole_download(self):
        message = f"messages/{'b' * 16}.json"  # a name in every version of the layout
        for produced in ([fp(7)], {VERSION: [fp(7)]}, {VERSION: {"../x": [fp(7)]}}, {VERSION: {fp(1): ["../y"]}},
                         {VERSION: {fp(1): fp(7)}}):
            with self.subTest(produced=produced):
                shutil.rmtree(self.bucket.root, ignore_errors=True)
                self.bucket.created.clear()
                self.objects(VERSION, message)
                self.record("101", 1, {VERSION: [message]}, produced=produced)
                with self.assertRaises(FC.Refused):
                    self.fetcher().fetch(self.dest)

    def test_records_older_than_the_window_are_left_out(self):
        self.objects(VERSION, f"answers/{fp(1)}.json", f"answers/{fp(2)}.json")
        self.record("100", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 40 * HOUR)
        self.record("101", 1, {VERSION: [f"answers/{fp(2)}.json"]}, created=NOW - 2 * HOUR)
        result = self.fetcher(max_age_hours=30).fetch(self.dest)
        self.assertEqual(result.builds, ["101"])
        self.assertEqual(os.listdir(os.path.join(self.dest, "answers")), [f"{fp(2)}.json"])

    def test_a_key_outside_the_layout_refuses_the_whole_download(self):
        self.objects(VERSION, f"answers/{fp(1)}.json")
        self.record("101", 1, {VERSION: [f"answers/{fp(1)}.json"]})
        for key in ("replay-cache/builds/101/../../f246cb61aa625224/answers/x.json",
                    "replay-cache/builds/101//publish-2.json", "replay-cache/builds/101/notes.txt"):
            with self.subTest(key=key):
                self.bucket.created[key] = NOW
                with self.assertRaises(FC.Refused):
                    self.fetcher().fetch(self.dest)
                self.assertFalse(os.path.exists(self.dest), "nothing was written")
                del self.bucket.created[key]

    def test_a_listing_line_that_is_not_an_object_refuses(self):
        self.record("101", 1, {})
        self.bucket.extra_listing = "something unexpected\n"
        with self.assertRaises(FC.Refused):
            self.fetcher().records()

    def test_a_record_naming_something_outside_the_layout_refuses(self):
        for name in ("answers/../../etc.json", f"answers/{fp(1)}.tmp", f"variants/{fp(1)}/{'a' * 16}.json",
                     "store/requests/x.json", f"answers/{fp(1)}.json\n"):
            with self.subTest(name=name):
                self.record("101", 1, {VERSION: [(name, 1, "")]})
                with self.assertRaises(FC.Refused):
                    self.fetcher().fetch(self.dest)

    def test_a_slot_without_a_named_answer_or_an_object_without_a_size_refuses(self):
        for item in ((f"slots/{fp(1)}.json", 1, ""), (f"answers/{fp(1)}.json", -1, ""),
                     (f"answers/{fp(1)}.json", True, "")):
            with self.subTest(item=item):
                self.record("101", 1, {VERSION: [item]})
                with self.assertRaises(FC.Refused):
                    self.fetcher().fetch(self.dest)

    def test_the_bound_leaves_out_by_priority_and_never_refuses(self):
        answers = [f"answers/{fp(n)}.json" for n in (1, 2)]
        slot = f"slots/{fp(10)}.json"
        self.targets[slot] = fp(1)
        message = f"messages/{'b' * 16}.json"
        self.objects(VERSION, *answers, slot, message)
        self.record("101", 1, {VERSION: [answers[0], slot, message]})
        self.record("101", 2, {VERSION: [answers[1]]})
        # Room for two files: both answers; the slot and the message are left out.
        result = self.fetcher(max_bytes=2 * FC.disk_cost(100)).fetch(self.dest)
        self.assertEqual(result.by_kind, {"answers": 2})
        self.assertEqual(result.left_out, {"slots": 1, "messages": 1})

    def test_the_newest_records_answers_come_first(self):
        old, new = f"answers/{fp(1)}.json", f"answers/{fp(2)}.json"
        self.objects(VERSION, old, new)
        self.record("100", 1, {VERSION: [old]}, created=NOW - 10)
        self.record("101", 1, {VERSION: [new]})
        result = self.fetcher(max_bytes=FC.disk_cost(100)).fetch(self.dest)
        self.assertEqual(os.listdir(os.path.join(self.dest, "answers")), [f"{fp(2)}.json"])
        self.assertEqual(result.left_out, {"answers": 1})

    def test_a_slot_whose_answer_was_left_out_is_left_out(self):
        slot = f"slots/{fp(10)}.json"
        self.targets[slot] = fp(1)
        self.objects(VERSION, f"answers/{fp(1)}.json", slot)
        self.record("101", 1, {VERSION: [(f"answers/{fp(1)}.json", 10 ** 9, ""), slot]})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual(result.left_out, {"answers": 1, "slots": 1})
        self.assertFalse(os.path.exists(os.path.join(self.dest, "slots")))

    def test_a_slot_whose_answer_did_not_arrive_is_removed(self):
        slot = f"slots/{fp(10)}.json"
        self.targets[slot] = fp(1)
        self.objects(VERSION, f"answers/{fp(2)}.json", slot)
        # The record names the slot's answer, but the object has expired.
        self.record("101", 1, {VERSION: [(f"answers/{fp(1)}.json", 10, ""), f"answers/{fp(2)}.json", slot]})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual(result.removed, {"slots": 1})
        self.assertFalse(os.path.exists(os.path.join(self.dest, "slots", f"{fp(10)}.json")))

    def test_a_copy_larger_than_its_record_is_costed_at_its_real_size(self):
        big = "x" * (3 * FC.DISK_BLOCK_BYTES)
        self.objects(VERSION, f"answers/{fp(1)}.json", content=big)
        self.objects(VERSION, f"answers/{fp(2)}.json")
        self.record("101", 1, {VERSION: [(f"answers/{fp(1)}.json", 10, ""), (f"answers/{fp(2)}.json", 10, "")]})
        result = self.fetcher(max_bytes=2 * FC.disk_cost(10)).fetch(self.dest)
        self.assertEqual(result.removed, {"answers": 1})
        self.assertEqual(result.downloaded, 1)

    def test_disk_cost_rounds_to_blocks_and_counts_the_entry(self):
        self.assertEqual(FC.disk_cost(0), FC.FILE_OVERHEAD_BYTES)
        self.assertEqual(FC.disk_cost(1), FC.DISK_BLOCK_BYTES + FC.FILE_OVERHEAD_BYTES)
        self.assertEqual(FC.disk_cost(FC.DISK_BLOCK_BYTES + 1), 2 * FC.DISK_BLOCK_BYTES + FC.FILE_OVERHEAD_BYTES)

    def test_an_object_expired_since_its_record_is_skipped(self):
        self.objects(VERSION, f"answers/{fp(1)}.json")
        self.record("101", 1, {VERSION: [f"answers/{fp(1)}.json", (f"answers/{fp(2)}.json", 10, "")]})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual((result.objects, result.downloaded), (2, 1))

    def test_an_empty_bucket_lists_empty_and_a_failed_listing_says_so(self):
        self.bucket.ls_result = (1, b"ERROR: (gcloud.storage.ls) One or more URLs matched no objects.")
        self.assertEqual(self.fetcher().fetch(self.dest).records, 0)
        for result, cls in (((1, b"ERROR: 403 caller does not have storage.objects.list access to gs://b"),
                             "list_failed_permission_denied"),
                            ((1, b"ERROR: (gcloud.storage.ls) Your credentials are invalid. Please run reauthenticate"),
                             "list_failed_unauthenticated"),
                            (None, "list_failed")):
            with self.subTest(cls=cls):
                self.bucket.ls_result = result
                with self.assertRaises(FC.ListFailed) as raised:
                    self.fetcher().fetch(self.dest)
                self.assertEqual(raised.exception.args[0], cls)

    def test_main_names_a_failed_listing_and_reads_an_empty_one_as_empty(self):
        import contextlib
        import io
        for result, error in (((1, b"ERROR: 403 AccessDenied on gs://secret-bucket-name"), "list_failed_permission_denied"),
                              ((1, b"ERROR: One or more URLs matched no objects."), None),
                              (("ok",), None)):
            with self.subTest(error=error, result=result[0]):
                self.bucket.ls_result = result
                original = FC.default_run
                FC.default_run = self.bucket
                out = io.StringIO()
                try:
                    with contextlib.redirect_stdout(out):
                        status = FC.main(["--bucket", BUCKET, "--dest", self.dest, "--version", VERSION])
                finally:
                    FC.default_run = original
                summary = json.loads(out.getvalue())
                self.assertEqual(summary.get("error"), error)
                self.assertEqual(status, 1, "nothing to replay either way")
                self.assertNotIn("secret-bucket-name", out.getvalue())
                if error is None:
                    self.assertEqual(summary["records"], 0)

    def test_runs_come_from_records_back_to_the_lifecycle_and_objects_from_the_window(self):
        self.objects(VERSION, f"answers/{fp(1)}.json", f"answers/{fp(2)}.json")
        old_runs = [{"case": "a", "rep": 1, "outcome": "pass"}]
        self.record("100", 1, {VERSION: [f"answers/{fp(1)}.json"]}, runs=old_runs[:0], created=NOW - 48 * HOUR)
        self.record("100", 2, {VERSION: []}, runs=old_runs, created=NOW - 47 * HOUR)
        self.record("99", 1, {}, runs=old_runs, created=NOW - 40 * 24 * HOUR)
        self.record("101", 1, {VERSION: [f"answers/{fp(2)}.json"]}, runs=old_runs)
        result = self.fetcher(max_age_hours=30).fetch(self.dest, self.runs_out)
        self.assertEqual(os.listdir(os.path.join(self.dest, "answers")), [f"{fp(2)}.json"], "objects: the window only")
        self.assertEqual((result.records, result.verdict_records), (1, 1))
        with open(self.runs_out) as handle:
            runs = json.load(handle)
        self.assertEqual(sorted(runs), ["100", "101"], "build 99 is past the lifecycle")
        self.assertEqual(runs["100"], old_runs, "an older build's newest record")
        self.assertEqual([k for k in self.bucket.copied if "builds/100/" in k],
                         ["replay-cache/builds/100/publish-2.json"], "one record per older build: its newest")

    def test_the_real_runner_hands_back_a_failed_listing_for_the_fetcher_to_class(self):
        import sys
        failing = [sys.executable, "-c", "import sys; sys.stderr.write('matched no objects'); sys.exit(1)"]
        proc = FC.default_run("list_records", failing, 30)
        self.assertEqual((proc.returncode, proc.stderr), (1, b"matched no objects"))
        self.assertIsNone(FC.default_run("upload", failing, 30), "other kinds still fail as None")

    def test_only_records_of_the_lanes_mode_and_forge_give_objects(self):
        names = {key: f"answers/{fp(n)}.json" for n, key in enumerate(("github-next", "gitlab", "today", "old"), 1)}
        self.objects(VERSION, *names.values())
        runs = [{"case": "a", "rep": 1, "outcome": "pass"}]
        self.record("101", 1, {VERSION: [names["github-next"]]}, runs=runs)
        self.record("102", 1, {VERSION: [names["gitlab"]]}, runs=runs,
                    job={"name": "ci-kube-agents-eval-nightly-gitlab", "mode": "today", "forge": "gitlab"})
        self.record("103", 1, {VERSION: [names["today"]]}, runs=runs,
                    job={"name": "ci-kube-agents-eval-nightly-today", "mode": "today", "forge": "github"})
        self.record("104", 1, {VERSION: [names["old"]]}, runs=runs, job=None)
        result = self.fetcher().fetch(self.dest, self.runs_out)
        self.assertEqual(os.listdir(os.path.join(self.dest, "answers")), [f"{fp(1)}.json"])
        self.assertEqual(result.other_job_records, 3, "a record naming no job counts as another job's")
        with open(self.runs_out) as handle:
            self.assertEqual(sorted(json.load(handle)), ["101", "102", "103", "104"], "verdicts from every record")
        gitlab = self.fetcher(mode="today", forge="gitlab").fetch(os.path.join(self.dest, "gl"))
        self.assertEqual(gitlab.downloaded, 1)

    def test_another_jobs_record_with_a_name_outside_the_layout_still_refuses(self):
        self.record("102", 1, {VERSION: [("answers/../x.json", 1, "")]},
                    job={"name": "gl", "mode": "today", "forge": "gitlab"})
        with self.assertRaises(FC.Refused):
            self.fetcher().fetch(self.dest)

    def test_a_build_with_a_dot_is_a_record_like_any_other(self):
        self.objects(VERSION, f"answers/{fp(1)}.json")
        self.record("101.2", 1, {VERSION: [f"answers/{fp(1)}.json"]})
        self.assertEqual(self.fetcher().fetch(self.dest).builds, ["101.2"])


COMMIT_A, COMMIT_B, COMMIT_C = "a" * 40, "b" * 40, "c" * 40


class PrintCommitTest(FetchTest):
    """--print-commit: the commit the newest night of the lane's mode and forge ran, or nothing."""

    def job(self, commit, **overrides):
        job = dict(self.NEXT_GITHUB, commit=commit)
        job.update(overrides)
        return job

    def main(self, *extra):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        original = FC.default_run
        FC.default_run = self.bucket
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = FC.main(["--bucket", BUCKET, "--print-commit", *extra])
        finally:
            FC.default_run = original
        return status, out.getvalue(), err.getvalue()

    def test_it_prints_the_newest_nights_commit_at_any_version(self):
        # Last night (builds 100, 101, one still publishing hours later), tonight
        # (200, 201, answers at another normalizer version than this checkout's),
        # and a night past the window.
        self.record("150", 1, {}, created=NOW - 40 * HOUR, job=self.job("f" * 40))
        self.record("100", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 26 * HOUR, job=self.job(COMMIT_A))
        self.record("100", 2, {VERSION: [f"answers/{fp(2)}.json"]}, created=NOW - 20 * HOUR, job=self.job(COMMIT_A))
        self.record("101", 1, {}, created=NOW - 25 * HOUR, job=self.job(COMMIT_A))
        self.record("200", 1, {OTHER_VERSION: [f"answers/{fp(3)}.json"]}, created=NOW - 3 * HOUR, job=self.job(COMMIT_B))
        self.record("201", 1, {}, created=NOW - 1 * HOUR, job=self.job(COMMIT_B))
        night = self.fetcher(max_age_hours=30).night_commit()
        self.assertEqual((night.commit, night.builds, night.commit_builds), (COMMIT_B, ["200", "201"],
                                                                             {COMMIT_B: ["200", "201"]}))
        self.assertEqual(night.answers, {}, "one commit: no answers need counting")
        self.assertEqual(sorted(self.bucket.copied),
                         [f"replay-cache/builds/{b}/publish-{ms}.json" for b, ms in
                          (("100", 2), ("101", 1), ("200", 1), ("201", 1))],
                         "each young build's newest record, and nothing else")
        status, out, err = self.main()
        self.assertEqual((status, out), (0, COMMIT_B + "\n"))
        self.assertNotIn("commits;", err)

    def test_two_commits_in_one_night_print_the_one_with_the_most_answers_and_say_so(self):
        self.targets[f"slots/{fp(9)}.json"] = fp(1)
        self.record("200", 1, {VERSION: [f"answers/{fp(1)}.json", f"slots/{fp(9)}.json"]},
                    created=NOW - 3 * HOUR, job=self.job(COMMIT_B))
        self.record("200", 2, {VERSION: [f"answers/{fp(2)}.json"]}, created=NOW - 2 * HOUR, job=self.job(COMMIT_B))
        # The newer build, but fewer answers: one, confirmed twice.
        self.record("201", 1, {VERSION: [f"answers/{fp(5)}.json"]}, created=NOW - 1 * HOUR, job=self.job(COMMIT_C))
        self.record("201", 2, {VERSION: [f"answers/{fp(5)}.json"]}, created=NOW, job=self.job(COMMIT_C))
        night = self.fetcher().night_commit()
        self.assertEqual((night.commit, night.answers), (COMMIT_B, {COMMIT_B: 2, COMMIT_C: 1}))
        self.assertIn("replay-cache/builds/200/publish-1.json", self.bucket.copied, "contested: every record read")
        status, out, err = self.main()
        self.assertEqual((status, out), (0, COMMIT_B + "\n"))
        self.assertIn(f"name 2 commits; printing {COMMIT_B}", err)

    def test_a_rerun_from_earlier_in_the_day_is_not_tonights_night(self):
        # A Deck rerun of a nightly 11 hours before tonight's shards records under
        # its own (older) commit, with more answers; it is not part of tonight.
        self.record("190", 1, {VERSION: [f"answers/{fp(1)}.json", f"answers/{fp(2)}.json"]},
                    created=NOW - 12 * HOUR, job=self.job(COMMIT_C))
        self.record("200", 1, {VERSION: [f"answers/{fp(3)}.json"]}, created=NOW - 1 * HOUR, job=self.job(COMMIT_B))
        night = self.fetcher().night_commit()
        self.assertEqual((night.commit, night.builds), (COMMIT_B, ["200"]))

    def test_an_answer_two_builds_of_one_commit_confirmed_counts_once(self):
        # Two shards of commit B both confirm fp(1); commit C confirms two answers.
        self.record("202", 1, {VERSION: [f"answers/{fp(5)}.json", f"answers/{fp(6)}.json"]},
                    created=NOW - 3 * HOUR, job=self.job(COMMIT_C))
        self.record("200", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 2 * HOUR, job=self.job(COMMIT_B))
        self.record("201", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 1 * HOUR, job=self.job(COMMIT_B))
        night = self.fetcher().night_commit()
        self.assertEqual((night.commit, night.answers), (COMMIT_C, {COMMIT_B: 1, COMMIT_C: 2}))

    def test_a_commit_with_more_alternates_but_fewer_keys_loses(self):
        # Commit C confirmed three alternates of one key; commit B one answer
        # each to two keys. Requests covered, not objects, decide.
        a, b, c = "a" * 64, "b" * 64, "c" * 64
        self.record("202", 1, {VERSION: [f"alts/{fp(5)}-{a}.json", f"alts/{fp(5)}-{b}.json",
                                         f"alts/{fp(5)}-{c}.json"]}, created=NOW - 1 * HOUR, job=self.job(COMMIT_C))
        self.record("200", 1, {VERSION: [f"alts/{fp(1)}-{a}.json", f"answers/{fp(2)}.json"]},
                    created=NOW - 2 * HOUR, job=self.job(COMMIT_B))
        night = self.fetcher().night_commit()
        self.assertEqual((night.commit, night.answers), (COMMIT_B, {COMMIT_B: 2, COMMIT_C: 1}))

    def test_no_commit_prints_nothing_and_exits_1(self):
        for name, records in (("empty bucket", ()),
                              ("records before the commit", (("200", None), ("201", ""))),
                              ("only last night named one", (("100", COMMIT_A), ("200", None)))):
            with self.subTest(name):
                self.bucket.created.clear()
                for build, commit in records:
                    job = dict(self.NEXT_GITHUB) if commit is None else self.job(commit)
                    created = NOW - 26 * HOUR if build == "100" else NOW
                    self.record(build, 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=created, job=job)
                if not records:
                    self.bucket.ls_result = (1, b"ERROR: One or more URLs matched no objects.")
                status, out, err = self.main()
                self.bucket.ls_result = ("ok",)
                self.assertEqual((status, out), (1, ""))
                self.assertIn("stays on main", err)

    def test_other_modes_and_forges_are_ignored(self):
        self.record("200", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 3 * HOUR, job=self.job(COMMIT_B))
        self.record("300", 1, {VERSION: [f"answers/{fp(2)}.json", f"answers/{fp(3)}.json"]}, created=NOW,
                    job=self.job(COMMIT_C, name="ci-kube-agents-eval-nightly-gitlab", forge="gitlab"))
        self.record("301", 1, {VERSION: [f"answers/{fp(4)}.json"]}, created=NOW,
                    job=self.job(COMMIT_A, mode="today"))
        self.record("302", 1, {VERSION: [f"answers/{fp(5)}.json"]}, created=NOW, job=None)
        night = self.fetcher().night_commit()
        self.assertEqual((night.commit, night.builds, night.other_job_records), (COMMIT_B, ["200"], 3))
        status, out, _ = self.main()
        self.assertEqual((status, out), (0, COMMIT_B + "\n"))
        status, out, _ = self.main("--forge", "gitlab")
        self.assertEqual((status, out), (0, COMMIT_C + "\n"), "the lane's own mode and forge pick the night")

    def test_the_fetch_writes_the_newest_nights_builds_by_commit_as_print_commit_chose_them(self):
        # Last night on commit A (its answer still served), tonight's shards on
        # B (one publishing later), a rerun on C, and another forge's build.
        self.objects(VERSION, *(f"answers/{fp(i)}.json" for i in (1, 2, 3, 4)))
        self.record("100", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 26 * HOUR, job=self.job(COMMIT_A))
        self.record("200", 1, {VERSION: [f"answers/{fp(2)}.json"]}, created=NOW - 3 * HOUR, job=self.job(COMMIT_B))
        self.record("200", 2, {}, created=NOW, job=self.job(COMMIT_B))
        self.record("201", 1, {VERSION: [f"answers/{fp(3)}.json"]}, created=NOW - 2 * HOUR, job=self.job(COMMIT_B))
        self.record("202", 1, {}, created=NOW - 1 * HOUR, job=self.job(COMMIT_C))
        self.record("300", 1, {VERSION: [f"answers/{fp(4)}.json"]}, created=NOW,
                    job=self.job(COMMIT_A, name="gl", mode="today", forge="gitlab"))
        night_out = os.path.join(os.path.dirname(self.runs_out), FC.NIGHT_FILE)
        result = self.fetcher(max_age_hours=30).fetch(self.dest, self.runs_out, night_out)
        night = self.fetcher(max_age_hours=30).night_commit()
        with open(night_out) as handle:
            written = json.load(handle)
        self.assertEqual(written, {"builds": ["200", "201", "202"],
                                   "commit_builds": {COMMIT_B: ["200", "201"], COMMIT_C: ["202"]}})
        self.assertEqual((written["builds"], written["commit_builds"]), (night.builds, night.commit_builds))
        self.assertEqual(result.night_commit_builds, written["commit_builds"])

    def test_a_malformed_commit_leaves_its_build_out_of_the_night_and_the_fetch_goes_on(self):
        self.objects(VERSION, f"answers/{fp(1)}.json", f"answers/{fp(2)}.json")
        self.record("200", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 2 * HOUR, job=self.job("HEAD"))
        self.record("201", 1, {VERSION: [f"answers/{fp(2)}.json"]}, created=NOW - 1 * HOUR, job=self.job(COMMIT_B))
        night_out = os.path.join(os.path.dirname(self.runs_out), FC.NIGHT_FILE)
        result = self.fetcher().fetch(self.dest, self.runs_out, night_out)
        self.assertEqual(result.downloaded, 2)
        with open(night_out) as handle:
            self.assertEqual(json.load(handle), {"builds": ["200", "201"], "commit_builds": {COMMIT_B: ["201"]}})

    def test_records_without_a_commit_are_tolerated(self):
        # A night of records some of which predate the commit: they count for
        # nothing, the others decide. fetch() reads them as it always has.
        self.objects(VERSION, f"answers/{fp(1)}.json", f"answers/{fp(2)}.json")
        self.record("200", 1, {VERSION: [f"answers/{fp(1)}.json"]}, created=NOW - 2 * HOUR)
        self.record("201", 1, {VERSION: [f"answers/{fp(2)}.json"]}, created=NOW - 1 * HOUR, job=self.job(COMMIT_B))
        night = self.fetcher().night_commit()
        self.assertEqual((night.commit, night.uncommitted_builds), (COMMIT_B, 1))
        self.assertEqual(self.fetcher().fetch(self.dest).downloaded, 2)

    def test_a_record_out_of_shape_refuses_and_prints_nothing(self):
        for name, objects, commit in (("a commit that is not one", {}, "HEAD"),
                                      ("an uppercase commit", {}, "A" * 40),
                                      ("a name outside the layout at another version",
                                       {OTHER_VERSION: [("answers/../x.json", 1, "")]}, COMMIT_B)):
            with self.subTest(name):
                self.bucket.created.clear()
                self.record("200", 1, objects, job=self.job(commit))
                with self.assertRaises(FC.Refused):
                    self.fetcher().night_commit()
                status, out, err = self.main()
                self.assertEqual((status, out), (1, ""))
                self.assertIn('"refused"', err)

    def test_print_commit_needs_no_dest_and_fetch_still_does(self):
        import contextlib
        import io
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            FC.main(["--bucket", BUCKET])
        status, out, _ = self.main("--version", "bad")
        self.assertEqual((status, out), (1, ""), "a bad argument keeps stdout empty too")


if __name__ == "__main__":
    unittest.main()
