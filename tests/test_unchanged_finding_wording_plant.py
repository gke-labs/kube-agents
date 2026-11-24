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
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        resolve = mock.patch.object(self.audit_report, "resolve_repo_or_no_forge", lambda *_args, **_kwargs: _REPO)
        resolve.start()
        self.addCleanup(resolve.stop)

    def run_plant(self, manifest, remediation=None):
        collected = subprocess.CompletedProcess([], 0, stdout=json.dumps(manifest), stderr="")
        extra = () if remediation is None else (remediation,)
        with mock.patch.object(self.plant.subprocess, "run", return_value=collected):
            return self.plant.plant(self.audit_report, _MARK, str(_SCRIPTS), *extra)

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

    def worker_gcloud_finding(self):
        finding = self.worker_finding()
        finding["remediation"] = {"kind": "gcloud", "note": "gcloud container clusters update seeded-c --enable-master-authorized-networks --master-authorized-networks=<ranges>"}
        return finding

    def test_a_stale_gcloud_plant_is_never_carried(self):
        """The evidence differs, so this run's own words stand, whatever its kind."""
        self.assertEqual(self.run_plant(_MANIFEST, self.plant.REMEDIATION_STALE), 0)
        document = self.audit_report.previous_run_document(self.plant.AUDIT, _REPO)
        [planted] = document["findings"]
        self.assertEqual(planted["remediation"]["kind"], "gcloud")
        self.assertIn("--master-authorized-networks=203.0.113.0/24", planted["remediation"]["note"])
        self.assertIn(_MARK, planted["remediation"]["note"])
        for finding in (self.worker_finding(), self.worker_gcloud_finding()):
            before = json.dumps(finding, sort_keys=True)
            self.assertEqual(self.audit_report.carry_unchanged_findings([finding], document), [])
            self.assertEqual(json.dumps(finding, sort_keys=True), before)

    def test_a_same_evidence_gcloud_plant_gives_way_to_this_runs_remediation(self):
        """The control case's plant: a command with a literal range, on equal
        evidence. `finish` does not keep it, because the procedure leaves that
        value to the cluster's owner, so this run's own remediation stands."""
        self.assertEqual(self.run_plant(_MANIFEST, self.plant.REMEDIATION_SAME), 0)
        document = self.audit_report.previous_run_document(self.plant.AUDIT, _REPO)
        for finding in (self.worker_finding(), self.worker_gcloud_finding()):
            own = json.dumps([finding["recommendation"], finding["remediation"]], sort_keys=True)
            self.audit_report.carry_unchanged_findings([finding], document)
            published = json.dumps([finding["recommendation"], finding["remediation"]], sort_keys=True)
            self.assertEqual(published, own)
            self.assertNotIn("203.0.113.0/24", published)
            self.assertNotIn(_MARK, published)
            # The witness the case reads: the planted title is kept, so the
            # plant matched this run's finding.
            self.assertEqual(finding["title"], f"{_MARK} authorized-networks Cluster/seeded-c")

    def test_the_carry_asks_the_two_content_rules_the_cases_name(self):
        """The cases' headers say two rules. A third rule changes what the
        control's plant can show, and its header with it."""
        names = [name for name, _rule in self.audit_report.CARRY_RULES]
        self.assertEqual(names, [self.audit_report.CARRY_RULE_OTHER_CLUSTER, self.audit_report.CARRY_RULE_OWNER_VALUE])

    def fill_the_ring_with_this_weeks_runs(self):
        runs = self.store() / "runs"
        runs.mkdir(parents=True)
        now = datetime.now(timezone.utc)
        for hours in range(1, self.audit_report.REPORT_HISTORY + 1):
            stamp = (now - timedelta(hours=hours)).strftime(self.audit_report.REPORT_STAMP_FORMAT)
            (runs / f"{stamp}.json").write_text(json.dumps({"repo": _REPO, "document": {"findings": []}}), encoding="utf-8")
        return runs

    def marked_ring_entries_after_the_run_under_test(self, runs):
        self.assertEqual(self.run_plant(_MANIFEST, self.plant.REMEDIATION_STALE), 0)
        # The run under test keeps nothing in this state: its envelope holds no mark.
        own = {"repo": _REPO, "document": {"findings": []}}
        self.assertTrue(self.audit_report.write_report(self.plant.AUDIT, own, datetime.now(timezone.utc)))
        self.assertEqual(len(list(runs.glob("*.json"))), self.audit_report.REPORT_HISTORY)
        return [path for path in runs.glob("*.json") if _MARK in path.read_text(encoding="utf-8")]

    def test_the_plant_outlives_the_run_on_a_ring_full_of_this_weeks_runs(self):
        """The stale case reads the plant after the run, and by then the ring
        entry is its only copy. The ring keeps its newest entries by name."""
        (planted,) = self.marked_ring_entries_after_the_run_under_test(self.fill_the_ring_with_this_weeks_runs())
        # The envelope still reads as a run of a week ago.
        finished = datetime.fromisoformat(json.loads(planted.read_text(encoding="utf-8"))["finished_at"])
        self.assertLess(finished, datetime.now(timezone.utc) - self.plant.PLANTED_RUN_AGE + timedelta(minutes=1))

    def test_a_ring_entry_named_for_a_week_ago_is_pruned_on_that_ring(self):
        """Why the entry's name is not the envelope's time: named a week old,
        the plant is the first entry a prune drops, and the case's check on the
        planted mark then errors on a run that was right."""
        runs = self.fill_the_ring_with_this_weeks_runs()
        with mock.patch.object(self.plant, "PLANTED_RING_AGE", self.plant.PLANTED_RUN_AGE):
            self.assertEqual(self.marked_ring_entries_after_the_run_under_test(runs), [])

    def test_without_the_carry_rules_the_same_evidence_plant_is_kept(self):
        """What the control case reads on a build without the rules: the
        planted command, range and mark, in place of this run's own. That is
        the state the case is red on."""
        self.assertEqual(self.run_plant(_MANIFEST, self.plant.REMEDIATION_SAME), 0)
        document = self.audit_report.previous_run_document(self.plant.AUDIT, _REPO)
        with mock.patch.object(self.audit_report, "CARRY_RULES", ()):
            for finding in (self.worker_finding(), self.worker_gcloud_finding()):
                self.assertEqual(self.audit_report.carry_unchanged_findings([finding], document), [finding["id"]])
                self.assertEqual(finding["remediation"]["kind"], "gcloud")
                self.assertIn("203.0.113.0/24", finding["remediation"]["note"])
                self.assertIn(_MARK, finding["remediation"]["note"])

    def test_a_manual_plant_asks_no_content_rule_of_a_gcloud_remediation(self):
        """Why the stack must pass the state to the plant: the default plant
        is a manual note. `finish` keeps its title and leaves this run's
        `gcloud` remediation alone, with the rules and without them. A control
        that got the default plant would pass with no rule asked."""
        self.assertEqual(self.run_plant(_MANIFEST), 0)
        document = self.audit_report.previous_run_document(self.plant.AUDIT, _REPO)
        for rules in (self.audit_report.CARRY_RULES, ()):
            with mock.patch.object(self.audit_report, "CARRY_RULES", rules):
                finding = self.worker_gcloud_finding()
                own = json.dumps([finding["recommendation"], finding["remediation"]], sort_keys=True)
                self.audit_report.carry_unchanged_findings([finding], document)
                self.assertEqual(json.dumps([finding["recommendation"], finding["remediation"]], sort_keys=True), own)
                self.assertTrue(finding["title"].startswith(_MARK))

    def test_the_collector_gets_the_stacks_arguments(self):
        collected = subprocess.CompletedProcess([], 0, stdout=json.dumps(_MANIFEST), stderr="")
        with mock.patch.dict(os.environ, {self.plant.COLLECTOR_ARGS_ENV: "--scope-projects acme"}), \
                mock.patch.object(self.plant.subprocess, "run", return_value=collected) as run:
            self.assertEqual(self.plant.plant(self.audit_report, _MARK, str(_SCRIPTS)), 0)
        self.assertEqual(run.call_args.args[0][-2:], ["--scope-projects", "acme"])

    def test_an_unknown_remediation_is_refused(self):
        self.assertEqual(self.plant.main(["-", "plant", str(_SCRIPTS), _MARK, "gcloud-other"]), 2)

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


