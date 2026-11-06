# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The in-pod half of bench/tf/prebuilt/unchanged-manifest-digest.

`plant.py` runs the previous scheduled drift run with no model: `start`, a
collector, `draft` and `finish --scheduled`. These tests run it against the real
`audit_report.py` on the no-forge path and a stub collector, and check what it
leaves: a stored scheduled envelope that holds the mark, the digest record a
scheduled `finish` writes, and a `precheck` of the same manifest that closes the
next run. They also check that the teardown removes the record and only the
envelopes that hold the mark. `arm.py` is checked for what it records.
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STACK = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "unchanged-manifest-digest"
_PLANT = _STACK / "plant.py"
_ARM = _STACK / "arm.py"
_SCRIPTS = _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
_MARK = "unchanged-digest-test"
_MANIFEST = {
    "audit": "fleet-consistency-drift",
    "checks_revision": "rev-a",
    "started_at": "2099-01-01T06:20:00Z",
    "finished_at": "2099-01-01T06:21:00Z",
    "clusters": [
        {
            "name": "p/us-central1/seeded-c",
            "project": "p",
            "location": "us-central1",
            "outcome": "collected",
            "commands": [
                {"check": "authorized-networks", "command": "gcloud container clusters list --project p --format=json", "rc": 0, "duration_s": 1.0},
            ],
            "candidates": [
                {
                    "check": "authorized-networks",
                    "namespace": "",
                    "object": "Cluster/seeded-c",
                    "severity": "critical",
                    "excerpt": "baseline: enabled\noutlier: disabled",
                    "impact": "the control plane is open",
                }
            ],
        }
    ],
}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlantTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        env = mock.patch.dict(
            os.environ,
            {
                "FLEET_AUDIT_REPORTS_DIR": str(self.root / "reports"),
                "FLEET_AUDIT_SCRATCH_DIR": str(self.root / "scratch"),
                "FLEET_AUDIT_GITOPS_ROOT": str(self.root / "gitops"),
                "CREDENTIAL_PROXY_URL": "",
                "PLATFORM_AGENT_HOME": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("audit_report", "manifest_delta", "gitops_workspace"):
            sys.modules.pop(name, None)
        self.plant = _load(_PLANT, "unchanged_manifest_digest_plant")
        self.audit_report = self.plant.load(str(_SCRIPTS))
        # No forge: `start` and `finish` take the no-forge path, with no git or broker call.
        no_remote = type(sys)("github_token_refresh")
        no_remote.get_current_git_repo = lambda cwd=None: None
        import gitops_workspace  # noqa: PLC0415 - on sys.path once the plant loaded audit_report

        for patcher in (
            mock.patch.object(gitops_workspace, "get_managed_repos", return_value=[]),
            mock.patch.object(gitops_workspace, "_read_state_text", lambda key: ""),
            mock.patch.object(gitops_workspace, "get_context_github_repo_entries", return_value=[]),
            mock.patch.dict(sys.modules, {"github_token_refresh": no_remote}),
            mock.patch.object(self.audit_report, "refresh_credentials", lambda repo=None: None),
            mock.patch.object(self.audit_report, "claim_in_flight", lambda *a, **k: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_plant(self, manifest, args=()):
        collected = subprocess.CompletedProcess([], 0, stdout=json.dumps(manifest), stderr="")
        err = io.StringIO()
        with mock.patch.object(self.plant.subprocess, "run", return_value=collected) as run, contextlib.redirect_stderr(err):
            code = self.plant.plant(self.audit_report, _MARK, str(_SCRIPTS), list(args))
        self.collector_calls = run.call_args_list
        self.err = err.getvalue()
        return code

    def store(self):
        return self.root / "reports" / self.plant.AUDIT / "no_forge" / "local"

    def latest(self):
        return json.loads((self.store() / "latest.json").read_text(encoding="utf-8"))

    def test_the_plant_leaves_a_scheduled_run_with_the_mark_and_the_digest_record(self):
        self.assertEqual(self.run_plant(_MANIFEST, ["--scope-projects", "p"]), 0, self.err)
        latest = self.latest()
        self.assertTrue(latest["scheduled"])
        self.assertNotIn("precheck", latest)
        titles = [f["title"] for f in latest["document"]["findings"]]
        self.assertEqual(titles, [f"{_MARK} authorized-networks Cluster/seeded-c"])
        self.assertTrue(self.plant.digest_record_path(self.audit_report).is_file())
        self.assertTrue(self.plant.planted_manifest_path(self.audit_report).is_file())
        argv = self.collector_calls[0].args[0]
        self.assertEqual(argv[1:], [str(_SCRIPTS / "fleet_drift.py"), "--scope-projects", "p", "--config-connector"])

    def test_a_precheck_of_the_same_manifest_closes_the_next_run(self):
        self.assertEqual(self.run_plant(_MANIFEST), 0, self.err)
        again = json.loads(json.dumps(_MANIFEST))
        again["clusters"][0]["commands"][0]["duration_s"] = 7.0
        path = self.root / "again.json"
        path.write_text(json.dumps(again), encoding="utf-8")
        code, line = self.plant.run_cli(
            self.audit_report,
            ["precheck", "--audit", self.plant.AUDIT, "--manifest-file", str(path), "--scheduled"],
        )
        self.assertEqual(code, 0)
        payload = json.loads(line.strip().splitlines()[-1])
        self.assertEqual(payload["precheck"], "unchanged")
        self.assertEqual(self.latest()["precheck"], "unchanged")

    def test_no_candidate_fails_the_plant(self):
        empty = {**_MANIFEST, "clusters": [{**_MANIFEST["clusters"][0], "candidates": []}]}
        self.assertEqual(self.run_plant(empty), 1)
        self.assertIn("no candidate", self.err)

    def test_a_collector_that_gives_no_manifest_fails_the_plant(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch.object(self.plant.subprocess, "run", return_value=failed), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.plant.plant(self.audit_report, _MARK, str(_SCRIPTS), []), 1)

    def test_the_teardown_removes_the_record_and_only_marked_envelopes(self):
        self.assertEqual(self.run_plant(_MANIFEST), 0, self.err)
        runs = self.store() / "runs"
        kept = runs / "00000000T000000Z.json"
        kept.write_text(json.dumps({"document": {"findings": []}}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.plant.teardown(self.audit_report, _MARK), 0)
        self.assertFalse((self.store() / "latest.json").exists())
        self.assertEqual(sorted(runs.glob("*.json")), [kept])
        self.assertFalse(self.plant.digest_record_path(self.audit_report).exists())
        self.assertFalse(self.plant.planted_manifest_path(self.audit_report).exists())

    def test_become_data_owner_does_nothing_when_not_root(self):
        with mock.patch.object(self.plant.os, "geteuid", return_value=1000):
            self.assertIn("running as uid 1000", self.plant.become_data_owner(str(self.root)))

    def test_become_data_owner_takes_the_scratch_owner(self):
        scratch = self.root / "scratch"
        scratch.mkdir(exist_ok=True)
        owner = scratch.stat()
        calls = []
        with (
            mock.patch.object(self.plant.os, "geteuid", return_value=0),
            mock.patch.object(self.plant.os, "setgroups", lambda groups: calls.append(("groups", groups))),
            mock.patch.object(self.plant.os, "setgid", lambda gid: calls.append(("gid", gid))),
            mock.patch.object(self.plant.os, "setuid", lambda uid: calls.append(("uid", uid))),
        ):
            if owner.st_uid == 0:
                self.assertIn("root-owned", self.plant.become_data_owner(str(self.root)))
                self.assertEqual(calls, [])
            else:
                self.assertIn(f"became uid {owner.st_uid}", self.plant.become_data_owner(str(self.root)))
                self.assertEqual(calls, [("groups", []), ("gid", owner.st_gid), ("uid", owner.st_uid)])


class ArmTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.arm = _load(_ARM, "unchanged_manifest_digest_arm")
        roster = self.home / "profiles" / "platform" / "cron"
        roster.mkdir(parents=True)
        (roster / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "fleet-consistency-drift", "enabled": False, "state": "paused", "paused_at": "x"}]}),
            encoding="utf-8",
        )

    def test_fire_records_that_the_job_was_paused(self):
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.arm.subprocess, "run", return_value=done), contextlib.redirect_stdout(io.StringIO()):
            self.arm.fire(str(self.home), "python3", "fleet-consistency-drift")
        state = json.loads((self.home / self.arm.STATE_NAME).read_text(encoding="utf-8"))
        self.assertTrue(state["was_paused"])
        self.assertFalse(state["repaused"])
        self.assertIn("fired_at", state)

    def test_clean_pauses_the_job_again_once(self):
        (self.home / self.arm.STATE_NAME).write_text(
            json.dumps({"job_id": "fleet-consistency-drift", "fired_at": "2026-11-05T08:20:00+00:00", "was_paused": True, "repaused": False}),
            encoding="utf-8",
        )
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(self.arm.subprocess, "run", return_value=done) as run, contextlib.redirect_stdout(io.StringIO()):
            self.arm.clean(str(self.home))
        self.assertEqual(run.call_args.args[0][-3:], ["cron", "pause", "fleet-consistency-drift"])
        self.assertFalse((self.home / self.arm.STATE_NAME).exists())

    def test_clean_leaves_an_unpaused_job_alone(self):
        (self.home / self.arm.STATE_NAME).write_text(
            json.dumps({"job_id": "fleet-consistency-drift", "fired_at": "2026-11-05T08:20:00+00:00", "was_paused": False, "repaused": False}),
            encoding="utf-8",
        )
        with mock.patch.object(self.arm.subprocess, "run") as run:
            self.arm.clean(str(self.home))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
