"""Publishing a nightly's recorded answers: only from runs that passed, each name written once."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import publish_cache as P
from test_run_attribution import World, created, front, worker

VERSION = "f246cb61aa625224"
BUCKET = "replay-cache-bucket"
POD = "inference-replay-shadow-abc12-xyz34"
# A second proxy pod, whose name sorts before POD's.
EARLIER_POD = "inference-replay-shadow-abc12-aaaaa"


def fp(n):
    return f"{n:064x}"


class FakeBucket:
    """`gcloud storage cp --no-clobber` into a directory: an existing object is never replaced."""

    def __init__(self, root):
        self.root = root
        self.fail = False
        # Command kinds (the Publisher's: publish, publish_record) that fail.
        self.fail_kinds = set()
        self.calls = []

    def path(self, url):
        assert url.startswith("gs://"), url
        return os.path.join(self.root, url[len("gs://"):])

    def __call__(self, kind, cmd, timeout, input=None, env=None):
        self.calls.append((kind, list(cmd)))
        assert cmd[:3] == ["gcloud", "storage", "cp"] and "--no-clobber" in cmd, cmd
        if self.fail or kind in self.fail_kinds:
            return None
        if "-I" in cmd:
            dest = self.path(cmd[-1])
            os.makedirs(dest, exist_ok=True)
            for src in filter(None, input.decode().split("\n")):
                target = os.path.join(dest, os.path.basename(src))
                if not os.path.exists(target):
                    shutil.copyfile(src, target)
        else:
            target = self.path(cmd[-1])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.exists(target):
                shutil.copyfile(cmd[-2], target)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def objects(self):
        out = {}
        for dirpath, _, names in os.walk(self.root):
            for name in names:
                full = os.path.join(dirpath, name)
                with open(full) as handle:
                    out[os.path.relpath(full, os.path.join(self.root, BUCKET))] = handle.read()
        return out


class PublishTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.world = World(root)
        self.keep = root
        self.pod_dir = os.path.join(root, "pods", POD)
        os.makedirs(os.path.join(self.pod_dir, "logs"))
        self.bucket = FakeBucket(os.path.join(root, "bucket"))
        self.publisher = P.Publisher(keep_dir=self.keep, verdicts_dir=self.world.verdicts, bucket=BUCKET,
                                     build="1790000000000000000", run=self.bucket, clock=self.clock)
        self.now = 1_800_000_000.0

    def clock(self):
        self.now += 1
        return self.now

    def cache_file(self, rel, content, pod_dir=None):
        path = os.path.join(pod_dir or self.pod_dir, "cache", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)

    def answer(self, n, version=VERSION, pod_dir=None):
        self.cache_file(f"answers/{fp(n)}.json", json.dumps(
            {"fingerprint": fp(n), "answer": {"content": f"answer {n}"}, "normalizer_version": version}), pod_dir)

    def write_logs(self, pod_dir=None, lines=None):
        pod_dir = pod_dir or self.pod_dir
        os.makedirs(os.path.join(pod_dir, "logs"), exist_ok=True)
        lines = [{"event": "replay_shadow_start", "normalizer_version": VERSION}] + (
            self.world.lines if lines is None else lines)
        with open(os.path.join(pod_dir, "logs", "seg-1.jsonl"), "w") as handle:
            handle.write("\n".join(json.dumps(l) for l in lines) + "\n")

    def test_only_answers_from_runs_that_passed_go_up(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.run("case-a", 2, "fail", ["t_22222222"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.world.request(worker("t_22222222"), fp(2), recorded=True)
        self.answer(1)
        self.answer(2)
        self.answer(3)  # saved, but no line of this pod recorded it
        self.write_logs()
        report = self.publisher.publish(final=True)
        objects = self.bucket.objects()
        self.assertIn(f"replay-cache/{VERSION}/answers/{fp(1)}.json", objects)
        self.assertNotIn(f"replay-cache/{VERSION}/answers/{fp(2)}.json", objects)
        self.assertNotIn(f"replay-cache/{VERSION}/answers/{fp(3)}.json", objects)
        self.assertEqual(report["decisions"], {"pass": 1, "fail": 1, "no_recording_line": 1})

    def test_a_slot_goes_up_only_with_its_answer_and_variants_go_up_flat(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.run("case-a", 2, "fail", ["t_22222222"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.world.request(worker("t_22222222"), fp(2), recorded=True)
        self.answer(1)
        self.answer(2)
        self.cache_file(f"slots/{fp(10)}.json", json.dumps(fp(1)))
        self.cache_file(f"slots/{fp(11)}.json", json.dumps(fp(2)))
        self.cache_file(f"variants/{fp(20)}/{'a' * 16}.json", "")
        self.cache_file(f"messages/{'b' * 16}.json", json.dumps("text"))
        self.write_logs()
        self.publisher.publish(final=True)
        objects = self.bucket.objects()
        prefix = f"replay-cache/{VERSION}"
        self.assertIn(f"{prefix}/slots/{fp(10)}.json", objects)
        self.assertNotIn(f"{prefix}/slots/{fp(11)}.json", objects, "its answer came from a failing run")
        self.assertIn(f"{prefix}/variants/{fp(20)}-{'a' * 16}.json", objects)
        self.assertIn(f"{prefix}/messages/{'b' * 16}.json", objects)

    def two_pods_one_slot(self, earlier_outcome):
        """Each pod saved its own answer for one slot key, and its own pointer to it; EARLIER_POD sorts first."""
        earlier = os.path.join(self.keep, "pods", EARLIER_POD)
        self.world.run("case-a", 1, earlier_outcome, ["t_11111111"])
        mine = self.world.request(worker("t_11111111"), fp(1), recorded=True)
        theirs = self.world.request(worker("t_22222222"), fp(2), recorded=True)
        self.answer(1, pod_dir=earlier)
        self.cache_file(f"slots/{fp(10)}.json", json.dumps(fp(1)), pod_dir=earlier)
        self.write_logs(pod_dir=earlier, lines=[mine])
        self.answer(2)
        self.cache_file(f"slots/{fp(10)}.json", json.dumps(fp(2)))
        self.write_logs(lines=[theirs])

    def test_a_slot_whose_answer_did_not_go_up_does_not_hide_another_pods_for_the_same_key(self):
        self.two_pods_one_slot("fail")
        self.world.run("case-a", 2, "pass", ["t_22222222"])
        self.publisher.publish(final=True)
        slot = f"replay-cache/{VERSION}/slots/{fp(10)}.json"
        self.assertEqual(json.loads(self.bucket.objects().get(slot, "null")), fp(2),
                         "the passing pod's pointer goes up, though the failing pod's sorts first")
        objects = {o["name"]: o for o in self._records()[-1]["objects"][VERSION]}
        self.assertEqual(objects[f"slots/{fp(10)}.json"]["target"], fp(2))

    def test_a_slot_waits_for_its_answer_across_calls(self):
        self.two_pods_one_slot("fail")
        self.publisher.publish()
        self.assertEqual(self.bucket.objects().get(f"replay-cache/{VERSION}/slots/{fp(10)}.json"), None,
                         "the passing pod's answer is not graded yet")
        self.world.run("case-a", 2, "pass", ["t_22222222"])
        self.publisher.publish()
        self.assertEqual(json.loads(self.bucket.objects()[f"replay-cache/{VERSION}/slots/{fp(10)}.json"]), fp(2))

    def test_a_slot_kept_after_its_answer_went_up_follows_it(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        self.assertEqual(self.publisher.publish()["uploaded"], {"answers": 1})
        self.cache_file(f"slots/{fp(10)}.json", json.dumps(fp(1)))
        self.assertEqual(self.publisher.publish(final=True)["uploaded"], {"slots": 1},
                         "its answer is in the bucket from an earlier call")

    def test_the_first_night_to_record_a_key_keeps_it(self):
        earlier = self.bucket.path(f"gs://{BUCKET}/replay-cache/{VERSION}/answers/{fp(1)}.json")
        os.makedirs(os.path.dirname(earlier))
        with open(earlier, "w") as handle:
            handle.write("an earlier night's answer")
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        self.publisher.publish(final=True)
        with open(earlier) as handle:
            self.assertEqual(handle.read(), "an earlier night's answer")
        record = self._records()[-1]
        self.assertEqual([o["name"] for o in record["objects"][VERSION]], [f"answers/{fp(1)}.json"],
                         "the record still names it, so the lane fetches the earlier night's copy")

    def test_an_answer_waits_until_its_case_is_graded_and_goes_up_once(self):
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        self.assertEqual(self.publisher.publish()["decisions"], {"pending": 1})
        self.assertEqual(self.bucket.objects(), {})
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.assertEqual(self.publisher.publish()["uploaded"], {"answers": 1})
        self.assertEqual(self.publisher.publish()["uploaded"], {}, "never uploaded twice")

    def test_an_opening_goes_up_once_a_passing_run_went_on_from_its_answer(self):
        self.world.run("case-a", 1, "pass", ["t_00000001"])
        self.world.request(front(), fp(1), recorded=True, answer="A")
        self.answer(1)
        self.write_logs()
        self.assertEqual(self.publisher.publish()["decisions"], {"pending": 1})
        self.world.request(front({"role": "assistant", "content": "A"}, *created("t_00000001")), fp(2),
                           recorded=True, prior=(f"ctx-{fp(1)}", "A"))
        self.answer(2)
        self.write_logs()
        self.assertEqual(self.publisher.publish()["uploaded"], {"answers": 2})

    def test_records_name_the_recording_job(self):
        self.publisher.job = P.job_identity("ci-kube-agents-eval-nightly", "1", "")
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        self.publisher.publish(final=True)
        self.assertEqual(self._records()[-1]["job"],
                         {"name": "ci-kube-agents-eval-nightly", "mode": "next", "forge": "github"})
        self.assertEqual(P.job_identity("gl", "", "gitlab"), {"name": "gl", "mode": "today", "forge": "gitlab"})

    def test_records_give_each_objects_size_and_each_slots_target(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.cache_file(f"slots/{fp(10)}.json", json.dumps(fp(1)))
        self.write_logs()
        self.publisher.publish(final=True)
        objects = {o["name"]: o for o in self._records()[-1]["objects"][VERSION]}
        self.assertEqual(objects[f"slots/{fp(10)}.json"]["target"], fp(1))
        self.assertEqual(objects[f"answers/{fp(1)}.json"]["bytes"],
                         os.path.getsize(os.path.join(self.pod_dir, "cache", "answers", f"{fp(1)}.json")))

    def test_a_kept_segment_is_read_again_only_once_it_changes(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        reads = []
        original = P.RA.read_lines
        P.RA.read_lines = lambda paths: reads.append(list(paths)) or original(paths)
        self.addCleanup(setattr, P.RA, "read_lines", original)
        self.publisher.publish()
        first = len(reads)
        self.publisher.publish()
        self.assertEqual(len(reads), first, "nothing changed, nothing is parsed again")

    def test_an_answer_from_another_normalizer_version_is_dropped(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1, version="0000000000000000")
        self.write_logs()
        self.assertEqual(self.publisher.publish(final=True)["decisions"], {"version_mismatch": 1})

    def test_a_failed_upload_is_retried_and_its_names_recorded_then(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        self.bucket.fail = True
        self.assertEqual(self.publisher.publish()["uploaded"], {"answers": 0})
        self.bucket.fail = False
        self.publisher.publish(final=True)
        self.assertIn(f"answers/{fp(1)}.json", [o["name"] for o in self._records()[-1]["objects"][VERSION]])
        runs = self._records()[-1]["runs"]
        self.assertEqual(runs, [{"case": "case-a", "rep": 1, "outcome": "pass"}])

    def test_names_whose_record_failed_go_in_the_next_record(self):
        self.world.run("case-a", 1, "pass", ["t_11111111"])
        self.world.run("case-a", 2, "pass", ["t_22222222"])
        self.world.request(worker("t_11111111"), fp(1), recorded=True)
        self.answer(1)
        self.write_logs()
        self.bucket.fail_kinds = {"publish_record"}
        self.assertEqual(self.publisher.publish()["uploaded"], {"answers": 1})
        self.assertEqual(self._records(), [], "the record did not go up")
        self.bucket.fail_kinds = set()
        self.world.request(worker("t_22222222"), fp(2), recorded=True)
        self.answer(2)
        self.write_logs()
        self.publisher.publish()
        [record] = self._records()
        self.assertEqual(sorted(o["name"] for o in record["objects"][VERSION]),
                         [f"answers/{fp(1)}.json", f"answers/{fp(2)}.json"],
                         "the earlier call's names ride in the next record, or no lane would ever fetch them")

    def test_every_cache_layout_shape_maps_to_a_name_the_lane_accepts(self):
        import fetch_cache
        import replay_cache
        shapes = [(sub, f"{fp(1)}.json") for sub in ("answers", "slots", "messages")]
        shapes.append(("variants", f"{fp(2)}/{'a' * 16}.json"))
        fetcher = fetch_cache.Fetcher(bucket=BUCKET, version=VERSION, run=self.bucket)
        for sub, rel in shapes:
            with self.subTest(sub=sub):
                self.assertTrue(replay_cache.CACHE_REL_RE.match(f"{sub}/{rel}"))
                name = P.object_name(sub, rel)
                self.assertTrue(P.OBJECT_NAME_RE.match(name), name)
                item = {"name": name, "bytes": 1}
                if sub == "slots":
                    item["target"] = fp(1)
                self.assertEqual(fetcher._confirmed({"objects": {VERSION: [item]}})[0][0], name)

    def test_object_names_refuse_anything_outside_the_layout(self):
        self.assertEqual(P.object_name("variants", f"{fp(1)}/{'a' * 16}.json"), f"variants/{fp(1)}-{'a' * 16}.json")
        for sub, rel in (("answers", "../x.json"), ("answers", f"{fp(1)}.tmp"), ("slots", f"a/{fp(1)}.json"),
                         ("variants", f"{fp(1)}.json"), ("other", f"{fp(1)}.json"), ("answers", "ABC.json")):
            with self.subTest(sub=sub, rel=rel):
                self.assertIsNone(P.object_name(sub, rel))

    def test_names_with_a_trailing_newline_are_refused(self):
        self.assertIsNone(P.object_name("answers", f"{fp(1)}.json\n"))
        for pattern, good in ((P.SAFE_HEX, fp(1)), (P.OBJECT_NAME_RE, f"variants/{fp(1)}-{'a' * 16}.json"),
                              (P.NORMALIZER_VERSION_RE, VERSION), (P.POD_NAME_RE, POD),
                              (P.BUILD_RE, "1790000000000000000"), (P.BUCKET_RE, BUCKET)):
            with self.subTest(pattern=pattern.pattern):
                self.assertTrue(pattern.match(good))
                self.assertIsNone(pattern.match(good + "\n"))
        self.assertFalse(P.valid_bucket_and_build(BUCKET + "\n", "1790000000000000000"))
        self.assertFalse(P.valid_bucket_and_build(BUCKET, "1790000000000000000\n"))

    def _records(self):
        out = []
        for name, content in sorted(self.bucket.objects().items()):
            if name.startswith("replay-cache/builds/1790000000000000000/publish-"):
                out.append(json.loads(content))
        return out


if __name__ == "__main__":
    unittest.main()