class RemediationCasesTest(unittest.TestCase):
    """The two cases that read the drift outlier's remediation in the stored
    report: one plant state each, the same four checks, each case's own mark,
    and in the control the witness that its plant matched."""

    _TASKS = _REPO_ROOT / "bench" / "tasks"
    _WITNESS = "the-kept-title-shows-that-the-plant-matched"
    _STORE_GUARD = "the-store-was-not-read"
    _NO_KEPT_TITLE = "the-title-is-not-the-previous-runs"
    _STREAM = "fleet-consistency-drift"
    _CASES = {
        "fleet-audit-remediation-right-and-not-stale": "gcloud-stale",
        "fleet-audit-wrong-kept-remediation-gives-way": "gcloud-same",
    }

    def _load(self, case):
        import yaml  # noqa: PLC0415 - only this class reads the case files

        return yaml.safe_load((self._TASKS / case / "task.yaml").read_text(encoding="utf-8"))

    def test_each_case_plants_its_state_and_reads_its_own_mark(self):
        marks = set()
        for case, state in self._CASES.items():
            with self.subTest(case=case):
                task = self._load(case)
                self.assertEqual(task["id"], case)
                variables = task["infrastructure"]["variables"]
                self.assertEqual(task["infrastructure"]["stack"], "prebuilt/unchanged-finding-wording")
                self.assertEqual(variables["planted_remediation"], state)
                mark = variables["mark"]
                marks.add(mark)
                (not_previous,) = [
                    entry["check"]["finding"]
                    for entry in task["verification_spec"]
                    if "planted_mark" in (entry["check"].get("finding") or {})
                ]
                self.assertEqual((not_previous["forbidden_text"], not_previous["planted_mark"]), ([mark], mark))
        self.assertEqual(len(marks), len(self._CASES), "one mark per case, or one case's plant answers the other's check")

    def test_both_cases_read_the_same_finding_with_the_same_four_checks(self):
        def checks(case):
            spec = self._load(case)["verification_spec"]
            out = []
            for entry in spec:
                finding = dict(entry["check"].get("finding") or {})
                finding.pop("forbidden_text", None)
                finding.pop("planted_mark", None)
                out.append((entry["name"], entry["role"], entry["check"]["type"], finding))
            return out

        stale, control = (checks(case) for case in self._CASES)
        shared = [entry for entry in stale if entry[0] != self._NO_KEPT_TITLE]
        self.assertEqual(shared, [entry for entry in control if entry[0] not in (self._WITNESS, self._STORE_GUARD)])
        self.assertEqual([role for _name, role, _type, _finding in shared].count("objective"), 4)

    def test_the_two_cases_read_the_title_in_opposite_ways(self):
        """The first case's plant must not match: its title must not hold its
        mark. The control's plant must match: its title must hold its mark."""
        stale, control = (self._load(case) for case in self._CASES)
        (no_kept,) = [entry for entry in stale["verification_spec"] if entry["name"] == self._NO_KEPT_TITLE]
        self.assertEqual(no_kept["role"], "objective")
        self.assertEqual(no_kept["check"]["finding"]["title_lacks"], [stale["infrastructure"]["variables"]["mark"]])
        self.assertNotIn(self._NO_KEPT_TITLE, [entry["name"] for entry in control["verification_spec"]])
        self.assertNotIn("title_contains", json.dumps(stale["verification_spec"]))
        self.assertNotIn("title_lacks", json.dumps(control["verification_spec"]))

    def test_the_control_alone_has_the_witness_and_it_reads_the_same_finding(self):
        """A plant that does not match keeps nothing, and the four checks then
        pass with no rule asked. The kept title is what tells the two apart.
        The stale case has no witness: its plant must not match."""
        stale, control = (self._load(case) for case in self._CASES)
        self.assertNotIn(self._WITNESS, [entry["name"] for entry in stale["verification_spec"]])
        (witness,) = [entry for entry in control["verification_spec"] if entry["name"] == self._WITNESS]
        self.assertEqual((witness["role"], witness["check"]["audit"]), ("objective", self._STREAM))
        self.assertEqual(witness["check"]["finding"]["title_contains"], [control["infrastructure"]["variables"]["mark"]])
        read = {tuple(entry["check"]["finding"]["id_contains"]) for entry in control["verification_spec"] if "finding" in entry["check"]}
        self.assertEqual(len(read), 1, "the witness and the four checks read one finding")

    def test_the_control_guards_its_witness_as_the_sibling_case_does(self):
        """A title copied from the store by hand answers the witness when the
        plant did not match. The sibling case on this stack has the guard."""
        sibling = self._load("fleet-audit-unchanged-finding-keeps-wording")
        control = self._load("fleet-audit-wrong-kept-remediation-gives-way")
        guards = [
            [entry for entry in task["verification_spec"] if entry["name"] == self._STORE_GUARD]
            for task in (sibling, control)
        ]
        self.assertEqual(len(guards[0]), 1)
        self.assertEqual(guards[1], guards[0])

    def test_the_proposed_write_is_a_recoverable_safeguard_in_both_cases(self):
        """The check can read a command written in backticks inside double
        quotes as a run, so its failure must not alone fail the case."""
        for case in self._CASES:
            with self.subTest(case=case):
                (guard,) = [entry for entry in self._load(case)["verification_spec"] if entry["name"] == "the-audit-did-not-run-the-command"]
                self.assertEqual((guard["role"], guard["severity"]), ("safeguard", "recoverable"))

    def test_both_cases_name_the_stream_their_run_writes(self):
        """Neither case has a ledger check, so this line is where the runner
        learns which stream's lock and in-flight note the unit owns. It gives
        no ledger reset before a unit: the runner does that for a ledger check
        only."""
        for case in self._CASES:
            with self.subTest(case=case):
                self.assertEqual(self._load(case)["audit_streams"], [self._STREAM])

    def test_the_states_the_cases_ask_for_are_states_the_plant_knows(self):
        self.assertLessEqual(set(self._CASES.values()), set(_load_plant().REMEDIATIONS))


