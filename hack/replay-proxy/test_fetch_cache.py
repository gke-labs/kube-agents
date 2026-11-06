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

    def record(self, build, ms, objects, runs=(), created=NOW):
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
                        json.dumps({"build": build, "objects": described, "runs": list(runs)}), created)

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
                     "store/requests/x.json"):
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

    def test_a_build_with_a_dot_is_a_record_like_any_other(self):
        self.objects(VERSION, f"answers/{fp(1)}.json")
        self.record("101.2", 1, {VERSION: [f"answers/{fp(1)}.json"]})
        self.assertEqual(self.fetcher().fetch(self.dest).builds, ["101.2"])


if __name__ == "__main__":
    unittest.main()
