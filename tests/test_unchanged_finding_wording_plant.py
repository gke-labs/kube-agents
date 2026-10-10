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

"""The in-pod half of bench/tf/prebuilt/unchanged-finding-wording.

`plant.py` writes a previous drift run into the report store. These tests run
it against the real `audit_report.py` and a stub collector, and check that
`finish`'s carry accepts what it plants: the same id, evidence and severity as
a worker's finding for the same candidate. They also check that the teardown
removes only the envelopes that hold the mark.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PLANT = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "unchanged-finding-wording" / "plant.py"
_SCRIPTS = _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
_MARK = "kept-wording-test"
_REPO = "acme/infra"
_MANIFEST = {
    "clusters": [
        {
            "name": "seeded-c",
            "outcome": "collected",
            "commands": [
                {
                    "check": "authorized-networks",
                    "command": "gcloud container clusters list --project p --format=json",
                    "rc": 0,
                }
            ],
            "candidates": [
                {
                    "check": "authorized-networks",
                    "namespace": "",
                    "object": "Cluster/seeded-c",
                    "severity": "critical",
                    "excerpt": "baseline: enabled\noutlier: disabled",
                }
            ],
        }
    ]
}


def _load_plant():
    spec = importlib.util.spec_from_file_location("unchanged_finding_wording_plant", _PLANT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlantTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.plant = _load_plant()
        self.audit_report = self.plant.load(str(_SCRIPTS))
        resolve = mock.patch.object(self.audit_report, "resolve_repo", lambda **_: _REPO)
        resolve.start()
        self.addCleanup(resolve.stop)

    def run_plant(self, manifest):
        collected = subprocess.CompletedProcess([], 0, stdout=json.dumps(manifest), stderr="")
        with mock.patch.object(self.plant.subprocess, "run", return_value=collected):
            return self.plant.plant(self.audit_report, _MARK, str(_SCRIPTS))

    def store(self):
        return self.audit_report.reports_dir_for(self.plant.AUDIT, _REPO)

    def worker_finding(self):
        finding = {
            "check": "authorized-networks",
            "cluster": "seeded-c",
            "namespace": "",
            "object": "Cluster/seeded-c",
            "severity": "critical",
            "title": "seeded-c has no authorized networks",
            "impact": "worker impact",
            "recommendation": {"action": "a", "rationale": "r", "risk": "k"},
            "remediation": {"kind": "manual", "note": "worker note"},
            "evidence": {"command": "gcloud container clusters describe", "excerpt": "x"},
        }
        finding["id"] = self.audit_report.published_id(finding)
        self.audit_report.adopt_collector_evidence([finding], _MANIFEST)
        return finding

    def test_the_carry_keeps_the_planted_wording(self):
        self.assertEqual(self.run_plant(_MANIFEST), 0)
        document = self.audit_report.previous_run_document(self.plant.AUDIT, _REPO)
        finding = self.worker_finding()
        carried = self.audit_report.carry_unchanged_findings([finding], document)
        self.assertEqual(carried, [finding["id"]])
        self.assertEqual(finding["title"], f"{_MARK} authorized-networks Cluster/seeded-c")
        self.assertTrue(finding["remediation"]["note"].startswith(_MARK))

    def test_the_planted_envelope_names_no_issue(self):
        self.run_plant(_MANIFEST)
        envelope = json.loads((self.store() / "latest.json").read_text(encoding="utf-8"))
        self.assertIsNone(envelope["issue_number"])

    def test_no_candidate_fails_the_plant(self):
        self.assertEqual(self.run_plant({"clusters": []}), 1)

    def test_the_teardown_removes_only_marked_envelopes(self):
        self.run_plant(_MANIFEST)
        runs = self.store() / "runs"
        kept = runs / "00000000T000000Z.json"
        kept.write_text(json.dumps({"document": {"findings": []}}), encoding="utf-8")
        self.assertEqual(self.plant.teardown(self.audit_report, _MARK), 0)
        self.assertFalse((self.store() / "latest.json").exists())
        self.assertEqual(sorted(runs.glob("*.json")), [kept])

    def test_a_missing_latest_after_write_report_tears_down_and_fails(self):
        with (
            mock.patch.object(self.audit_report, "write_report", lambda *_a, **_kw: None),
            mock.patch.object(self.plant, "teardown", wraps=self.plant.teardown) as clean,
        ):
            self.assertEqual(self.run_plant(_MANIFEST), 1)
            clean.assert_called_once_with(self.audit_report, _MARK)

    def test_ensure_proxy_path_prepends_an_existing_proxy_directory_once(self):
        proxy = str(self.store())
        pathlib.Path(proxy).mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.plant.ensure_proxy_path(proxy)
            self.assertEqual(os.environ["PATH"], f"{proxy}:/usr/bin:/bin")
            self.plant.ensure_proxy_path(proxy)
            self.assertEqual(os.environ["PATH"], f"{proxy}:/usr/bin:/bin")


if __name__ == "__main__":
    unittest.main()
