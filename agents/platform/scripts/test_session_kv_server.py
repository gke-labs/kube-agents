import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

# Create a temporary SQLite database for testing and set it in the environment
# BEFORE importing session_kv_server to prevent it from creating the default production DB path.
db_fd, temp_db_path = tempfile.mkstemp()
os.close(db_fd)
os.environ["SESSION_KV_DB_PATH"] = temp_db_path

# Add the directory containing session_kv_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

import session_kv_server
from session_kv_server import clean_workload_name, clean_reason_label, clean_event_message, get_severity_details

class TestSessionKvServerUtils(unittest.TestCase):

    def test_clean_workload_name_pod_replicas(self):
        # Deployment pod replicas (hash + random suffix)
        self.assertEqual(clean_workload_name("pod", "billing-processor-6cfdb6b98b-zwv24"), "billing-processor")
        # StatefulSet / replica suffix
        self.assertEqual(clean_workload_name("pod", "redis-master-0"), "redis-master-0")
        self.assertEqual(clean_workload_name("pod", "billing-pod-zwv24"), "billing-pod")
        # Non-pod resource names should not be modified
        self.assertEqual(clean_workload_name("service", "billing-processor-service"), "billing-processor-service")

    def test_clean_reason_label_camel_case(self):
        self.assertEqual(clean_reason_label("FailedToDrainNode"), "Failed to drain node")
        self.assertEqual(clean_reason_label("PodEviction"), "Pod eviction")
        self.assertEqual(clean_reason_label("FailedMount"), "Failed mount")
        self.assertEqual(clean_reason_label("Unhealthy"), "Unhealthy")

    def test_clean_event_message_pdb(self):
        # PDB Eviction warning simplification
        msg = "cannot be evicted: would violate PDB default/billing-processor-pdb"
        self.assertEqual(clean_event_message(msg), "Eviction would violate PDB billing-processor-pdb")
        
        # General messages remain unchanged
        msg_general = "MountVolume.SetUp failed for volume \"config\""
        self.assertEqual(clean_event_message(msg_general), msg_general)

    def test_get_severity_details(self):
        # Blocker warnings -> Critical
        self.assertEqual(get_severity_details("Warning", "FailedMount"), ("🔴", "Critical"))
        self.assertEqual(get_severity_details("Warning", "FailedScheduling"), ("🔴", "Critical"))
        self.assertEqual(get_severity_details("Warning", "FailedToDrainNode"), ("🔴", "Critical"))
        
        # Normal warnings -> Warning
        self.assertEqual(get_severity_details("Warning", "Unhealthy"), ("🟡", "Warning"))
        
        # Normal events -> Info
        self.assertEqual(get_severity_details("Normal", "Scheduled"), ("🔵", "Info"))


class TestSessionKvServerApi(unittest.TestCase):

    def setUp(self):
        # Set up fastapi TestClient
        from fastapi.testclient import TestClient
        self.client = TestClient(session_kv_server.app)

        # Mock register_gateway_routing to avoid filesystem changes in tests
        self.patcher_routing = patch("session_kv_server.register_gateway_routing")
        self.mock_register_routing = self.patcher_routing.start()

    def tearDown(self):
        self.patcher_routing.stop()

    def test_create_session(self):
        response = self.client.post("/sessions")
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("sessionID", data)
        self.assertTrue(data["sessionID"].startswith("k8s-evt-"))

    def test_get_session_metadata_not_found(self):
        response = self.client.get("/v1/sessions/non-existent-session/metadata")
        self.assertEqual(response.status_code, 404)

    def test_create_and_get_session_metadata(self):
        # Create session
        create_resp = self.client.post("/sessions")
        session_id = create_resp.json()["sessionID"]

        # Get metadata
        meta_resp = self.client.get(f"/v1/sessions/{session_id}/metadata")
        self.assertEqual(meta_resp.status_code, 200)
        data = meta_resp.json()
        self.assertEqual(data.get("platform"), "k8s-watcher")
        self.assertIn("created_at", data)