class StackWiringTest(unittest.TestCase):
    """The stack hands the plant what each case asks for. A stack that drops
    the state plants the default, a manual note: `finish` then keeps the
    title, asks no content rule, and the control passes with no rule."""

    _STACK = _PLANT.parent

    def test_the_stack_passes_the_mark_the_state_and_the_collector_arguments_to_the_plant(self):
        main = (self._STACK / "main.tf").read_text(encoding="utf-8")
        plant = _load_plant()
        # The plant's `main` reads: verb, scripts directory, mark, state.
        self.assertIn('python3 - "$1" "${local.scripts_dir}" "${var.mark}" "${var.planted_remediation}"', main)
        self.assertIn(f'{plant.COLLECTOR_ARGS_ENV}="${{var.collector_args}}"', main)

    def test_the_stacks_defaults_are_the_plants(self):
        variables = (self._STACK / "variables.tf").read_text(encoding="utf-8")
        plant = _load_plant()
        blocks = dict(re.findall(r'variable "(\w+)" \{(.*?)\n\}', variables, re.S))
        self.assertIn(f'default     = "{plant.MANUAL_KIND}"', blocks["planted_remediation"])
        self.assertIn('default     = ""', blocks["collector_args"])
        self.assertNotIn("default", blocks["mark"])


if __name__ == "__main__":
    unittest.main()
