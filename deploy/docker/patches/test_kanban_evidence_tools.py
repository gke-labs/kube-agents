"""Unit tests for the typed evidence recorder installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import kanban_evidence_tools as ket

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPACITY_REQUEST = {
    "region": "us-central1",
    "acceleratorType": "nvidia-a100",
    "acceleratorCount": 32,
}
PLANNING_REQUEST = {
    "region": "europe-west4",
    "nodeCount": 64,
    "futureResourcesSpecs": {"spec": {"deploymentType": "DENSE"}},
}


def tool_error(message: str) -> str:
    return f"ERROR: {message}"


class RecorderFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.db_path = Path(self._directory.name) / "kanban.db"
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                    session_id TEXT, created_at REAL, started_at REAL,
                    completed_at REAL, last_heartbeat_at REAL,
                    last_failure_error TEXT, result TEXT
                );
                CREATE TABLE task_runs (
                    id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT
                );
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at REAL
                );
                """
            )
            connection.execute(
                "INSERT INTO tasks VALUES ('t_1', 'Design', 'platform', 'done', "
                "'portal_session', 1, NULL, 2, NULL, '', 'Full report.')"
            )
        self._original_connect = ket._connect
        ket._connect = lambda: sqlite3.connect(self.db_path)
        self.addCleanup(setattr, ket, "_connect", self._original_connect)
        self._original_env = os.environ.get("HERMES_KANBAN_TASK")
        os.environ["HERMES_KANBAN_TASK"] = "t_1"
        self.addCleanup(self._restore_env)
        self.record, self.attach = ket.make_handlers(tool_error)

    def _restore_env(self) -> None:
        if self._original_env is None:
            os.environ.pop("HERMES_KANBAN_TASK", None)
        else:
            os.environ["HERMES_KANBAN_TASK"] = self._original_env

    def rows(self, table: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


class RecordEvidenceTest(RecorderFixture):
    def test_records_a_typed_entry_on_the_workers_own_task(self) -> None:
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "api_method": "compute.beta.AdviceService.Capacity",
                "request": CAPACITY_REQUEST,
                "analysis": {"availableQuantity": 8},
                "execution_ref": "exec-7",
            }
        )
        self.assertEqual(reply, "recorded advice_service_capacity evidence on t_1")
        (row,) = self.rows("task_evidence")
        self.assertEqual(row["task_id"], "t_1")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(json.loads(row["analysis_json"]), {"availableQuantity": 8})

    def test_a_completed_record_must_name_what_it_asked_and_which_api_answered(self) -> None:
        # Observed live: a record with the right method and an empty request
        # (nothing to check), and one with a full request and no method
        # (nothing to attribute). Both were claims, not evidence.
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "api_method": "compute.beta.AdviceService.Capacity",
                "request": {},
                "analysis": {"availableQuantity": 8},
            }
        )
        self.assertIn("must name region, acceleratorType, acceleratorCount", reply)
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "request": CAPACITY_REQUEST,
                "analysis": {"availableQuantity": 8},
            }
        )
        self.assertIn("must name api_method", reply)
        reply = self.record(
            {
                "type": "advice_service_workload_obtainability_planning",
                "api_method": "compute.beta.AdviceService.CalendarMode",
                "request": {"region": "europe-west4"},
                "analysis": {"windowFound": False},
            }
        )
        self.assertIn("nodeCount, futureResourcesSpecs", reply)

    def test_a_failed_probe_may_be_recorded_thinly(self) -> None:
        # A probe that could not run has nothing to report but the attempt,
        # and hiding it would be worse than recording it thinly.
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "status": "failed",
                "analysis": {"notes": "FLEX_START rejected by the SDK"},
            }
        )
        self.assertNotIn("ERROR", reply)

    def test_rejects_types_outside_the_evidence_contract(self) -> None:
        reply = self.record({"type": "vibes", "analysis": {}})
        self.assertIn("unknown evidence type 'vibes'", reply)
        self.assertEqual(self.rows("tasks")[0]["id"], "t_1")

    def test_refuses_to_write_onto_another_workers_task(self) -> None:
        reply = self.record(
            {"task_id": "t_other", "type": "quota_check", "request": {"region": "r"}, "analysis": {}}
        )
        self.assertIn("scoped to task t_1", reply)

    def test_refuses_a_task_that_does_not_exist(self) -> None:
        os.environ["HERMES_KANBAN_TASK"] = "t_ghost"
        reply = self.record({"type": "quota_check", "request": {"region": "r"}, "analysis": {}})
        self.assertIn("does not exist on this board", reply)

    def test_bounds_the_analysis_payload(self) -> None:
        reply = self.record(
            {
                "type": "quota_check",
                "request": {"region": "us-central1"},
                "analysis": {"blob": "x" * (ket.MAX_OBJECT_BYTES + 1)},
            }
        )
        self.assertIn("exceeds", reply)