class TestRegisterGatewayRouting(unittest.TestCase):
    """Exercises the hardened gateway_routing writer against a real temp DB.

    hermes owns gateway_routing in production; these tests reproduce its shape
    (extra hermes-owned columns + a UNIQUE session_key) and assert we never
    clobber foreign rows or unknown columns.
    """

    def setUp(self):
        import sqlite3
        self.db_fd, self.gateway_db = tempfile.mkstemp(suffix="_state.db")
        os.close(self.db_fd)
        with sqlite3.connect(self.gateway_db) as conn:
            # Mirror hermes: session_key is UNIQUE, plus a column we don't own.
            conn.execute(
                """
                CREATE TABLE gateway_routing (
                    scope TEXT,
                    session_key TEXT UNIQUE,
                    entry_json TEXT,
                    updated_at REAL,
                    hermes_owned_col TEXT
                )
                """
            )
        self.patcher = patch("session_kv_server.STATE_DB_PATH", self.gateway_db)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.gateway_db):
            os.remove(self.gateway_db)

    def _rows(self):
        import sqlite3
        with sqlite3.connect(self.gateway_db) as conn:
            return conn.execute(
                "SELECT session_key, entry_json, hermes_owned_col FROM gateway_routing"
            ).fetchall()

    def test_inserts_new_routing_row(self):
        session_kv_server.register_gateway_routing(
            "k8s-evt-abc123", "slack", "C123", "1700000000.0001"
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        session_key, entry_json, _ = rows[0]
        self.assertEqual(session_key, "agent:main:slack:group:C123:1700000000.0001")
        self.assertEqual(json.loads(entry_json)["session_id"], "k8s-evt-abc123")

    def test_does_not_clobber_foreign_row(self):
        """A row owned by hermes (human session) must never be overwritten."""
        import sqlite3
        session_key = "agent:main:slack:group:C123:1700000000.0001"
        with sqlite3.connect(self.gateway_db) as conn:
            conn.execute(
                "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at, hermes_owned_col) "
                "VALUES (?, ?, ?, ?, ?)",
                ("/opt/data/sessions", session_key,
                 json.dumps({"session_id": "human-user-42"}), 1.0, "DO_NOT_TOUCH"),
            )
        # Our thread happens to collide with the human's session_key.
        session_kv_server.register_gateway_routing(
            "k8s-evt-abc123", "slack", "C123", "1700000000.0001"
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        # Untouched: still the human's session and hermes's column value.
        self.assertEqual(json.loads(rows[0][1])["session_id"], "human-user-42")
        self.assertEqual(rows[0][2], "DO_NOT_TOUCH")

    def test_updates_own_row_and_preserves_unknown_columns(self):
        """ON CONFLICT DO UPDATE refreshes our columns without wiping hermes's."""
        import sqlite3
        session_key = "agent:main:slack:group:C123:1700000000.0001"
        with sqlite3.connect(self.gateway_db) as conn:
            conn.execute(
                "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at, hermes_owned_col) "
                "VALUES (?, ?, ?, ?, ?)",
                ("/opt/data/sessions", session_key,
                 json.dumps({"session_id": "k8s-evt-OLD"}), 1.0, "hermes_value"),
            )
        session_kv_server.register_gateway_routing(
            "k8s-evt-NEW", "slack", "C123", "1700000000.0001"
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        # Our column updated...
        self.assertEqual(json.loads(rows[0][1])["session_id"], "k8s-evt-NEW")
        # ...hermes's column preserved (not nulled by a delete+reinsert).
        self.assertEqual(rows[0][2], "hermes_value")

    def test_template_inherits_unknown_columns_on_insert(self):
        """New rows copy hermes-owned columns from a real existing row."""
        import sqlite3
        with sqlite3.connect(self.gateway_db) as conn:
            conn.execute(
                "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at, hermes_owned_col) "
                "VALUES (?, ?, ?, ?, ?)",
                ("/opt/data/sessions", "agent:main:slack:group:Cother:9.9",
                 json.dumps({"session_id": "human-user-1"}), 1.0, "TEMPLATE_VAL"),
            )
        session_kv_server.register_gateway_routing(
            "k8s-evt-abc123", "slack", "C123", "1700000000.0001"
        )
        import sqlite3 as s
        with s.connect(self.gateway_db) as conn:
            val = conn.execute(
                "SELECT hermes_owned_col FROM gateway_routing WHERE session_key = ?",
                ("agent:main:slack:group:C123:1700000000.0001",),
            ).fetchone()[0]
        self.assertEqual(val, "TEMPLATE_VAL")

    def test_refuses_on_schema_drift(self):
        """If an owned column disappears, refuse rather than corrupt state."""
        import sqlite3
        with sqlite3.connect(self.gateway_db) as conn:
            conn.execute("DROP TABLE gateway_routing")
            conn.execute(
                "CREATE TABLE gateway_routing (session_key TEXT UNIQUE, entry_json TEXT)"
            )
        session_kv_server.register_gateway_routing(
            "k8s-evt-abc123", "slack", "C123", "1700000000.0001"
        )
        self.assertEqual(len(self._rows_no_scope()), 0)

    def _rows_no_scope(self):
        import sqlite3
        with sqlite3.connect(self.gateway_db) as conn:
            return conn.execute("SELECT session_key FROM gateway_routing").fetchall()


if __name__ == "__main__":
    # Clean up temp database file on exit
    try:
        unittest.main()
    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
