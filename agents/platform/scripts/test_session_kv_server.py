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

    def tearDown(self):
        pass

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

    def test_store_and_get_incident(self):
        # Store incident
        incident_data = {
            "chat_id": "test-chat",
            "thread_id": "test-thread",
            "report": "This is a test report with Option A and Option B"
        }
        resp = self.client.post("/v1/incidents", json=incident_data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "stored"})

        # Get incident
        get_resp = self.client.get("/v1/incidents/by-thread?chat_id=test-chat&thread_id=test-thread")
        self.assertEqual(get_resp.status_code, 200)
        data = get_resp.json()
        self.assertEqual(data["chat_id"], "test-chat")
        self.assertEqual(data["thread_id"], "test-thread")
        self.assertEqual(data["report"], "This is a test report with Option A and Option B")

    def test_get_incident_not_found(self):
        get_resp = self.client.get("/v1/incidents/by-thread?chat_id=missing&thread_id=missing")
        self.assertEqual(get_resp.status_code, 404)

    def test_database_cleanup_ttl(self):
        import sqlite3
        from datetime import datetime, timedelta
        
        # 1. Insert stale records manually (older than 14 days)
        old_time = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                # Insert old session metadata
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata, updated_at) VALUES (?, ?, ?)",
                    ("old-session", '{"platform": "k8s-watcher"}', old_time)
                )
                # Insert old incident
                conn.execute(
                    "INSERT INTO incidents (chat_id, thread_id, report, created_at) VALUES (?, ?, ?, ?)",
                    ("old-chat", "old-thread", "old-report", old_time)
                )
                
                # Insert fresh incident manually so we verify it is NOT deleted
                conn.execute(
                    "INSERT INTO incidents (chat_id, thread_id, report) VALUES (?, ?, ?)",
                    ("fresh-chat", "fresh-thread", "fresh-report")
                )

        # 2. Trigger endpoint write which calls cleanup_old_records
        resp = self.client.post("/sessions")
        self.assertEqual(resp.status_code, 201)

        # 3. Assert old records are deleted and fresh records are kept
        with sqlite3.connect(temp_db_path) as conn:
            # Check old session metadata
            res = conn.execute("SELECT session_id FROM session_metadata WHERE session_id = ?", ("old-session",)).fetchone()
            self.assertIsNone(res)
            
            # Check old incident
            res = conn.execute("SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ?", ("old-chat", "old-thread")).fetchone()
            self.assertIsNone(res)

            # Check fresh incident
            res = conn.execute("SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ?", ("fresh-chat", "fresh-thread")).fetchone()
            self.assertIsNotNone(res)
            self.assertEqual(res[0], "fresh-report")






class TestSessionKvServerQueryBuilding(unittest.TestCase):

    @patch.dict(os.environ, {"GCP_PROJECT_ID": "test-project-id"})
    def test_build_agent_query_with_project_id(self):
        payload = {
            "reason": "FailedMount",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        query = session_kv_server._build_agent_query("test-session", payload)
        self.assertIn("project=test-project-id", query)
        self.assertNotIn("jayantid-gkedemos", query)

    @patch.dict(os.environ, {"GCP_PROJECT": "test-project-legacy"})
    def test_build_agent_query_with_legacy_project(self):
        payload = {
            "reason": "FailedMount",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        with patch.dict(os.environ, {"GCP_PROJECT_ID": ""}):
            query = session_kv_server._build_agent_query("test-session", payload)
            self.assertIn("project=test-project-legacy", query)

    def test_build_agent_query_no_project(self):
        payload = {
            "reason": "FailedMount",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        with patch.dict(os.environ, {"GCP_PROJECT_ID": "", "GCP_PROJECT": ""}):
            query = session_kv_server._build_agent_query("test-session", payload)
            self.assertIn("project=", query)


if __name__ == "__main__":
    # Clean up temp database file on exit
    try:
        unittest.main()
    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
