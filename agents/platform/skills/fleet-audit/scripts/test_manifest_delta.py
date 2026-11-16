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

"""manifest_delta: the digest of a collector manifest and the gate it decides."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manifest_delta  # noqa: E402

AUDIT = "compliance-audit"
NOW = 1_800_000_000.0
DAY = 86400


def manifest(**overrides):
    base = {
        "version": 1,
        "checks_revision": "rev-a",
        "audit": AUDIT,
        "started_at": "2026-11-05T06:20:00Z",
        "finished_at": "2026-11-05T06:23:30Z",
        "clusters": [
            {
                "name": "acme-prod/us-central1/prod",
                "project": "acme-prod",
                "autopilot": False,
                "outcome": "collected",
                "commands": [
                    {"check": "netpol-missing", "command": "KUBECONFIG=/tmp/a kubectl get netpol", "rc": 0, "duration_s": 1.5, "output_sha256": "aa"},
                    {"check": "cluster-admin-binding", "command": "KUBECONFIG=/tmp/a kubectl get crb", "rc": 0, "duration_s": 0.4, "output_sha256": "bb"},
                ],
                "checks_not_applicable": [{"check": "hostpath-mount", "reason": "Autopilot"}],
                "candidates": [
                    {"check": "netpol-missing", "namespace": "payments", "object": "Namespace/payments", "severity": "major", "excerpt": "no policy"},
                ],
            },
            {
                "name": "acme-stage/europe-west1/stage",
                "outcome": "collected",
                "commands": [{"check": "netpol-missing", "command": "kubectl get netpol", "rc": 0, "duration_s": 2.0}],
                "candidates": [],
                "limitations": "one zone did not answer",
            },
        ],
    }
    base.update(overrides)
    return base


class DigestTest(unittest.TestCase):
    def test_volatile_fields_leave_the_digest_unchanged(self):
        before = manifest_delta.manifest_digest(manifest())
        later = manifest(started_at="2026-11-06T06:20:00Z", finished_at="2026-11-06T06:29:00Z")
        for command in later["clusters"][0]["commands"]:
            command["duration_s"] = 99.0
            command["output_sha256"] = "zz"
            command["command"] = command["command"].replace("/tmp/a", "/tmp/b")
        self.assertEqual(manifest_delta.manifest_digest(later), before)

    def test_the_order_of_clusters_and_commands_does_not_count(self):
        before = manifest_delta.manifest_digest(manifest())
        shuffled = manifest()
        shuffled["clusters"].reverse()
        shuffled["clusters"][1]["commands"].reverse()
        self.assertEqual(manifest_delta.manifest_digest(shuffled), before)

    def test_the_digest_is_hex_and_stable(self):
        digest = manifest_delta.manifest_digest(manifest())
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, manifest_delta.manifest_digest(copy.deepcopy(manifest())))

    def assert_changes(self, mutate, what):
        before = manifest_delta.manifest_digest(manifest())
        changed = manifest()
        mutate(changed)
        self.assertNotEqual(manifest_delta.manifest_digest(changed), before, what)

    def test_an_added_candidate_changes_the_digest(self):
        self.assert_changes(
            lambda m: m["clusters"][1]["candidates"].append({"check": "netpol-missing", "namespace": "x", "object": "Namespace/x", "severity": "major"}),
            "added candidate",
        )

    def test_a_removed_candidate_changes_the_digest(self):
        self.assert_changes(lambda m: m["clusters"][0]["candidates"].clear(), "removed candidate")

    def test_a_candidates_severity_changes_the_digest(self):
        self.assert_changes(lambda m: m["clusters"][0]["candidates"][0].update(severity="critical"), "severity")
        self.assert_changes(lambda m: m["clusters"][0]["candidates"][0].update(object="Namespace/other"), "object")

    def test_a_candidates_excerpt_and_impact_do_not_count(self):
        """The cost collector writes ages into excerpts (`AGE=12d`), the stockout
        collector log-window stamps; a static fleet would otherwise digest
        differently every day. `finish` adopts the fresh excerpt on a close."""
        before = manifest_delta.manifest_digest(manifest())
        aged = manifest()
        aged["clusters"][0]["candidates"][0].update(excerpt="phase=Available, unclaimed, AGE=13d", impact="idle for 13 days")
        self.assertEqual(manifest_delta.manifest_digest(aged), before)
        shuffled = manifest()
        shuffled["clusters"][0]["candidates"].append({"check": "x", "namespace": "", "object": "Namespace/x", "severity": "minor"})
        reordered = manifest()
        reordered["clusters"][0]["candidates"].insert(0, {"check": "x", "namespace": "", "object": "Namespace/x", "severity": "minor"})
        self.assertEqual(manifest_delta.manifest_digest(shuffled), manifest_delta.manifest_digest(reordered))

    def test_an_authoritative_impact_counts(self):
        """On a check with more than one arm the collector's impact says which arm fired,
        and on some of them nothing else does: a pool that moved from zone-locked to its
        ceiling, with the same severity, is a changed fleet."""
        pool = {"check": "single-zone-nodepool", "namespace": "", "object": "NodePool/default", "severity": "major", "excerpt": "e", "impact": "zone-locked", "impact_authoritative": True}
        locked = manifest()
        locked["clusters"][0]["candidates"].append(dict(pool))
        ceiling = manifest()
        ceiling["clusters"][0]["candidates"].append({**pool, "impact": "at its ceiling", "excerpt": "e2"})
        self.assertNotEqual(manifest_delta.manifest_digest(locked), manifest_delta.manifest_digest(ceiling))
        excerpt_only = manifest()
        excerpt_only["clusters"][0]["candidates"].append({**pool, "excerpt": "e2"})
        self.assertEqual(manifest_delta.manifest_digest(locked), manifest_delta.manifest_digest(excerpt_only))
        unmarked = manifest()
        unmarked["clusters"][0]["candidates"].append({**pool, "impact_authoritative": False})
        reworded = manifest()
        reworded["clusters"][0]["candidates"].append({**pool, "impact_authoritative": False, "impact": "idle for 13 days"})
        self.assertEqual(manifest_delta.manifest_digest(unmarked), manifest_delta.manifest_digest(reworded))

    def test_a_check_that_stopped_running_changes_the_digest(self):
        self.assert_changes(lambda m: m["clusters"][0]["commands"].pop(), "dropped command")
        self.assert_changes(lambda m: m["clusters"][0]["commands"][0].update(rc=1), "rc")

    def test_not_applicable_and_limitations_change_the_digest(self):
        self.assert_changes(lambda m: m["clusters"][0]["checks_not_applicable"].clear(), "checks_not_applicable")
        self.assert_changes(lambda m: m["clusters"][1].update(limitations="two zones did not answer"), "limitations")

    def test_an_unevaluated_check_changes_the_digest(self):
        self.assert_changes(
            lambda m: m["clusters"][0].update(checks_unevaluated=[{"check": "x", "reason": "read failed"}]),
            "checks_unevaluated",
        )

    def test_a_new_checks_revision_changes_the_digest(self):
        self.assert_changes(lambda m: m.update(checks_revision="rev-b"), "checks_revision")

    def test_a_new_target_changes_the_digest(self):
        self.assert_changes(lambda m: m["clusters"].append({"name": "new", "outcome": "collected", "commands": [], "candidates": []}), "new target")

    def test_an_outcome_change_changes_the_digest(self):
        self.assert_changes(lambda m: m["clusters"][1].update(outcome="unreachable", error="rc=1"), "outcome")

    def test_a_key_this_module_does_not_name_counts(self):
        """A field a collector adds later is a change until named volatile here."""
        self.assert_changes(lambda m: m["clusters"][0].update(kcc_unready=[{"kind": "ContainerCluster", "name": "c"}]), "kcc_unready")
        self.assert_changes(lambda m: m.update(config_connector_read="in-flight"), "top-level key")

    def test_malformed_entries_do_not_raise(self):
        odd = {"clusters": ["not-a-dict", {"name": "x", "commands": "not-a-list"}, {"commands": [1, "two"]}], "error": None}
        self.assertEqual(len(manifest_delta.manifest_digest(odd)), 64)
        self.assertEqual(len(manifest_delta.manifest_digest({})), 64)


class GateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scratch = Path(tmp.name) / "scratch"

    def gate(self, m, now=NOW, **kwargs):
        return manifest_delta.should_run_llm_synthesis(AUDIT, m, now, scratch_dir=self.scratch, **kwargs)

    def record(self, m, now=NOW, full_run=True):
        return manifest_delta.record_scheduled_digest(AUDIT, m, now, full_run=full_run, scratch_dir=self.scratch)

    def test_no_record_is_a_first_run(self):
        self.assertEqual(self.gate(manifest()), (True, manifest_delta.VERDICT_FIRST_RUN))
        self.assertFalse(manifest_delta.digest_path_for(AUDIT, self.scratch).exists())

    def test_the_same_manifest_the_next_day_is_unchanged(self):
        self.record(manifest(), now=NOW)
        later = manifest(started_at="2026-11-06T06:20:00Z", finished_at="2026-11-06T06:24:00Z")
        self.assertEqual(self.gate(later, now=NOW + DAY), (False, manifest_delta.VERDICT_UNCHANGED))

    def test_a_changed_digest_is_findings_changed(self):
        self.record(manifest())
        changed = manifest()
        changed["clusters"][0]["candidates"].clear()
        self.assertEqual(self.gate(changed, now=NOW + DAY), (True, manifest_delta.VERDICT_FINDINGS_CHANGED))

    def test_an_unevaluated_check_is_findings_changed_even_when_recorded(self):
        """A record written from a manifest with a gap does not make the same gap unchanged."""
        gapped = manifest()
        gapped["clusters"][0]["checks_unevaluated"] = [{"check": "x", "reason": "read failed"}]
        self.record(gapped)
        self.assertEqual(self.gate(gapped, now=NOW + DAY), (True, manifest_delta.VERDICT_FINDINGS_CHANGED))

    def test_an_uncollected_target_is_findings_changed(self):
        partial = manifest()
        partial["clusters"][1].update(outcome="gate-failed", error="truncated")
        self.record(partial)
        self.assertEqual(self.gate(partial, now=NOW + DAY), (True, manifest_delta.VERDICT_FINDINGS_CHANGED))

    def test_an_out_of_scope_target_is_not_a_gap(self):
        ruled_out = manifest()
        ruled_out["clusters"].append({"name": "alpha", "outcome": "out-of-scope"})
        self.record(ruled_out)
        self.assertEqual(self.gate(ruled_out, now=NOW + DAY), (False, manifest_delta.VERDICT_UNCHANGED))

    def test_a_top_level_error_is_findings_changed(self):
        self.record(manifest())
        self.assertEqual(self.gate(manifest(error="nothing enumerated", clusters=[]), now=NOW + DAY), (True, manifest_delta.VERDICT_FINDINGS_CHANGED))

    def test_a_new_checks_revision_is_findings_changed(self):
        self.record(manifest())
        self.assertEqual(self.gate(manifest(checks_revision="rev-b"), now=NOW + DAY), (True, manifest_delta.VERDICT_FINDINGS_CHANGED))

    def test_unchanged_expires_at_the_silent_age(self):
        self.record(manifest(), now=NOW)
        self.assertEqual(self.gate(manifest(), now=NOW + 7 * DAY - 1), (False, manifest_delta.VERDICT_UNCHANGED))
        self.assertEqual(self.gate(manifest(), now=NOW + 7 * DAY), (True, manifest_delta.VERDICT_PERIODIC_REFRESH))

    def test_the_silent_age_is_a_parameter(self):
        self.record(manifest(), now=NOW)
        self.assertEqual(self.gate(manifest(), now=NOW + 2 * DAY, max_silent_age_seconds=DAY), (True, manifest_delta.VERDICT_PERIODIC_REFRESH))

    def test_a_carried_close_keeps_last_full_run_at(self):
        self.record(manifest(), now=NOW, full_run=True)
        self.record(manifest(), now=NOW + DAY, full_run=False)
        record = manifest_delta.read_record(AUDIT, self.scratch)
        self.assertEqual(record[manifest_delta.RECORD_LAST_FULL_RUN_AT_KEY], manifest_delta._iso(NOW))
        self.assertEqual(record[manifest_delta.RECORD_RECORDED_AT_KEY], manifest_delta._iso(NOW + DAY))
        # Six carried closes later the refresh still fires on the seventh day.
        self.assertEqual(self.gate(manifest(), now=NOW + 7 * DAY), (True, manifest_delta.VERDICT_PERIODIC_REFRESH))

    def test_a_full_run_moves_last_full_run_at(self):
        self.record(manifest(), now=NOW, full_run=True)
        self.record(manifest(), now=NOW + 3 * DAY, full_run=True)
        self.assertEqual(self.gate(manifest(), now=NOW + 9 * DAY), (False, manifest_delta.VERDICT_UNCHANGED))

    def test_a_carried_close_with_no_record_takes_now(self):
        self.record(manifest(), now=NOW, full_run=False)
        record = manifest_delta.read_record(AUDIT, self.scratch)
        self.assertEqual(record[manifest_delta.RECORD_LAST_FULL_RUN_AT_KEY], manifest_delta._iso(NOW))

    def test_the_record_names_the_envelope_when_given_one(self):
        path = manifest_delta.record_scheduled_digest(
            AUDIT, manifest(), NOW, full_run=True, scratch_dir=self.scratch, envelope_finished_at="2026-11-05T06:30:00+00:00"
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record[manifest_delta.RECORD_ENVELOPE_KEY], "2026-11-05T06:30:00+00:00")
        self.assertEqual(manifest_delta.read_record(AUDIT, self.scratch)[manifest_delta.RECORD_ENVELOPE_KEY], "2026-11-05T06:30:00+00:00")
        path = self.record(manifest())
        self.assertNotIn(manifest_delta.RECORD_ENVELOPE_KEY, json.loads(path.read_text(encoding="utf-8")))

    def test_the_record_names_the_stream_and_the_checks_revision(self):
        path = self.record(manifest())
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record[manifest_delta.RECORD_AUDIT_KEY], AUDIT)
        self.assertEqual(record[manifest_delta.RECORD_CHECKS_REVISION_KEY], "rev-a")
        self.assertEqual(record[manifest_delta.RECORD_DIGEST_KEY], manifest_delta.manifest_digest(manifest()))
        self.assertEqual(path.name, "last_scheduled_digest_compliance-audit.json")

    def test_a_record_for_another_stream_is_no_record(self):
        path = self.record(manifest())
        record = json.loads(path.read_text(encoding="utf-8"))
        record[manifest_delta.RECORD_AUDIT_KEY] = "obtainability-audit"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.gate(manifest()), (True, manifest_delta.VERDICT_FIRST_RUN))

    def test_an_unreadable_record_is_no_record(self):
        path = manifest_delta.digest_path_for(AUDIT, self.scratch)
        path.parent.mkdir(parents=True)
        for text in ("{not json", "[]", json.dumps({"audit": AUDIT}), json.dumps({"audit": AUDIT, "digest": ""})):
            path.write_text(text, encoding="utf-8")
            self.assertEqual(self.gate(manifest()), (True, manifest_delta.VERDICT_FIRST_RUN), text)

    def test_a_record_without_a_readable_time_refreshes(self):
        path = self.record(manifest())
        record = json.loads(path.read_text(encoding="utf-8"))
        record[manifest_delta.RECORD_LAST_FULL_RUN_AT_KEY] = "yesterday"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.gate(manifest()), (True, manifest_delta.VERDICT_PERIODIC_REFRESH))

    def test_an_epoch_time_in_the_record_is_read(self):
        path = self.record(manifest())
        record = json.loads(path.read_text(encoding="utf-8"))
        record[manifest_delta.RECORD_LAST_FULL_RUN_AT_KEY] = NOW
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.gate(manifest(), now=NOW + DAY), (False, manifest_delta.VERDICT_UNCHANGED))

    def test_the_write_is_atomic_and_leaves_no_temp_file(self):
        self.record(manifest())
        self.record(manifest(checks_revision="rev-b"))
        self.assertEqual([p.name for p in self.scratch.iterdir()], ["last_scheduled_digest_compliance-audit.json"])


if __name__ == "__main__":
    unittest.main()
