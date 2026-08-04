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
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
        """last_run/deliver/origin are the deployment's, not the image's.

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

    def test_runtime_disable_is_not_overridden(self):
        """An operator who disabled a job in a live deployment keeps it disabled."""
        merged, _, _ = cron_jobs_sync.reconcile(
            [job("j", enabled=True)], [job("j", enabled=False)], set()
        )
        self.assertFalse(merged[0]["enabled"])

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

    def test_runtime_field_absent_in_runtime_is_not_inherited_from_image(self):
        """A job never run has no last_run; the image must not supply a stale one."""
        merged, _, _ = cron_jobs_sync.reconcile(
            [job("j", last_run="2020-01-01T00:00:00Z")], [job("j")], set()
        )
        self.assertNotIn("last_run", merged[0])


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
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.tmp")), [])


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
