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

    def put(self, key, content, created=NOW):
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
        self.created[key] = created

    def __call__(self, kind, cmd, timeout, input=None, env=None):
        prefix = f"gs://{BUCKET}/"
        if cmd[:4] == ["gcloud", "storage", "ls", "-l"]:
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

    def fetcher(self, **overrides):
        settings = dict(bucket=BUCKET, version=VERSION, run=self.bucket, clock=lambda: NOW)
        settings.update(overrides)
        return FC.Fetcher(**settings)

    def record(self, build, ms, objects, runs=(), created=NOW):
        self.bucket.put(f"replay-cache/builds/{build}/publish-{ms}.json",
                        json.dumps({"build": build, "objects": objects, "runs": list(runs)}), created)

    def objects(self, version, *names):
        for name in names:
            self.bucket.put(f"replay-cache/{version}/{name}", json.dumps(name))

    def test_fetches_what_recent_records_confirmed_at_this_version(self):
        names = [f"answers/{fp(1)}.json", f"slots/{fp(2)}.json", f"variants/{fp(3)}-{'a' * 16}.json",
                 f"messages/{'b' * 16}.json"]
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
                self.record("101", 1, {VERSION: [name]})
                with self.assertRaises(FC.Refused):
                    self.fetcher().fetch(self.dest)

    def test_the_byte_bound_refuses_a_cache_too_large_for_the_pod(self):
        self.objects(VERSION, f"answers/{fp(1)}.json", f"answers/{fp(2)}.json")
        self.record("101", 1, {VERSION: [f"answers/{fp(1)}.json", f"answers/{fp(2)}.json"]})
        with self.assertRaises(FC.Refused):
            self.fetcher(max_bytes=80).fetch(self.dest)

    def test_an_object_expired_since_its_record_is_skipped(self):
        self.objects(VERSION, f"answers/{fp(1)}.json")
        self.record("101", 1, {VERSION: [f"answers/{fp(1)}.json", f"answers/{fp(2)}.json"]})
        result = self.fetcher().fetch(self.dest)
        self.assertEqual((result.objects, result.downloaded), (2, 1))


if __name__ == "__main__":
    unittest.main()
