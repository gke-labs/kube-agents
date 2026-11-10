"""The CI collector against a fake cluster and a fake bucket.

The fake `kubectl exec` runs the collector's in-pod scripts for real, with
this machine's python3, against a directory standing in for a pod's data
volume; the fake `gcloud storage cp` copies into a directory standing in for
the bucket, and can be told to fail, or to copy part of a batch and then fail.
The last class runs collect_shadow.py as a process with fake `kubectl` and
`gcloud` executables on PATH, to cover the signal handling and the exit status.
"""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest

import collect_shadow as C

HERE = os.path.dirname(os.path.abspath(__file__))
NAMESPACE = "kubeagents-system"
SELECTOR = "app=litellm,kubeagents.x-k8s.io/inference-role=replay-shadow"
CONTAINER = "replay-shadow-proxy"
BUCKET = "shadow-bucket"
DEST = C.job_prefix(BUCKET, "presubmit", "2026-11-04", "1790000000000000000")
HEX = "ab" * 32
# What a subprocess test waits for, at most.
PROCESS_WAIT_SECONDS = 30
# Longer than any fixed pause a test might take before signalling.
SLOW_START_SECONDS = 1.0


class FakeCluster:
    """Pods as directories, and a bucket as a directory, behind a Runner."""

    def __init__(self, root):
        self.root = root
        self.pods = {}  # name -> data dir
        self.bucket = os.path.join(root, "bucket")
        os.makedirs(self.bucket)
        self.upload_fail = False
        # Copy this many files of the next upload, then fail it.
        self.upload_partial = None
        self.list_pods_fail = False
        # Pods being deleted: listed with a deletionTimestamp, still running (the preStop drain).
        self.draining = set()
        # Pods whose exec fails as a pod deleted mid-copy does.
        self.gone = set()
        self.calls = []

    def add_pod(self, name):
        path = os.path.join(self.root, "pods", name)
        for sub in ("logs", "store/requests", "store/blobs"):
            os.makedirs(os.path.join(path, sub), exist_ok=True)
        self.pods[name] = path
        return path

    def object_path(self, dest_url):
        assert dest_url.startswith("gs://"), dest_url
        return os.path.join(self.bucket, dest_url[len("gs://"):])

    def objects(self):
        out = {}
        for dirpath, _, files in os.walk(self.bucket):
            for name in files:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as handle:
                    out[os.path.relpath(full, self.bucket)] = handle.read()
        return out

    def __call__(self, cmd, input=None, remaining=None, env=None, stdin_path=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if stdin_path is not None:
            with open(stdin_path, "rb") as handle:
                input = handle.read()
        if cmd[0] == "kubectl":
            return self.kubectl(cmd, input)
        if cmd[0] == "gcloud":
            assert env and env.get("CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED") == "False"
            return self.gcloud(cmd, input)
        raise AssertionError(cmd)

    def kubectl(self, cmd, input):
        if "get" in cmd:
            if self.list_pods_fail:
                return subprocess.CompletedProcess(cmd, 1, b"", b"Unable to connect to the server: dial tcp: i/o timeout")
            items = [{"metadata": {"name": name, **({"deletionTimestamp": "2026-11-10T06:00:00Z"}
                                                    if name in self.draining else {})},
                      "status": {"phase": "Running", "containerStatuses": [
                          {"name": CONTAINER, "state": {"running": {}}}]}} for name in sorted(self.pods)]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"items": items}).encode(), b"")
        assert "exec" in cmd, cmd
        pod = cmd[cmd.index("exec") + 1 + (1 if cmd[cmd.index("exec") + 1] == "-i" else 0)]
        assert cmd[cmd.index("-c") + 1] == CONTAINER
        if pod in self.gone:
            return subprocess.CompletedProcess(cmd, 1, b"", f'Error from server (NotFound): pods "{pod}" not found'.encode())
        script_args = cmd[cmd.index("--") + 1:]
        assert script_args[:2] == ["python3", "-c"], script_args
        script, args = script_args[2], script_args[3:]
        args[0] = self.pods[pod]  # the data dir, in this "pod"
        proc = subprocess.run([sys.executable, "-c", script, *args], input=input, capture_output=True)
        return subprocess.CompletedProcess(cmd, proc.returncode, proc.stdout, proc.stderr)

    def gcloud(self, cmd, input):
        assert cmd[:3] == ["gcloud", "storage", "cp"], cmd
        assert "--no-clobber" in cmd
        if self.upload_fail:
            return subprocess.CompletedProcess(cmd, 1, b"", f"ERROR: 403 caller does not have storage.objects.create access to gs://{BUCKET}/x/{HEX}.json".encode())
        if "-I" in cmd:
            sources = [p for p in input.decode().split("\n") if p]
            dest_dir = self.object_path(cmd[-1])
            copied = 0
            for src in sources:
                if self.upload_partial is not None and copied >= self.upload_partial:
                    self.upload_partial = None
                    return subprocess.CompletedProcess(cmd, 1, b"", b"ERROR: connection reset by peer")
                target = os.path.join(dest_dir, os.path.basename(src))
                os.makedirs(dest_dir, exist_ok=True)
                if not os.path.exists(target):
                    shutil.copyfile(src, target)
                copied += 1
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        src, dest = cmd[-2], cmd[-1]
        target = self.object_path(dest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not os.path.exists(target):
            shutil.copyfile(src, target)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


class CollectorTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cluster = FakeCluster(self.root)
        self.pod = self.cluster.add_pod("proxy-a")
        self.staging = os.path.join(self.root, "staging")
        os.makedirs(self.staging)
        self.out = io.StringIO()
        self.now = 0.0
        self.collector = C.Collector(namespace=NAMESPACE, selector=SELECTOR, container=CONTAINER, dest=DEST,
                                     staging=self.staging, runner=self.cluster, out=self.out,
                                     clock=lambda: self.now)

    def put(self, rel, data, pod=None):
        path = os.path.join(pod or self.pod, rel)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def size(self, rel, pod=None):
        return os.path.getsize(os.path.join(pod or self.pod, rel))

    def key(self, rel):
        return os.path.relpath(self.cluster.object_path(f"{DEST}/{rel}"), self.cluster.bucket)


class OnlyCompleteFilesTest(CollectorTestCase):
    def test_closed_files_are_uploaded_and_emptied_and_the_rest_left(self):
        self.put("logs/p-1-000001.jsonl", b'{"a": 1}\n')
        self.put("logs/p-1-000002.jsonl.open", b'{"b": 2}\n{"c"')
        self.put(f"store/blobs/{HEX}.json", b'"blob"')
        self.put(f"store/blobs/{HEX}.json.1.2.tmp", b'"half')
        self.put("store/requests/r1.json", b'{"messages": []}')
        self.put("store/requests/empty.json", b"")
        stats = self.collector.collect_pass()
        self.assertEqual(stats["uploaded"], 3)
        self.assertEqual(self.cluster.objects(), {
            self.key("logs/p-1-000001.jsonl"): b'{"a": 1}\n',
            self.key(f"store/blobs/{HEX}.json"): b'"blob"',
            self.key("store/requests/r1.json"): b'{"messages": []}',
        })
        for rel in ("logs/p-1-000001.jsonl", f"store/blobs/{HEX}.json", "store/requests/r1.json"):
            self.assertEqual(self.size(rel), 0, rel)
        # Still being written: untouched.
        self.assertEqual(self.size("logs/p-1-000002.jsonl.open"), len(b'{"b": 2}\n{"c"'))
        self.assertEqual(self.size(f"store/blobs/{HEX}.json.1.2.tmp"), len(b'"half'))

    def test_an_emptied_file_is_neither_listed_nor_uploaded_again(self):
        self.put("store/requests/r1.json", b"{}")
        self.collector.collect_pass()
        uploads_before = [c for c in self.cluster.calls if c[0] == "gcloud"]
        stats = self.collector.collect_pass()
        self.assertEqual(stats["listed"], 0)
        self.assertEqual([c for c in self.cluster.calls if c[0] == "gcloud"], uploads_before)

    def test_a_blob_another_pod_uploaded_is_emptied_without_a_second_copy(self):
        self.put(f"store/blobs/{HEX}.json", b'"same"')
        self.collector.collect_pass()
        other = self.cluster.add_pod("proxy-b")
        self.put(f"store/blobs/{HEX}.json", b'"same"', pod=other)
        gcloud_before = len([c for c in self.cluster.calls if c[0] == "gcloud"])
        self.collector.collect_pass()
        self.assertEqual(len([c for c in self.cluster.calls if c[0] == "gcloud"]), gcloud_before)
        self.assertEqual(self.size(f"store/blobs/{HEX}.json", pod=other), 0)

    def test_a_draining_pod_is_still_listed_and_emptied(self):
        other = self.cluster.add_pod("proxy-b")
        self.cluster.draining.add("proxy-b")
        self.put("logs/p-2-000001.jsonl", b"last\n", pod=other)
        self.assertEqual(self.collector.pods(), [C.ProxyPod("proxy-a", False), C.ProxyPod("proxy-b", True)])
        stats = self.collector.collect_pass()
        self.assertEqual(stats["pods"], 2)
        self.assertEqual(self.cluster.objects(), {self.key("logs/p-2-000001.jsonl"): b"last\n"})
        self.assertEqual(self.size("logs/p-2-000001.jsonl", pod=other), 0)


class TruncateOnlyAfterUploadTest(CollectorTestCase):
    def test_a_failed_upload_empties_nothing_and_is_retried(self):
        self.put("logs/p-1-000001.jsonl", b"line\n")
        self.cluster.upload_fail = True
        stats = self.collector.collect_pass()
        self.assertEqual(stats["uploaded"], 0)
        self.assertEqual(self.size("logs/p-1-000001.jsonl"), 5)
        self.assertEqual(self.cluster.objects(), {})
        self.cluster.upload_fail = False
        self.collector.collect_pass()
        self.assertEqual(self.cluster.objects(), {self.key("logs/p-1-000001.jsonl"): b"line\n"})
        self.assertEqual(self.size("logs/p-1-000001.jsonl"), 0)

    def test_a_partial_upload_empties_nothing_and_the_retry_completes_it(self):
        names = [f"store/requests/r{i}.json" for i in range(3)]
        for name in names:
            self.put(name, name.encode())
        self.cluster.upload_partial = 1  # one object lands, then the copy fails
        self.collector.collect_pass()
        self.assertEqual(len(self.cluster.objects()), 1)
        self.assertEqual([self.size(n) for n in names], [len(n) for n in names])
        self.collector.collect_pass()
        self.assertEqual(sorted(self.cluster.objects()), sorted(self.key(n) for n in names))
        self.assertEqual([self.size(n) for n in names], [0, 0, 0])

    def test_a_fetch_that_fails_uploads_and_empties_nothing(self):
        self.put("store/requests/r1.json", b"{}")
        real = self.cluster.kubectl

        def broken_tar(cmd, input):
            if "exec" in cmd and "-i" in cmd and C.TAR_SCRIPT in cmd:
                return subprocess.CompletedProcess(cmd, 1, b"", b"error dialing backend")
            return real(cmd, input)

        self.cluster.kubectl = broken_tar
        self.collector.collect_pass()
        self.assertEqual(self.cluster.objects(), {})
        self.assertEqual(self.size("store/requests/r1.json"), 2)
        self.assertEqual(self.collector.totals.errors.get("fetch_exec_failed"), 1)

    def test_a_tar_stream_cut_short_uploads_only_whole_members(self):
        self.put("store/requests/a.json", b"A" * 4000)
        self.put("store/requests/b.json", b"B" * 4000)
        real = self.cluster.kubectl

        def cut(cmd, input):
            proc = real(cmd, input)
            if C.TAR_SCRIPT in cmd:
                # Through a's data and into b's: b's member is short.
                return subprocess.CompletedProcess(cmd, 0, proc.stdout[:512 + 4096 + 512 + 1000], b"")
            return proc

        self.cluster.kubectl = cut
        self.collector.collect_pass()
        self.assertEqual(self.cluster.objects(), {self.key("store/requests/a.json"): b"A" * 4000})
        self.assertEqual(self.size("store/requests/a.json"), 0)
        self.assertEqual(self.size("store/requests/b.json"), 4000)

    def test_a_file_whose_size_changed_since_upload_is_not_emptied(self):
        rel = "store/requests/r1.json"
        self.put(rel, b"old")
        real_upload = self.collector.upload

        def upload_then_change(root, names):
            done = real_upload(root, names)
            self.put(rel, b"rewritten-longer")
            return done

        self.collector.upload = upload_then_change
        self.collector.collect_pass()
        self.assertEqual(self.size(rel), len(b"rewritten-longer"))

    def test_empty_script_refuses_open_segments_temporary_files_and_paths_outside(self):
        self.put("logs/p-1-000001.jsonl.open", b"x\n")
        self.put("store/blobs/b.json.1.1.tmp", b"x")
        outside = os.path.join(self.root, "outside.json")
        with open(outside, "wb") as handle:
            handle.write(b"x")
        entries = [b"2\tlogs/p-1-000001.jsonl.open", b"1\tstore/blobs/b.json.1.1.tmp", b"1\t../../outside.json"]
        proc = subprocess.run([sys.executable, "-c", C.EMPTY_SCRIPT, self.pod], input=b"\0".join(entries),
                              capture_output=True, check=True)
        self.assertEqual(proc.stdout.strip(), b"0")
        self.assertEqual(self.size("logs/p-1-000001.jsonl.open"), 2)
        self.assertEqual(os.path.getsize(outside), 1)


class UnsafeNamesTest(CollectorTestCase):
    """The pod's listing and tar stream choose no path on this machine."""

    def test_names_outside_the_layout_are_dropped_and_counted(self):
        self.put("store/requests/ok.json", b"{}")
        real = self.cluster.kubectl
        forged = [["../../escape.json", 2], ["logs/../../x.jsonl", 2], ["store/other/y.json", 2],
                  ["store/requests/ok.json", 2], ["logs/.hidden.jsonl", 2]]

        def forge(cmd, input):
            if C.LIST_SCRIPT in cmd:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(forged).encode(), b"")
            return real(cmd, input)

        self.cluster.kubectl = forge
        self.collector.collect_pass()
        self.assertEqual(self.cluster.objects(), {self.key("store/requests/ok.json"): b"{}"})
        self.assertEqual(self.collector.totals.errors.get("list_files_unsafe_name"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.json")))

    def test_a_tar_member_with_an_unsafe_name_is_not_written(self):
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name in ("../evil.json", "store/requests/ok.json"):
                info = tarfile.TarInfo(name)
                info.size = 2
                tar.addfile(info, io.BytesIO(b"{}"))
        root = os.path.join(self.staging, "batch")
        os.makedirs(root)
        self.collector.runner = lambda cmd, input=None, remaining=None, env=None: subprocess.CompletedProcess(
            cmd, 0, buf.getvalue(), b"")
        written = self.collector.fetch("proxy-a", [("../evil.json", 2), ("store/requests/ok.json", 2)], root)
        self.assertEqual(written, {"store/requests/ok.json": 2})
        self.assertFalse(os.path.exists(os.path.join(self.staging, "evil.json")))


class FinalPassTest(CollectorTestCase):
    def test_final_pass_takes_open_segments_cut_to_whole_lines_and_never_empties_them(self):
        rel = "logs/p-1-000002.jsonl.open"
        self.put(rel, b'{"a": 1}\n{"b": 2}\n{"c": ')
        self.collector.deadline = self.now + 60
        stats = self.collector.collect_pass(final=True)
        self.assertEqual(stats["uploaded"], 1)
        self.assertEqual(self.cluster.objects(), {self.key("logs/p-1-000002.jsonl"): b'{"a": 1}\n{"b": 2}\n'})
        self.assertEqual(self.size(rel), len(b'{"a": 1}\n{"b": 2}\n{"c": '))

    def test_an_open_segment_without_a_whole_line_uploads_nothing(self):
        self.put("logs/p-1-000002.jsonl.open", b'{"partial')
        self.collector.deadline = self.now + 60
        self.collector.collect_pass(final=True)
        self.assertEqual(self.cluster.objects(), {})

    def test_ordinary_pass_never_takes_open_segments(self):
        self.put("logs/p-1-000002.jsonl.open", b"whole\n")
        self.collector.collect_pass()
        self.assertEqual(self.cluster.objects(), {})


class DeadlineTest(CollectorTestCase):
    def test_past_the_deadline_nothing_runs(self):
        self.put("store/requests/r1.json", b"{}")
        self.collector.deadline = self.now - 1
        self.collector.collect_pass(final=True)
        self.assertEqual(self.cluster.calls, [])

    def test_once_the_last_pass_is_due_an_ordinary_pass_stops(self):
        self.cluster.add_pod("proxy-b")
        self.collector.deadline = self.now + 60
        stats = self.collector.collect_pass(final=False)
        self.assertEqual(stats["pods"], 0)

    def test_a_command_gets_no_more_than_the_deadline_leaves(self):
        seen = []

        def runner(cmd, input=None, remaining=None, env=None):
            seen.append(remaining())
            return subprocess.CompletedProcess(cmd, 0, b'{"items": []}', b"")

        self.collector.runner = runner
        self.collector.deadline = self.now + 5
        self.collector.pods()
        self.assertEqual(seen, [5])

    def test_a_timed_out_command_is_counted_not_raised(self):
        def runner(cmd, input=None, remaining=None, env=None):
            raise subprocess.TimeoutExpired(cmd, 0)

        self.collector.runner = runner
        self.assertEqual(self.collector.pods(), [])
        self.assertEqual(self.collector.totals.errors, {"list_pods_timeout": 1})

    def test_default_runner_kills_a_command_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            C.default_runner([sys.executable, "-c", "import time; time.sleep(30)"],
                             remaining=lambda: 1.5 - (time.monotonic() - started))
        self.assertLess(time.monotonic() - started, 5)

    def test_default_runner_feeds_input_and_returns_output(self):
        proc = C.default_runner([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read()[::-1])"],
                                input=b"abc")
        self.assertEqual((proc.returncode, proc.stdout), (0, b"cba"))


class RunLoopTest(CollectorTestCase):
    def test_a_pass_that_raises_is_counted_and_the_loop_goes_on(self):
        passes = []

        def flaky(final=False):
            passes.append(final)
            if len(passes) == 1:
                raise RuntimeError("boom")
            return {"pods": 0, "listed": 0, "new": 0, "uploaded": 0}

        self.collector.collect_pass = flaky
        stops = iter([False, False, True])
        C.run_loop(self.collector, interval=0, final_deadline=60, parent_pid=0,
                   stop=lambda: next(stops, True), sleep=lambda s: None)
        self.assertEqual(passes, [False, False, True])
        self.assertEqual(self.collector.totals.errors["pass_crashed"], 1)

    def test_the_manifest_is_retried_until_it_uploads(self):
        attempts = []

        def manifest():
            attempts.append(1)
            return len(attempts) >= 2

        stops = iter([False, False, False, True])
        C.run_loop(self.collector, interval=0, final_deadline=60, parent_pid=0,
                   stop=lambda: next(stops, True), sleep=lambda s: None, manifest=manifest)
        self.assertEqual(len(attempts), 2)

    def test_a_gone_parent_ends_the_loop_with_a_final_pass(self):
        finals = []
        self.collector.collect_pass = lambda final=False: finals.append(final) or {}
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        C.run_loop(self.collector, interval=0, final_deadline=60, parent_pid=child.pid,
                   stop=lambda: False, sleep=lambda s: None)
        self.assertEqual(finals, [True])
        self.assertEqual(self.collector.deadline, self.now + 60)


class OutputIsPublicSafeTest(CollectorTestCase):
    def test_no_file_name_content_or_command_error_text_is_printed(self):
        content = b'{"secret-ish": "the request text"}'
        self.put(f"store/blobs/{HEX}.json", content)
        self.put("logs/proxy-a-1700000000000-000001.jsonl", b'{"fingerprint": "' + HEX.encode() + b'"}\n')
        self.cluster.upload_fail = True
        self.collector.report(self.collector.collect_pass(), final=False)
        self.cluster.upload_fail = False
        self.collector.report(self.collector.collect_pass(), final=False)
        self.cluster.list_pods_fail = True
        self.collector.report(self.collector.collect_pass(), final=False)
        printed = self.out.getvalue()
        for record in printed.splitlines():
            json.loads(record)
        self.assertIn("upload_permission_denied", printed)
        self.assertIn("list_pods_network", printed)
        for forbidden in (HEX, "secret-ish", "request text", "proxy-a-1700000000000", BUCKET, ".jsonl", ".json\"",
                          "storage.objects.create", "dial tcp"):
            self.assertNotIn(forbidden, printed)

    def test_long_hex_is_masked_in_anything_printed(self):
        self.collector.emit({"event": "x", "detail": "id " + "0123456789abcdef" * 2 + " short abcdef"})
        self.assertIn("id <hex> short abcdef", self.out.getvalue())

    def test_error_events_are_bounded_per_kind_but_still_counted(self):
        for _ in range(C.MAX_ERROR_EVENTS_PER_KIND + 3):
            self.collector.fail("upload_other")
        self.assertEqual(self.out.getvalue().count('"upload_other"'), C.MAX_ERROR_EVENTS_PER_KIND)
        self.assertEqual(self.collector.totals.errors["upload_other"], C.MAX_ERROR_EVENTS_PER_KIND + 3)

    def test_error_class_names_a_kind_and_falls_back_to_other(self):
        self.assertEqual(C.error_class(b"HTTPError 429: Too Many Requests"), "rate_limited")
        self.assertEqual(C.error_class(b"something odd"), C.ERROR_CLASS_OTHER)
        self.assertEqual(C.error_class(None), C.ERROR_CLASS_OTHER)


FAKE_KUBECTL = """\
#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
pod_dir = os.environ["FAKE_POD_DIR"]
if "get" in args and os.environ.get("FAKE_NO_PODS"):
    print(json.dumps({{"items": []}}))
    sys.exit(0)
if "get" in args:
    print(json.dumps({{"items": [{{"metadata": {{"name": "proxy-a"}}, "status": {{"phase": "Running",
        "containerStatuses": [{{"name": "{container}", "state": {{"running": {{}}}}}}]}}}}]}}))
    sys.exit(0)
rest = args[args.index("--") + 1:]
script, script_args = rest[2], rest[3:]
script_args[0] = pod_dir
sys.exit(subprocess.run([sys.executable, "-c", script, *script_args], stdin=sys.stdin).returncode)
"""

FAKE_GCLOUD = """\
#!{python}
import os, shutil, sys, time
args = sys.argv[1:]
bucket = os.environ["FAKE_BUCKET_DIR"]
if os.environ.get("FAKE_GCLOUD_HANG"):
    open(os.environ["FAKE_GCLOUD_HANG"], "w").close()  # says it is hanging now
    time.sleep(600)
def target(url):
    return os.path.join(bucket, url[len("gs://"):])
if "-I" in args:
    dest = target(args[-1])
    os.makedirs(dest, exist_ok=True)
    for src in sys.stdin.read().split("\\n"):
        if src and not os.path.exists(os.path.join(dest, os.path.basename(src))):
            shutil.copyfile(src, os.path.join(dest, os.path.basename(src)))
else:
    dst = target(args[-1])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(args[-2], dst)
"""


POD_NAME = "proxy-a"
CACHE = C.DEFAULT_CACHE_SUBDIR


def fp(n):
    return f"{n:064x}"


class RecordModeTest(CollectorTestCase):
    """Record mode copies saved answers to the kept copy, empties them in the pod, never uploads them."""

    def setUp(self):
        super().setUp()
        self.keep = os.path.join(self.root, "keep")
        self.publishes = []
        self.collector.mode = C.MODE_RECORD
        self.collector.keep_dir = self.keep
        self.collector.publisher = self
        self.collector.publish_interval = 300
        self.collector.publish_in_background = False

    def publish(self, final=False):
        self.publishes.append((self.now, final))

    def cache(self, rel, data):
        path = os.path.join(self.pod, CACHE, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def kept(self, rel):
        return os.path.join(self.keep, "pods", POD_NAME, "cache", rel)

    def test_answers_are_kept_on_this_machine_and_emptied_in_the_pod(self):
        answer = self.cache(f"answers/{fp(1)}.json", b'{"answer": 1}')
        variant = self.cache(f"variants/{fp(2)}/{'a' * 16}.json", b"")
        self.collector.collect_pass()
        with open(self.kept(f"answers/{fp(1)}.json"), "rb") as handle:
            self.assertEqual(handle.read(), b'{"answer": 1}')
        self.assertTrue(os.path.isfile(self.kept(f"variants/{fp(2)}/{'a' * 16}.json")), "an empty variant is kept")
        self.assertEqual(os.path.getsize(answer), 0, "emptied, so its name still says the key was written")
        self.assertTrue(os.path.exists(variant))
        self.assertFalse(any(CACHE in name or "answers" in name for name in self.cluster.objects()),
                         "the cache never goes to the shadow bucket")
        fetches = sum(1 for c in self.cluster.calls if "exec" in c and C.TAR_SCRIPT in c)
        self.collector.collect_pass()
        self.assertEqual(sum(1 for c in self.cluster.calls if "exec" in c and C.TAR_SCRIPT in c), fetches,
                         "a kept file is not fetched again")

    def test_lines_and_stored_requests_are_kept_beside_their_upload(self):
        self.put("logs/seg-1.jsonl", b'{"line": 1}\n')
        self.put(f"store/requests/{HEX}.json", b"{}")
        self.collector.collect_pass()
        self.assertTrue(os.path.isfile(os.path.join(self.keep, "pods", POD_NAME, "logs", "seg-1.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(self.keep, "store", "requests", f"{HEX}.json")))
        self.assertIn(self.key("logs/seg-1.jsonl"), self.cluster.objects())

    def test_a_name_outside_the_cache_layout_is_neither_copied_nor_emptied(self):
        odd = self.cache("answers/not-a-key.json", b"x")
        self.collector.collect_pass()
        self.assertEqual(os.path.getsize(odd), 1)
        self.assertFalse(os.path.exists(os.path.join(self.keep, "pods", POD_NAME, "cache", "answers", "not-a-key.json")))

    def test_publishes_on_its_interval_and_always_in_the_last_pass(self):
        self.collector.collect_pass()
        self.now = 100.0
        self.collector.collect_pass()
        self.now = 301.0
        self.collector.collect_pass()
        self.now = 302.0
        self.collector.deadline = 1000.0
        self.collector.collect_pass(final=True)
        self.assertEqual(self.publishes, [(0.0, False), (301.0, False), (302.0, True)])

    def test_an_ordinary_publish_runs_beside_collection_and_the_last_waits_for_it(self):
        import threading
        release, started, finished, order = threading.Event(), threading.Event(), threading.Event(), []

        class Slow:
            def publish(inner, final=False):
                order.append(("publish", final))
                if not final:
                    started.set()
                    release.wait(10)
                    finished.set()

        self.collector.publisher = Slow()
        self.collector.publish_in_background = True
        answer = self.cache(f"answers/{fp(1)}.json", b"{}")
        self.collector.collect_pass()
        self.assertFalse(finished.is_set(), "the pass returned while the publish still ran")
        self.assertTrue(started.wait(5))
        self.assertEqual(os.path.getsize(answer), 0, "the pod was emptied while the publish still ran")
        self.collector.deadline = 1000.0
        threading.Timer(0.2, release.set).start()
        self.collector.collect_pass(final=True)
        self.assertEqual(order, [("publish", False), ("publish", True)])

    def test_a_publish_still_running_at_the_deadline_skips_the_last_one_and_never_hangs(self):
        import threading
        release, started, order = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)

        class Stuck:
            def publish(inner, final=False):
                order.append(final)
                if not final:
                    started.set()
                    release.wait(30)

        self.collector.publisher = Stuck()
        self.collector.publish_in_background = True
        self.collector.collect_pass()
        self.assertTrue(started.wait(5))
        self.collector.deadline = self.now + 0.3
        began = time.monotonic()
        self.collector.collect_pass(final=True)
        self.assertLess(time.monotonic() - began, 10, "the last pass waits no longer than the deadline")
        self.assertEqual(order, [False], "no last publish beside one still running")
        self.assertEqual(self.collector.totals.errors.get("publish_overran"), 1)
        self.assertIn("publish_overran", self.out.getvalue())

    def test_keep_writes_never_leave_a_partial_file_under_its_name(self):
        answer = self.cache(f"answers/{fp(1)}.json", b'{"answer": 1}')
        self.put("logs/seg-1.jsonl", b'{"line": 1}\n')
        written = []
        original = C.shutil.copyfile
        C.shutil.copyfile = lambda src, dst, *a, **k: written.append(dst) or original(src, dst, *a, **k)
        self.addCleanup(setattr, C.shutil, "copyfile", original)
        self.collector.collect_pass()
        kept = [d for d in written if d.startswith(self.keep)]
        self.assertTrue(kept)
        self.assertTrue(all(d.endswith(".tmp") for d in kept), "a reader never sees a kept file half written")
        cache = os.path.join(self.keep, "pods", POD_NAME, "cache", "answers")
        self.assertEqual(os.listdir(cache), [f"{fp(1)}.json"], "no temporary file left beside it")

    def test_shadow_mode_copies_no_answers(self):
        self.collector.mode = C.MODE_SHADOW
        answer = self.cache(f"answers/{fp(1)}.json", b"{}")
        self.collector.collect_pass()
        self.assertEqual(os.path.getsize(answer), 2)
        self.assertFalse(os.path.exists(self.keep + "/pods"))


class ReplayModeTest(CollectorTestCase):
    """Replay mode seeds every pod that has not got the cache, and the last pass writes the lane's report."""

    def setUp(self):
        super().setUp()
        self.seed_dir = os.path.join(self.root, "seed")
        for rel, data in ((f"answers/{fp(1)}.json", b'{"a": 1}'), (f"variants/{fp(2)}/{'b' * 16}.json", b""),
                          ("answers/stray.txt", b"no")):
            path = os.path.join(self.seed_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        self.collector.mode = C.MODE_REPLAY
        self.collector.keep_dir = os.path.join(self.root, "keep")
        self.collector.seed_dir = self.seed_dir

    def seeds(self):
        return sum(1 for c in self.cluster.calls if "exec" in c and C.SEED_SCRIPT in c)

    def test_an_unseeded_pod_is_seeded_once_with_only_the_cache_layout(self):
        self.collector.collect_pass()
        base = os.path.join(self.pod, CACHE)
        with open(os.path.join(base, "answers", f"{fp(1)}.json"), "rb") as handle:
            self.assertEqual(handle.read(), b'{"a": 1}')
        self.assertTrue(os.path.isfile(os.path.join(base, "variants", fp(2), f"{'b' * 16}.json")))
        self.assertFalse(os.path.exists(os.path.join(base, "answers", "stray.txt")))
        with open(os.path.join(base, C.CACHE_SEED_MARKER)) as handle:
            self.assertEqual(handle.read(), "2")
        self.collector.collect_pass()
        self.assertEqual(self.seeds(), 1, "a seeded pod is not seeded again")

    def test_a_pod_that_replaced_one_is_seeded_too(self):
        self.collector.collect_pass()
        other = self.cluster.add_pod("proxy-b")
        self.collector.collect_pass()
        self.assertTrue(os.path.isfile(os.path.join(other, CACHE, C.CACHE_SEED_MARKER)))
        self.assertEqual(self.seeds(), 2)

    def test_seed_script_writes_no_member_outside_the_layout(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            for name in ("../escape.json", f"answers/{fp(1)}.json", f"answers/../../{fp(1)}.json"):
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        proc = subprocess.run([sys.executable, "-c", C.SEED_SCRIPT, self.pod, CACHE, C.CACHE_SEED_MARKER,
                               C.CACHE_REL_RE.pattern], input=stream.getvalue(), capture_output=True)
        self.assertEqual(proc.stdout.strip(), b"1")
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "pods", f"{fp(1)}.json")))

    def test_seed_all_reports_how_many_pods_took_it(self):
        self.cluster.add_pod("proxy-b")
        self.assertEqual(self.collector.seed_all(), (2, 2))

    def test_seed_once_skips_a_draining_pod_and_counts_only_live_ones(self):
        # The old pod of a rollout: listed while it drains, gone before its copy ends.
        old = self.cluster.add_pod("proxy-old")
        self.cluster.draining.add("proxy-old")
        self.cluster.gone.add("proxy-old")
        self.assertEqual(self.collector.seed_all(), (1, 1))
        self.assertFalse(os.path.exists(os.path.join(old, CACHE, C.CACHE_SEED_MARKER)))
        self.assertTrue(os.path.isfile(os.path.join(self.pod, CACHE, C.CACHE_SEED_MARKER)))
        self.assertEqual(self.collector.totals.errors, {})
        self.assertIn('"event": "seed_skipped_draining", "pods": 1', self.out.getvalue())

    def test_a_pod_deleted_during_its_seed_leaves_both_counts(self):
        # Live when listed, evicted while its copy ran: gone, not a refusal.
        self.cluster.add_pod("proxy-b")
        self.cluster.gone.add("proxy-b")
        listing = self.collector.pods
        calls = []

        def pods():
            calls.append(None)
            found = listing()
            return [pod for pod in found if pod.name != "proxy-b"] if len(calls) > 1 else found

        self.collector.pods = pods
        self.assertEqual(self.collector.seed_all(), (1, 1))
        self.assertIn('"event": "seed_skipped_vanished", "pods": 1', self.out.getvalue())

    def test_a_live_pod_that_refused_its_seed_still_counts(self):
        self.cluster.add_pod("proxy-b")
        self.cluster.gone.add("proxy-b")
        self.assertEqual(self.collector.seed_all(), (2, 1))
        self.assertNotIn("seed_skipped_vanished", self.out.getvalue())

    def test_a_pass_seeds_no_draining_pod_but_collects_its_lines(self):
        old = self.cluster.add_pod("proxy-old")
        self.cluster.draining.add("proxy-old")
        self.put("logs/p-2-000001.jsonl", b"last\n", pod=old)
        self.collector.collect_pass()
        self.assertFalse(os.path.exists(os.path.join(old, CACHE, C.CACHE_SEED_MARKER)))
        self.assertTrue(os.path.isfile(os.path.join(self.pod, CACHE, C.CACHE_SEED_MARKER)))
        self.assertEqual(self.seeds(), 1)
        self.assertIn(self.key("logs/p-2-000001.jsonl"), self.cluster.objects())

    def test_the_last_pass_uploads_the_lane_report_under_report(self):
        def report(out_dir):
            with open(os.path.join(out_dir, "verdicts.json"), "w") as handle:
                handle.write("{}")
            return "case-a: lane 3/3"

        self.collector.lane_report = report
        C.run_loop(self.collector, 0, 10, 0, stop=lambda: True, sleep=lambda s: None)
        self.assertIn(self.key("report/verdicts.json"), self.cluster.objects())
        self.assertIn("case-a: lane 3/3", self.out.getvalue())


class ProcessTest(unittest.TestCase):
    """collect_shadow.py as CI runs it: a process, signalled, always exiting 0."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin = os.path.join(self.root, "bin")
        self.pod = os.path.join(self.root, "pod")
        self.bucket = os.path.join(self.root, "bucket")
        for path in (self.bin, os.path.join(self.pod, "logs"), self.bucket):
            os.makedirs(path)
        for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as handle:
                handle.write(body.format(python=sys.executable, container=CONTAINER))
            os.chmod(path, 0o755)
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_POD_DIR=self.pod, FAKE_BUCKET_DIR=self.bucket)

    def start(self, *extra, env=None):
        cmd = [sys.executable, os.path.join(HERE, "collect_shadow.py"), "--namespace", NAMESPACE,
               "--selector", SELECTOR, "--container", CONTAINER, "--bucket", BUCKET, "--kind", "nightly",
               "--build", "123", "--interval", "0.2", *extra]
        return subprocess.Popen(cmd, env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait_for(self, predicate):
        deadline = time.monotonic() + PROCESS_WAIT_SECONDS
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def bucket_files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.bucket)
                      for d, _, files in os.walk(self.bucket) for f in files)

    def test_sigterm_makes_a_final_pass_with_open_segments_and_exits_0(self):
        with open(os.path.join(self.pod, "logs", "p-1-000001.jsonl.open"), "wb") as handle:
            handle.write(b"whole\npart")
        proc = self.start("--final-deadline", "20")
        self.wait_for(lambda: any(f.endswith(".json") and "/job-" in f for f in self.bucket_files()))
        self.assertFalse(any(f.endswith(".jsonl") for f in self.bucket_files()))
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(proc.returncode, 0, err)
        segment = [f for f in self.bucket_files() if f.endswith(".jsonl")]
        self.assertEqual(len(segment), 1)
        self.assertTrue(segment[0].startswith(f"{BUCKET}/replay-shadow/nightly/"), segment)
        with open(os.path.join(self.bucket, segment[0]), "rb") as handle:
            self.assertEqual(handle.read(), b"whole\n")
        events = [json.loads(l)["event"] for l in out.decode().splitlines()]
        self.assertEqual(events[0], "start")
        self.assertEqual(events[-1], "stop")

    def test_a_nightly_untrusted_run_uploads_under_its_own_folder(self):
        with open(os.path.join(self.pod, "logs", "p-1-000001.jsonl"), "wb") as handle:
            handle.write(b"line\n")
        proc = self.start("--kind", "nightly-untrusted", "--final-deadline", "20")
        self.wait_for(lambda: any(f.endswith(".jsonl") for f in self.bucket_files()))
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(proc.returncode, 0, err)
        files = self.bucket_files()
        self.assertTrue(files)
        for name in files:
            self.assertTrue(name.startswith(f"{BUCKET}/replay-shadow/nightly-untrusted/"), name)
        [manifest] = [f for f in files if "/job-" in f]
        with open(os.path.join(self.bucket, manifest)) as handle:
            self.assertEqual(json.load(handle)["kind"], "nightly-untrusted")

    def test_sigterm_cuts_a_hung_upload_at_the_final_deadline_and_exits_0(self):
        with open(os.path.join(self.pod, "logs", "p-1-000001.jsonl"), "wb") as handle:
            handle.write(b"line\n")
        hanging = os.path.join(self.root, "gcloud-hanging")
        proc = self.start("--final-deadline", "2", env=dict(self.env, FAKE_GCLOUD_HANG=hanging))
        # Into the hung manifest upload, however long the collector took to start.
        self.wait_for(lambda: os.path.exists(hanging))
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(proc.returncode, 0, err)
        self.assertLess(time.monotonic() - started, 10)
        printed = out.decode()
        self.assertTrue("manifest_timeout" in printed or "manifest_deadline" in printed, printed)
        # Nothing reached the bucket, so nothing in the pod was emptied.
        self.assertEqual(os.path.getsize(os.path.join(self.pod, "logs", "p-1-000001.jsonl")), 5)

    def test_parent_gone_ends_the_collector_with_exit_0(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
        proc = self.start("--parent-pid", str(parent.pid), "--final-deadline", "5")
        parent.wait()
        out, err = proc.communicate(timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(proc.returncode, 0, err)

    def test_seed_once_fails_on_bad_arguments_and_without_pods(self):
        bad = subprocess.run([sys.executable, os.path.join(HERE, "collect_shadow.py"), "--seed-once"],
                             env=self.env, capture_output=True, timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(bad.returncode, 1)
        seed = os.path.join(self.root, "seed")
        os.makedirs(seed)
        none = subprocess.run([sys.executable, os.path.join(HERE, "collect_shadow.py"), "--namespace", NAMESPACE,
                               "--selector", SELECTOR, "--container", CONTAINER, "--bucket", BUCKET, "--kind",
                               "replay-lane", "--build", "123", "--mode", "replay", "--keep-dir", self.root,
                               "--seed-dir", seed, "--seed-once"],
                              env=dict(self.env, FAKE_NO_PODS="1"), capture_output=True, timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(none.returncode, 1, none.stdout)

    def test_a_build_or_bucket_that_could_make_a_dot_segment_is_refused(self):
        base = ["--namespace", NAMESPACE, "--selector", SELECTOR, "--container", CONTAINER, "--kind", "presubmit"]
        for bucket, build in ((BUCKET, ".."), (BUCKET, "."), (BUCKET, "../../nightly/2026-11-05/1"), (BUCKET, "a/b"),
                              (BUCKET, ""), ("b/../x", "1"), ("gs://b", "1")):
            with self.subTest(bucket=bucket, build=build):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    C.parse_args([*base, "--bucket", bucket, "--build", build])
        args = C.parse_args([*base, "--bucket", BUCKET, "--build", "1790000000000000000"])
        self.assertNotRegex(C.job_prefix(args.bucket, args.kind, "2026-11-05", args.build), r"/\.\.?(/|$)")

    def test_bad_arguments_still_exit_0(self):
        proc = subprocess.run([sys.executable, os.path.join(HERE, "collect_shadow.py"), "--kind", "weekly"],
                              capture_output=True, timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(proc.returncode, 0)

    def test_missing_tools_still_exit_0(self):
        # A slow interpreter start (here a sitecustomize that sleeps) would
        # let a TERM sent after a fixed pause land before the handler; the
        # `start` line is printed only once the handler is in.
        site = os.path.join(self.root, "site")
        os.makedirs(site)
        with open(os.path.join(site, "sitecustomize.py"), "w") as handle:
            handle.write(f"import time\ntime.sleep({SLOW_START_SECONDS})\n")
        env = dict(self.env, PATH=os.path.join(self.root, "empty"), PYTHONPATH=site)
        proc = self.start("--final-deadline", "1", env=env)
        first = json.loads(proc.stdout.readline())
        self.assertEqual(first["event"], "start")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=PROCESS_WAIT_SECONDS)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FileNotFoundError", out.decode())


class OpenSegmentClosedMidPassTest(unittest.TestCase):
    """The last pass lists an open segment the proxy then closes before the copy."""

    def test_the_copy_reads_it_under_its_closed_name(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "logs"))
        opened = os.path.join(root, "logs", "p-1-000001.jsonl.open")
        with open(opened, "w") as handle:
            handle.write('{"a": 1}\n')
        listed = json.loads(subprocess.run([sys.executable, "-c", C.LIST_SCRIPT, root, "1", json.dumps(C.SOURCES)],
                                           capture_output=True, check=True).stdout)
        os.replace(opened, opened[: -len(".open")])
        out = subprocess.run([sys.executable, "-c", C.TAR_SCRIPT, root], input=b"\0".join(r.encode() for r, _ in listed),
                             capture_output=True, check=True).stdout
        members = [(m.name, m.size) for m in tarfile.open(fileobj=io.BytesIO(out), mode="r|")]
        self.assertEqual(members, [("logs/p-1-000001.jsonl.open", 9)])


if __name__ == "__main__":
    unittest.main()