class AttachArtifactTest(RecorderFixture):
    def test_attaches_a_structured_manifest_with_its_target_and_shape(self) -> None:
        reply = self.attach(
            {
                "type": "provisioning_request",
                "manifest": {"kind": "ProvisioningRequest", "spec": {"podSets": []}},
                "pair_id": "pair-1",
                "target": {"region": "europe-west4", "zone": "europe-west4-b"},
                "machine_spec": {"acceleratorType": "tpu-v5e", "chipsPerNode": 4},
            }
        )
        self.assertEqual(reply, "attached provisioning_request artifact to t_1")
        (row,) = self.rows("task_artifacts")
        self.assertEqual(json.loads(row["target_json"])["zone"], "europe-west4-b")
        self.assertEqual(json.loads(row["machine_spec_json"])["acceleratorType"], "tpu-v5e")

    def test_a_provisioning_request_must_name_its_shape(self) -> None:
        # Its manifest names a podTemplateRef, not the hardware.
        reply = self.attach({"type": "provisioning_request", "manifest": {"kind": "ProvisioningRequest"}})
        self.assertIn("must carry machine_spec", reply)

    def test_rejects_manifest_text_that_is_not_an_object(self) -> None:
        reply = self.attach({"type": "computeclass", "manifest": "kind: ComputeClass"})
        self.assertIn("manifest must be an object", reply)

    def test_rejects_manifest_whose_sections_collapsed_to_scalars(self) -> None:
        # Observed live: {"metadata": 1, "spec": 1} shadowed the good copy.
        reply = self.attach({"type": "computeclass", "manifest": {"metadata": 1, "spec": 1}})
        self.assertIn("manifest.metadata must be an object", reply)

    def test_an_older_board_is_migrated_in_place(self) -> None:
        # kanban.db persists on the profile volume, so task_artifacts may
        # predate target/machine_spec; the first write adds the columns.
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE task_artifacts (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, type TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}', pair_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                """
            )
        reply = self.attach(
            {"type": "local_queue", "manifest": {"kind": "LocalQueue"}, "target": {"zone": "z"}}
        )
        self.assertEqual(reply, "attached local_queue artifact to t_1")
        (row,) = self.rows("task_artifacts")
        self.assertEqual(json.loads(row["target_json"]), {"zone": "z"})


class ProjectionRoundTripTest(RecorderFixture):
    """The recorder's rows must project through the admin console's read script.

    This is the seam #804 is about: the worker writes with these tools, the
    portal reads with admin_console's embedded script, and the CUJ evaluators
    score exactly what comes out the far end.
    """

    def project(self) -> dict:
        sys.path.insert(0, str(REPO_ROOT))
        try:
            from admin_console.agent_runtime import _READ_SCRIPT
        finally:
            sys.path.pop(0)
        script = _READ_SCRIPT.replace('Path("/opt/data/kanban.db")', f"Path({str(self.db_path)!r})")
        completed = subprocess.run(
            [sys.executable, "-c", script, "tasks", "portal_session", "10"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)["tasks"][0]

    def test_records_reach_the_portal_projection_with_provenance_under_details(self) -> None:
        self.record(
            {
                "type": "advice_service_workload_obtainability_planning",
                "api_method": "compute.beta.AdviceService.CalendarMode",
                "request": PLANNING_REQUEST,
                "analysis": {"windowFound": True},
                "execution_ref": "exec-7",
            }
        )
        self.attach(
            {
                "type": "provisioning_request",
                "manifest": {"kind": "ProvisioningRequest"},
                "pair_id": "pair-w1",
                "target": {"region": "europe-west4", "zone": "europe-west4-b"},
                "machine_spec": {"acceleratorType": "tpu-v5e", "chipsPerNode": 4},
            }
        )
        row = self.project()
        self.assertEqual(row["result"], "Full report.")
        self.assertEqual(
            row["evidence"],
            [
                {
                    "type": "advice_service_workload_obtainability_planning",
                    "status": "completed",
                    "details": {
                        "apiMethod": "compute.beta.AdviceService.CalendarMode",
                        "region": "europe-west4",
                        "nodeCount": 64,
                        "request": PLANNING_REQUEST,
                        "analysis": {"windowFound": True},
                        "executionRef": "exec-7",
                    },
                }
            ],
        )
        self.assertEqual(
            row["artifacts"],
            [
                {
                    "type": "provisioning_request",
                    "manifest": {"kind": "ProvisioningRequest"},
                    "pairId": "pair-w1",
                    "target": {"region": "europe-west4", "zone": "europe-west4-b"},
                    "machineSpec": {"acceleratorType": "tpu-v5e", "chipsPerNode": 4},
                }
            ],
        )


class RegistrationTest(unittest.TestCase):
    def test_register_wires_both_tools_behind_the_given_gate(self) -> None:
        calls = []

        class Registry:
            def register(self, **kwargs):
                calls.append(kwargs)

        gate = object()
        ket.register(Registry(), gate, tool_error)
        self.assertEqual([call["name"] for call in calls], ["record_evidence", "attach_artifact"])
        for call in calls:
            self.assertEqual(call["toolset"], "kanban")
            self.assertIs(call["check_fn"], gate)
            self.assertIn("parameters", call["schema"])


if __name__ == "__main__":
    unittest.main()
