"""Unit tests for cron_jobs_sync.py.

Run: cd agents/chat/scripts && python3 -m unittest test_cron_jobs_sync.py

Stdlib only, deliberately: this module is imported by the container entrypoint
before anything else starts, so it must not depend on the agent's Python
environment being installed.

The property under test throughout is that an image roll changes a job's
*definition* and never its *runtime state*. The bug being fixed was silent — a
new job simply never appeared — so several tests assert on what did NOT happen.
"""

import json
import os
import pathlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import cron_jobs_sync


def job(job_id, **kw):
    base = {
        "id": job_id,
        "name": job_id,
        "schedule": {"kind": "interval", "minutes": 5},
        "prompt": "",
        "enabled": True,
    }
    base.update(kw)
    return base


class ReconcileTests(unittest.TestCase):
    def test_new_image_job_is_installed(self):
        merged, ledger, summary = cron_jobs_sync.reconcile(
            [job("existing"), job("brand-new")], [job("existing")], set()
        )
        self.assertEqual(summary["added"], ["brand-new"])
        self.assertEqual([j["id"] for j in merged], ["existing", "brand-new"])
        self.assertIn("brand-new", ledger)

    def test_runtime_state_survives_an_image_roll(self):
        """Scheduler state is the deployment's, and the image ships none of it.

        Resetting last_run would make every job look due at once; dropping
        deliver/origin would unbind the chat the onboarding plugin wrote.
        """
        runtime = [
            job(
                "j",
                last_run="2026-08-04T10:00:00Z",
                deliver="origin",
                origin={"chat_id": "abc"},
                prompt="old",
            )
        ]
        image = [job("j", prompt="new", deliver="local")]
        merged, _, summary = cron_jobs_sync.reconcile(image, runtime, set())

        self.assertEqual(summary["refreshed"], ["j"])
        self.assertEqual(merged[0]["prompt"], "new")
        self.assertEqual(merged[0]["last_run"], "2026-08-04T10:00:00Z")
        self.assertEqual(merged[0]["deliver"], "origin")
        self.assertEqual(merged[0]["origin"], {"chat_id": "abc"})

    def test_a_state_field_this_script_never_heard_of_survives(self):
        """The point of the per-key rule, and what an allowlist cannot do.

        `last_status` and `last_error` are read back out of the job dict by
        `tools/cronjob_tools.py`; `last_run_at` is a one-shot's already-ran
        guard and `next_run_at` is what the catch-up window reads. None of the
        four is `last_run`, which is the only scheduler field the earlier
        allowlist named — so all four were being erased on every pod start.
        A future Hermes field must survive without an edit here.
        """
        merged, _, _ = cron_jobs_sync.reconcile(
            [job("j")],
            [
                job(
                    "j",
                    last_run_at="2026-08-04T10:00:00Z",
                    next_run_at="2026-08-05T06:20:00Z",
                    last_status="ok",
                    last_error=None,
                    a_field_invented_next_year=17,
                )
            ],
            set(),
        )
        self.assertEqual(merged[0]["last_run_at"], "2026-08-04T10:00:00Z")
        self.assertEqual(merged[0]["next_run_at"], "2026-08-05T06:20:00Z")
        self.assertEqual(merged[0]["last_status"], "ok")
        self.assertIn("last_error", merged[0])
        self.assertEqual(merged[0]["a_field_invented_next_year"], 17)

    def test_the_image_decides_whether_a_job_is_enabled(self):
        """`enabled: false` in the image is the fleet-wide kill switch.

        The mirror of `profile_scaffold`'s
        `test_the_image_decides_whether_a_watchdog_is_enabled`, and deliberately
        so: two rosters obeying opposite merge rules is a trap for whoever edits
        either. It is also the only kill switch there is — nothing here prunes,
        so deleting the entry would leave the job running.
        """
        merged, _, _ = cron_jobs_sync.reconcile(
            [job("j", enabled=False)], [job("j", enabled=True)], set()
        )
        self.assertFalse(merged[0]["enabled"])

    def test_a_hand_disabled_job_is_switched_back_on(self):
        """The documented cost of the rule above, pinned so it cannot drift.

        A live-pod edit is not the supported way to retire a watchdog; the image
        is the declaration of record. `concepts/autonomous-watchdogs.md` states
        the cost under "Disabling a watchdog" — that a PVC edit holds only until
        the next restart, because this reconcile is what that restart runs. Note
        which way that dependency points: the page documents the behaviour this
        assertion fixes, so changing the assertion means editing the page, not
        the reverse.
        """
        merged, _, _ = cron_jobs_sync.reconcile(
            [job("j", enabled=True)], [job("j", enabled=False)], set()
        )
        self.assertTrue(merged[0]["enabled"])

    def test_a_deliver_the_runtime_lacks_is_taken_from_the_image(self):
        """`deliver` is runtime-*wins*, not runtime-only.

        A volume whose copy of a job predates the key has nothing to protect, so
        adding `deliver: "all"` to an existing entry has to reach it. Stripping
        it would be the silent-alert-drop this roster's history is about.
        """
        merged, _, _ = cron_jobs_sync.reconcile(
            [job("j", deliver="all")], [job("j")], set()
        )
        self.assertEqual(merged[0]["deliver"], "all")

    def test_deliberately_removed_job_is_not_resurrected(self):
        """bootstrap_delivery._cleanup removes its jobs on purpose."""
        merged, _, summary = cron_jobs_sync.reconcile(
            [job("bootstrap-inventory-scan")], [], {"bootstrap-inventory-scan"}
        )
        self.assertEqual(merged, [])
        self.assertEqual(summary["skipped_removed"], ["bootstrap-inventory-scan"])
        self.assertEqual(summary["added"], [])

    def test_operator_added_job_is_left_alone(self):
        """A job the image never declared is not ours to delete."""
        merged, _, summary = cron_jobs_sync.reconcile(
            [job("from-image")], [job("hand-written")], set()
        )
        self.assertEqual([j["id"] for j in merged], ["from-image", "hand-written"])
        self.assertEqual(summary["refreshed"], [])

    def test_identical_input_reports_no_change(self):
        """An unchanged image must not rewrite the file — that would churn the PVC
        on every restart and make a real change impossible to spot in a diff."""
        jobs = [job("j", last_run="2026-08-04T10:00:00Z")]
        merged, _, summary = cron_jobs_sync.reconcile([job("j")], jobs, {"j"})
        self.assertEqual(merged, jobs)
        self.assertEqual(summary["added"], [])
        self.assertEqual(summary["refreshed"], [])

    def test_image_job_without_id_is_skipped(self):
        merged, _, summary = cron_jobs_sync.reconcile([{"name": "nameless"}], [], set())
        self.assertEqual(merged, [])
        self.assertEqual(summary["added"], [])

    def test_the_shipped_roster_carries_no_scheduler_state(self):
        """Where a stray `last_run` in the image file has to be caught.

        Under the per-key rule the image wins every key it ships, so a scheduler
        field committed into the roster by accident would be pushed onto every
        cluster on every boot and pin the job's history there. The rule is not
        the place to defend against that — an exclusion list on the image side
        would be one more thing to keep in step with Hermes, for a mistake that
        is visible in a diff. This is: it fails in CI, at the source, naming the
        job and the key.
        """
        roster = Path(__file__).resolve().parents[1] / "defaults" / "cron" / "jobs.json"
        jobs = json.loads(roster.read_text(encoding="utf-8"))["jobs"]
        state = {
            "last_run",
            "last_run_at",
            "next_run_at",
            "last_status",
            "last_error",
            "origin",
        }
        for entry in jobs:
            with self.subTest(job=entry.get("id")):
                self.assertEqual(state & set(entry), set())


class SyncFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.image = self.tmp / "image.json"
        self.runtime = self.tmp / "runtime.json"
        self.ledger = self.tmp / "ledger.json"

    def write(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_wrapper_shape_is_preserved(self):
        """The repo's files use {"jobs": [...]}; rewriting them as a bare list
        would be read by nothing."""
        self.write(self.image, {"jobs": [job("a"), job("b")]})
        self.write(self.runtime, {"jobs": [job("a")]})

        self.assertEqual(cron_jobs_sync.sync(self.image, self.runtime, self.ledger), 0)
        out = self.read(self.runtime)
        self.assertIsInstance(out, dict)
        self.assertEqual([j["id"] for j in out["jobs"]], ["a", "b"])

    def test_bare_list_shape_is_preserved(self):
        self.write(self.image, [job("a")])
        self.write(self.runtime, [])
        self.assertEqual(cron_jobs_sync.sync(self.image, self.runtime, self.ledger), 0)
        self.assertIsInstance(self.read(self.runtime), list)

    def test_ledger_makes_removal_stick_across_two_boots(self):
        """The end-to-end property: install once, remove at runtime, stay removed."""
        self.write(self.image, {"jobs": [job("scan"), job("keep")]})
        self.write(self.runtime, {"jobs": [job("keep")]})

        cron_jobs_sync.sync(self.image, self.runtime, self.ledger)
        self.assertEqual([j["id"] for j in self.read(self.runtime)["jobs"]], ["scan", "keep"])

        # Runtime removes it, as _cleanup does.
        self.write(self.runtime, {"jobs": [job("keep")]})
        cron_jobs_sync.sync(self.image, self.runtime, self.ledger)
        self.assertEqual([j["id"] for j in self.read(self.runtime)["jobs"]], ["keep"])

    def test_assume_retired_covers_the_empty_ledger_first_run(self):
        """A deployment that finished onboarding before this script shipped has no
        ledger, so the retired jobs would otherwise look new and come back."""
        self.write(self.image, {"jobs": [job("bootstrap-inventory-scan"), job("new-job")]})
        self.write(self.runtime, {"jobs": []})

        cron_jobs_sync.sync(
            self.image,
            self.runtime,
            self.ledger,
            assume_retired={"bootstrap-inventory-scan"},
        )
        ids = [j["id"] for j in self.read(self.runtime)["jobs"]]
        self.assertEqual(ids, ["new-job"])
        # ...and the seed persists, so later boots need no flag.
        self.assertIn("bootstrap-inventory-scan", self.read(self.ledger))

    def test_missing_runtime_file_is_left_to_the_defaults_copy(self):
        self.write(self.image, {"jobs": [job("a")]})
        self.assertEqual(cron_jobs_sync.sync(self.image, self.runtime, self.ledger), 0)
        self.assertFalse(self.runtime.exists())

    def test_missing_image_file_is_a_no_op(self):
        self.write(self.runtime, {"jobs": [job("a")]})
        self.assertEqual(cron_jobs_sync.sync(self.image, self.runtime, self.ledger), 0)
        self.assertEqual([j["id"] for j in self.read(self.runtime)["jobs"]], ["a"])

    def test_unparseable_runtime_file_is_reported_and_left_alone(self):
        """Never overwrite a file we could not read: the operator's jobs are in it."""
        self.write(self.image, {"jobs": [job("a")]})
        self.runtime.write_text("{not json", encoding="utf-8")
        self.assertEqual(cron_jobs_sync.sync(self.image, self.runtime, self.ledger), 1)
        self.assertEqual(self.runtime.read_text(encoding="utf-8"), "{not json")

    def test_corrupt_ledger_does_not_block_the_sync(self):
        self.write(self.image, {"jobs": [job("a")]})
        self.write(self.runtime, {"jobs": []})
        self.ledger.write_text("garbage", encoding="utf-8")
        self.assertEqual(cron_jobs_sync.sync(self.image, self.runtime, self.ledger), 0)
        self.assertEqual([j["id"] for j in self.read(self.runtime)["jobs"]], ["a"])

    def test_dry_run_writes_nothing(self):
        self.write(self.image, {"jobs": [job("a")]})
        self.write(self.runtime, {"jobs": []})
        before = self.runtime.read_text(encoding="utf-8")
        self.assertEqual(
            cron_jobs_sync.sync(self.image, self.runtime, self.ledger, dry_run=True), 0
        )
        self.assertEqual(self.runtime.read_text(encoding="utf-8"), before)
        self.assertFalse(self.ledger.exists())

    def test_no_temp_file_is_left_behind(self):
        self.write(self.image, {"jobs": [job("a")]})
        self.write(self.runtime, {"jobs": []})
        cron_jobs_sync.sync(self.image, self.runtime, self.ledger)
        # `*.tmp*`, not `*.tmp`: the suffix carries the pid so that concurrent
        # writers on a ReadWriteMany volume cannot share one temp file and tear
        # the result. A glob still anchored to the old fixed name would pass
        # while leaking every temp file the new scheme creates.
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.tmp*")), [])

    def test_the_temp_file_is_private_to_this_writer(self):
        """A shared temp name is the tear the rename is supposed to prevent.

        `os.replace` is atomic, so concurrent writers can only lose a merge to
        each other — unless they stage through the SAME temp path, at which point
        A's truncate-and-write interleaves with B's replace and installs a
        half-written `jobs.json`. An agent with a stale schedule still has a
        schedule; an agent with a torn one has none.

        Asserting on the name rather than just "no leftovers": a fixed name leaves
        no leftovers either, so a leak test cannot tell the two apart. The path
        must also stay in the target's directory, or `os.replace` stops being
        atomic by crossing a filesystem.

        The pod name has to be in there, not only the pid. Replicas of one
        scale-up boot the same image through the same entrypoint in the same
        order, each in its own pid namespace, so they can be handed the same small
        pid — and two writers agreeing on `jobs.json.tmp.7` are exactly as exposed
        as two agreeing on `jobs.json.tmp`.
        """
        seen = {}
        # Bound before the patch: `cron_jobs_sync.os` is the stdlib module, so
        # patching it replaces os.replace everywhere, and a capture that reaches
        # for it by name calls the mock standing in for it.
        real_replace = os.replace

        def capture(src, dst):
            seen["src"] = pathlib.Path(src)
            real_replace(src, dst)

        target = self.tmp / "jobs.json"
        with mock.patch.dict(os.environ, {"HOSTNAME": "a-pod-name"}):
            with mock.patch.object(cron_jobs_sync.os, "replace", side_effect=capture):
                cron_jobs_sync.write_json(target, {"jobs": []})

        self.assertEqual(seen["src"].parent, target.parent, "must not cross a filesystem")
        self.assertIn(
            str(os.getpid()),
            seen["src"].name,
            "the temp name must be private to this writer, not a fixed sibling",
        )
        self.assertIn(
            "a-pod-name",
            seen["src"].name,
            "the pid alone is not distinct across replicas; the pod name is",
        )

    def test_the_writer_tag_prefers_the_pod_name_over_the_pid(self):
        """HOSTNAME is the pod name in Kubernetes, and it is what makes this unique.

        Two assertions, because either one alone passes against a mistake. Without
        the hostname the tag is not distinct across replicas; without the pid it is
        not distinct across a container restart, which is how a killed writer's
        abandoned temp file becomes indistinguishable from the live one's.
        """
        with mock.patch.dict(os.environ, {"HOSTNAME": "chat-agent-7d9f-abcde"}):
            tag = cron_jobs_sync.writer_tag()

        self.assertIn("chat-agent-7d9f-abcde", tag)
        self.assertIn(str(os.getpid()), tag)

    def test_the_writer_tag_falls_back_when_hostname_is_unset(self):
        """Off Kubernetes there may be no HOSTNAME, and a crash here is not an option.

        `gethostname` answers on any host worth running on, but the tag must degrade
        to something usable rather than raise or return an empty suffix — a
        `jobs.json.tmp.` with nothing after it is the fixed name this exists to
        avoid.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cron_jobs_sync.socket, "gethostname", return_value=""):
                tag = cron_jobs_sync.writer_tag()

        self.assertTrue(tag.startswith("nohost."), tag)
        self.assertIn(str(os.getpid()), tag)

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        """The pid in the name is a reuse hazard if a temp file outlives its writer.

        Pids wrap. A `jobs.json.tmp.4242` abandoned by a crashed boot is not inert
        the way a fixed-name leftover was: the next process to draw 4242 adopts a
        file it never created. `write_json` only ever truncates it, so nothing is
        read back — but it is left on the volume forever, one per crashed boot,
        and it is the kind of litter that gets mistaken for state.

        The failure is injected at `os.replace`, not at `json.dumps`: dumps is
        evaluated before `write_text` opens anything, so failing there leaves no
        temp file to clean up and the test would pass with the cleanup deleted.
        """
        target = self.tmp / "jobs.json"
        with mock.patch.object(
            cron_jobs_sync.os, "replace", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaises(OSError):
                cron_jobs_sync.write_json(target, {"jobs": []})
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.tmp*")), [])
        self.assertFalse(target.exists(), "a failed write must not create the target")


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_assume_retired_is_parsed_as_a_comma_list(self):
        image = self.tmp / "image.json"
        runtime = self.tmp / "runtime.json"
        ledger = self.tmp / "ledger.json"
        image.write_text(json.dumps({"jobs": [job("a"), job("b")]}), encoding="utf-8")
        runtime.write_text(json.dumps({"jobs": []}), encoding="utf-8")

        rc = cron_jobs_sync.main(
            [
                "--image-jobs", str(image),
                "--runtime-jobs", str(runtime),
                "--ledger", str(ledger),
                "--assume-retired", "a, b",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(runtime.read_text(encoding="utf-8"))["jobs"], [])


if __name__ == "__main__":
    unittest.main()
