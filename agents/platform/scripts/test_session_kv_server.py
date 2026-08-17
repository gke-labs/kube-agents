import json
import os
import sys
import tempfile
import time
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

# Every route that reads or writes stored data now requires this. /healthz is
# the one exception and has its own test below.
API_KEY = "test-session-kv-key"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

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
        
        # PodDisruptionBudget is abbreviated, and the namespace is optional
        msg_long = "cannot be evicted: would violate PodDisruptionBudget billing-processor-pdb"
        self.assertEqual(clean_event_message(msg_long), "Eviction would violate PDB billing-processor-pdb")

        # General messages remain unchanged
        msg_general = "MountVolume.SetUp failed for volume \"config\""
        self.assertEqual(clean_event_message(msg_general), msg_general)

    def test_clean_event_message_pathological_whitespace(self):
        # A long whitespace run with no PDB name must not trigger quadratic
        # backtracking (CodeQL py/polynomial-redos).
        msg = "cannot be evicted:would violate PDB " + " " * 60000
        start = time.monotonic()
        self.assertEqual(clean_event_message(msg), msg)
        self.assertLess(time.monotonic() - start, 1.0)

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
        # Set up fastapi TestClient. The key goes on the client rather than on
        # each call so these tests stay about behaviour; the auth boundary
        # itself is pinned by TestSessionKvServerAuth below.
        from fastapi.testclient import TestClient
        os.environ["SESSION_KV_API_KEY"] = API_KEY
        self.client = TestClient(session_kv_server.app, headers=AUTH_HEADERS)

    def tearDown(self):
        os.environ.pop("SESSION_KV_API_KEY", None)

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






class TestSessionKvServerAuth(unittest.TestCase):
    """The auth boundary, route by route.

    Enumerated rather than spot-checked: the failure this guards against is a
    new route being added without the dependency, and a test that only exercises
    two of six routes reads as coverage while providing none.
    """

    # (method, path, json body or None)
    PROTECTED_ROUTES = (
        ("POST", "/sessions", None),
        ("POST", "/sessions/sess-1/inject", {"message": "{}"}),
        ("GET", "/v1/sessions", None),
        ("GET", "/v1/sessions/sess-1/metadata", None),
        ("POST", "/v1/incidents", {"chat_id": "c", "thread_id": "t", "report": "r"}),
        ("GET", "/v1/incidents/by-thread?chat_id=c&thread_id=t", None),
        ("GET", "/v1/alert-quota", None),
    )

    def setUp(self):
        from fastapi.testclient import TestClient
        os.environ["SESSION_KV_API_KEY"] = API_KEY
        self.client = TestClient(session_kv_server.app)
        # TestClient runs BackgroundTasks inline, and /inject's task shells out
        # to `hermes send` and dials the gateway. This suite is about who is let
        # through the door, not what happens after.
        self._trigger = patch.object(session_kv_server, "trigger_agent_troubleshooter")
        self._trigger.start()

    def tearDown(self):
        self._trigger.stop()
        os.environ.pop("SESSION_KV_API_KEY", None)

    def _call(self, method, path, body, headers=None):
        if method == "GET":
            return self.client.get(path, headers=headers or {})
        return self.client.post(path, json=body, headers=headers or {})

    def test_declared_routes_are_all_covered(self):
        """Fails when a route is added without deciding whether it needs a key."""
        declared = {
            (method, route.path)
            for route in session_kv_server.app.routes
            for method in getattr(route, "methods", set()) or set()
            if method in ("GET", "POST")
        }
        covered = {
            (method, path.split("?")[0].replace("sess-1", "{session_id}"))
            for method, path, _ in self.PROTECTED_ROUTES
        } | {("GET", "/healthz")}
        self.assertEqual(declared, covered)

    def test_healthz_needs_no_key(self):
        os.environ.pop("SESSION_KV_API_KEY", None)
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_protected_routes_reject_a_missing_key(self):
        for method, path, body in self.PROTECTED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(self._call(method, path, body).status_code, 401)

    def test_protected_routes_reject_a_wrong_key(self):
        headers = {"Authorization": "Bearer not-the-key"}
        for method, path, body in self.PROTECTED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(self._call(method, path, body, headers).status_code, 401)

    def test_protected_routes_accept_the_configured_key(self):
        for method, path, body in self.PROTECTED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                status = self._call(method, path, body, AUTH_HEADERS).status_code
                self.assertNotIn(status, (401, 403, 503))

    def test_x_api_key_header_is_accepted(self):
        response = self.client.get("/v1/sessions", headers={"X-Api-Key": API_KEY})
        self.assertEqual(response.status_code, 200)

    def test_a_non_ascii_key_is_rejected_rather_than_crashing(self):
        """A 0x80–0xFF byte in the header must be a 401, not a 500.

        Starlette decodes header values as latin-1, so such a byte reaches the
        dependency as a non-ASCII `str`, and `hmac.compare_digest` raises
        TypeError on those rather than returning False — escaping as a 500 with
        a traceback. The dependency is called directly because the test client
        cannot deliver the header: httpx encodes header values as ASCII and
        rejects the request before the server sees it.
        """
        with self.assertRaises(session_kv_server.HTTPException) as caught:
            session_kv_server.verify_api_key(authorization="", x_api_key="café")
        self.assertEqual(caught.exception.status_code, 401)

        with self.assertRaises(session_kv_server.HTTPException) as caught:
            session_kv_server.verify_api_key(authorization="Bearer café", x_api_key="")
        self.assertEqual(caught.exception.status_code, 401)

    def test_unconfigured_key_fails_closed(self):
        """A deployment that never received the Secret must not serve the data."""
        os.environ.pop("SESSION_KV_API_KEY", None)
        response = self.client.get("/v1/sessions", headers=AUTH_HEADERS)
        self.assertEqual(response.status_code, 503)

    def test_schema_is_not_published(self):
        for path in ("/openapi.json", "/docs", "/redoc"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class TestPlaintextIdentityPurge(unittest.TestCase):
    """Rows written before pseudonymisation are stripped, not deleted."""

    def setUp(self):
        import sqlite3
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute("DELETE FROM session_metadata")

    def _write(self, session_id, metadata):
        import sqlite3
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (session_id, json.dumps(metadata)),
            )

    def _read(self, session_id):
        import sqlite3
        with sqlite3.connect(temp_db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM session_metadata WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def test_plaintext_email_is_removed_and_the_row_survives(self):
        self._write(
            "legacy-1",
            {
                "platform": "google_chat",
                "user_email": "user@example.com",
                "chat_id": "spaces/AAA",
                "thread_id": "spaces/AAA/threads/BBB",
            },
        )
        session_kv_server.init_db()

        row = self._read("legacy-1")
        self.assertIsNotNone(row, "the row must survive so threaded replies keep routing")
        self.assertNotIn("user_email", row)
        self.assertEqual(row["chat_id"], "spaces/AAA")
        self.assertEqual(row["thread_id"], "spaces/AAA/threads/BBB")

    def test_address_shaped_user_id_is_removed(self):
        self._write("legacy-2", {"platform": "google_chat", "user_id": "user@example.com"})
        session_kv_server.init_db()
        self.assertNotIn("user_id", self._read("legacy-2"))

    def test_opaque_user_id_is_left_alone(self):
        """A Slack member id is already pseudonymous and must not be dropped."""
        self._write("slack-1", {"platform": "slack", "user_id": "U012ABCDEF"})
        session_kv_server.init_db()
        self.assertEqual(self._read("slack-1")["user_id"], "U012ABCDEF")

    def test_hashed_rows_are_untouched(self):
        self._write("modern-1", {"platform": "google_chat", "user_email_hash": "deadbeef"})
        session_kv_server.init_db()
        self.assertEqual(self._read("modern-1")["user_email_hash"], "deadbeef")


class TestAlertDailyQuota(unittest.TestCase):
    """The per-severity daily ceiling enforced in /sessions/{id}/inject."""

    def setUp(self):
        import sqlite3
        from fastapi.testclient import TestClient

        # Every route these tests touch is behind verify_api_key, including
        # /v1/alert-quota. The key goes on the client rather than on each call
        # so these tests stay about the ceiling; the auth boundary itself is
        # pinned by TestSessionKvServerAuth above.
        os.environ["SESSION_KV_API_KEY"] = API_KEY
        self.client = TestClient(session_kv_server.app, headers=AUTH_HEADERS)
        # The temp database is shared by every test in this file, so today's
        # spent budget has to be cleared or these tests order-depend on each
        # other.
        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute("DELETE FROM alert_quota")

    def tearDown(self):
        os.environ.pop("SESSION_KV_API_KEY", None)

    def _inject(self, reason="Unhealthy", session_id="k8s-evt-quota"):
        payload = {
            "reason": reason,
            "namespace": "ns",
            "kind_of_object": "Pod",
            "name": "billing-pod",
            "message": "some message",
            "type": "Warning",
        }
        return self.client.post(f"/sessions/{session_id}/inject", json={"message": json.dumps(payload)})

    def test_alert_daily_limit_parsing(self):
        parse = session_kv_server._alert_daily_limit
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_LIMIT", None)
            # Unset falls back to the default rather than to "uncapped".
            self.assertEqual(parse("X_LIMIT", 10), 10)
        with patch.dict(os.environ, {"X_LIMIT": "3"}):
            self.assertEqual(parse("X_LIMIT", 10), 3)
        with patch.dict(os.environ, {"X_LIMIT": "0"}):
            # An explicit 0 is how the cap is turned off.
            self.assertEqual(parse("X_LIMIT", 10), 0)
        with patch.dict(os.environ, {"X_LIMIT": "-5"}):
            # Negative is not a ceiling; treated as "off", not as "block all".
            self.assertEqual(parse("X_LIMIT", 10), 0)
        with patch.dict(os.environ, {"X_LIMIT": "ten"}):
            # Garbage must not silently disable the cap or block everything.
            self.assertEqual(parse("X_LIMIT", 10), 10)

    def test_zero_limit_never_suppresses(self):
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 0}):
            for _ in range(20):
                allowed, suppressed = session_kv_server._claim_alert_quota("Warning")
                self.assertTrue(allowed)
                self.assertEqual(suppressed, 0)

    def test_info_severity_is_capped(self):
        # Info is a real arrival, not a theoretical one: nothing on the path
        # from the kubelet filters on Event.Type, so an allowlisted reason
        # emitted as `type: Normal` — BackOff during image-pull back-off, say —
        # is classified Info here. It gets a ceiling like everything else.
        self.assertIn("Info", session_kv_server.ALERT_DAILY_LIMITS)
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Info": 1}):
            allowed, _ = session_kv_server._claim_alert_quota("Info")
            self.assertTrue(allowed)

            allowed, suppressed = session_kv_server._claim_alert_quota("Info")
            self.assertFalse(allowed, "Info must not bypass the ceiling")
            self.assertEqual(suppressed, 1)

    def test_unknown_severity_is_allowed(self):
        # The .get default is now reachable only by a string
        # get_severity_details cannot return. Such a severity must pass through
        # rather than be read as a zero budget and blocked outright.
        self.assertNotIn("Nonsense", session_kv_server.ALERT_DAILY_LIMITS)
        allowed, _ = session_kv_server._claim_alert_quota("Nonsense")
        self.assertTrue(allowed)

    def test_claim_allows_exactly_the_limit_then_suppresses(self):
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 3}):
            for i in range(3):
                allowed, suppressed = session_kv_server._claim_alert_quota("Warning")
                self.assertTrue(allowed, f"alert {i + 1} of 3 should be within budget")
                self.assertEqual(suppressed, 0)

            allowed, suppressed = session_kv_server._claim_alert_quota("Warning")
            self.assertFalse(allowed)
            self.assertEqual(suppressed, 1)

            allowed, suppressed = session_kv_server._claim_alert_quota("Warning")
            self.assertFalse(allowed)
            self.assertEqual(suppressed, 2)

    def test_severities_have_independent_budgets(self):
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 1, "Critical": 2}):
            self.assertTrue(session_kv_server._claim_alert_quota("Warning")[0])
            self.assertFalse(session_kv_server._claim_alert_quota("Warning")[0])
            # Exhausting warnings must not touch the critical budget.
            self.assertTrue(session_kv_server._claim_alert_quota("Critical")[0])
            self.assertTrue(session_kv_server._claim_alert_quota("Critical")[0])
            self.assertFalse(session_kv_server._claim_alert_quota("Critical")[0])

    def test_yesterdays_spend_does_not_consume_today(self):
        import sqlite3

        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO alert_quota (day, severity, sent, suppressed) VALUES ('2020-01-01', 'Warning', 99, 42)"
                )
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 2}):
            self.assertTrue(session_kv_server._claim_alert_quota("Warning")[0])

    def test_claim_fails_open_when_the_database_is_unavailable(self):
        import sqlite3

        # A cap must never be the reason an incident goes unreported.
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 1}):
            with patch.object(session_kv_server.sqlite3, "connect", side_effect=sqlite3.OperationalError("locked")):
                allowed, suppressed = session_kv_server._claim_alert_quota("Warning")
        self.assertTrue(allowed)
        self.assertEqual(suppressed, 0)

    def test_inject_suppresses_past_the_limit_and_does_not_trigger_the_agent(self):
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 2}):
            with patch.object(session_kv_server, "trigger_agent_troubleshooter") as trigger:
                self.assertEqual(self._inject().json()["status"], "injected")
                self.assertEqual(self._inject().json()["status"], "injected")

                resp = self._inject()
                # 200, not an error: a failure response would leave the
                # watcher's dedup entry unbound and cost us a re-report.
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["status"], "suppressed")
                self.assertEqual(body["severity"], "Warning")
                self.assertEqual(body["suppressed_today"], "1")

                self.assertEqual(trigger.call_count, 2, "the suppressed alert must not reach the agent")

    def test_suppression_posts_nothing_to_chat(self):
        # Announcing the ceiling would spend a message to say no more messages
        # are coming. Nothing at all may be sent once the budget is spent.
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 1}):
            with patch.object(session_kv_server, "trigger_agent_troubleshooter"):
                with patch.object(session_kv_server, "_post_initial_alert") as post:
                    self._inject()
                    self._inject()
                    self._inject()
        post.assert_not_called()

    def test_alert_quota_endpoint_reports_spend_and_drops(self):
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 1, "Critical": 5}):
            with patch.object(session_kv_server, "trigger_agent_troubleshooter"):
                self._inject()
                self._inject()

            resp = self.client.get("/v1/alert-quota")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["severities"]["Warning"], {"limit": 1, "sent": 1, "suppressed": 1})
            # A capped severity with no traffic still reports, so a missing key
            # means "uncapped" rather than "quiet".
            self.assertEqual(data["severities"]["Critical"], {"limit": 5, "sent": 0, "suppressed": 0})

    def test_alert_quota_endpoint_omits_uncapped_severities(self):
        with patch.dict(session_kv_server.ALERT_DAILY_LIMITS, {"Warning": 0, "Critical": 5}):
            data = self.client.get("/v1/alert-quota").json()
            self.assertNotIn("Warning", data["severities"])
            self.assertIn("Critical", data["severities"])

    def test_old_quota_rows_are_cleaned_up(self):
        import sqlite3
        from datetime import datetime, timedelta

        stale_day = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
        fresh_day = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO alert_quota (day, severity, sent, suppressed) VALUES (?, 'Warning', 1, 1)",
                    (stale_day,),
                )
                conn.execute(
                    "INSERT INTO alert_quota (day, severity, sent, suppressed) VALUES (?, 'Warning', 1, 1)",
                    (fresh_day,),
                )

        # Any write endpoint runs cleanup_old_records.
        self.assertEqual(self.client.post("/sessions").status_code, 201)

        with sqlite3.connect(temp_db_path) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM alert_quota WHERE day = ?", (stale_day,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM alert_quota WHERE day = ?", (fresh_day,)).fetchone())


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
            # With no project configured the console links carry no project
            # qualifier at all — `?project=` / `;project=` are omitted rather
            # than emitted empty, which would send the reader to a dead link.
            self.assertNotIn("project=", query)

    @patch.dict(os.environ, {"GKE_CLUSTER_NAME": "platform-agent-host"})
    def test_build_agent_query_names_the_events_cluster(self):
        # The event came from a different cluster than the one this agent runs
        # on; the prompt must name the event's cluster, not the host's.
        payload = {
            "reason": "OOMKilled",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message",
            "cluster": "prod-us-central1"
        }
        query = session_kv_server._build_agent_query("test-session", payload)
        self.assertIn("prod-us-central1", query)
        self.assertNotIn("platform-agent-host", query)

    @patch.dict(os.environ, {"GKE_CLUSTER_NAME": "platform-agent-host"})
    def test_build_agent_query_falls_back_to_host_cluster(self):
        # No cluster on the payload (non-watcher caller, or a watcher started
        # without --cluster-name): fall back to the host cluster env var.
        payload = {
            "reason": "OOMKilled",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        query = session_kv_server._build_agent_query("test-session", payload)
        self.assertIn("platform-agent-host", query)

    def test_call_to_action_names_options_instead_of_a_placeholder(self):
        # The call-to-action is copied verbatim into the chat message, so a
        # `<letter>` there reaches the responder as an unfilled placeholder
        # rather than a choice they can act on. `<letter>` is still correct in
        # the instruction prose above the template, which the agent reads but
        # never echoes -- so pin the template line, not the whole query.
        payload = {
            "reason": "OOMKilled",
            "namespace": "test-ns",
            "kind_of_object": "Pod",
            "name": "test-pod",
            "message": "some message"
        }
        query = session_kv_server._build_agent_query("test-session", payload)
        cta = next(line for line in query.splitlines() if line.startswith("👉"))
        self.assertNotIn("<letter>", cta)
        self.assertIn("apply Option A", cta)
        self.assertIn("apply Option B", cta)


class FakeResponse:
    """The two attributes _post_gateway touches on a urlopen result."""

    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    import urllib.error

    return urllib.error.HTTPError(
        url="http://gw/api/sessions", code=code, msg="nope", hdrs=None, fp=None
    )


class Recorder:
    """Stands in for urlopen, recording the URL and body of every request."""

    def __init__(self, *results):
        # One entry per call: an int status, or an exception to raise.
        self.results = list(results)
        self.urls = []
        self.bodies = []
        self.timeouts = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        self.timeouts.append(timeout)
        result = self.results.pop(0) if self.results else 200
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


GATEWAY_HEADERS = {"Content-Type": "application/json"}


class TestTriageProfileIsNamed(unittest.TestCase):
    """The triage turn has to be routed, not left to whatever `default` means.

    The gateway picks a profile from the URL path prefix, so an unprefixed
    request is served by `default` — the Chat Agent. No `platform_control`, so
    no `send_notification`, so no way for the RCA to reach chat or the
    incidents table. The turn still does the diagnostic work and still ends
    clean, which is what made the failure so hard to see (#630).
    """

    def test_session_creation_asks_for_the_profile_prefix(self):
        rec = Recorder(200)
        with patch("urllib.request.urlopen", rec):
            self.assertTrue(
                session_kv_server._create_gateway_session("http://gw", "k8s-evt-1", GATEWAY_HEADERS)
            )
        self.assertEqual(rec.urls, ["http://gw/p/platform/api/sessions"])

    def test_agent_turn_asks_for_the_profile_prefix(self):
        rec = Recorder(200)
        with patch("urllib.request.urlopen", rec):
            session_kv_server._start_agent_turn(
                "http://gw", "k8s-evt-1", "diagnose this", GATEWAY_HEADERS
            )
        self.assertEqual(rec.urls, ["http://gw/p/platform/api/sessions/k8s-evt-1/chat"])
        self.assertEqual(rec.bodies[0]["message"], "diagnose this")

    def test_the_profile_default_is_the_platform_agent(self):
        self.assertEqual(session_kv_server.PLATFORM_API_PROFILE, "platform")

    def test_the_prefix_is_dropped_when_the_profile_is_default(self):
        """Asking `default` for `/p/default/...` would be a needless 404 round trip."""
        for value in ("default", ""):
            with self.subTest(profile=value):
                with patch.object(session_kv_server, "PLATFORM_API_PROFILE", value):
                    self.assertEqual(
                        session_kv_server._profile_urls("http://gw", "/api/sessions"),
                        ["http://gw/api/sessions"],
                    )

    def test_the_turn_is_given_the_whole_reasoning_loop(self):
        """The timeout bounds an agent turn, not an HTTP round trip (#630)."""
        self.assertGreaterEqual(session_kv_server.TURN_TIMEOUT_SECONDS, 1800)
        rec = Recorder(200)
        with patch("urllib.request.urlopen", rec):
            session_kv_server._start_agent_turn(
                "http://gw", "k8s-evt-1", "diagnose this", GATEWAY_HEADERS
            )
        self.assertEqual(rec.timeouts, [session_kv_server.TURN_TIMEOUT_SECONDS])


class TestTriageProfileFallback(unittest.TestCase):
    """A gateway that does not serve the prefix must not lose triage over it."""

    def test_an_unserved_prefix_falls_back_to_the_plain_path(self):
        rec = Recorder(http_error(404), 200)
        with patch("urllib.request.urlopen", rec):
            status = session_kv_server._post_gateway(
                "http://gw", "/api/sessions", {"a": 1}, GATEWAY_HEADERS, 10.0
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            rec.urls, ["http://gw/p/platform/api/sessions", "http://gw/api/sessions"]
        )
        # The retry must keep everything else about the request.
        self.assertEqual(rec.bodies[1], {"a": 1})

    def test_other_failures_are_not_retried(self):
        """A repeat of a 500 is a second outage; on /chat it is a second LLM turn."""
        rec = Recorder(http_error(500))
        with patch("urllib.request.urlopen", rec):
            with self.assertRaises(Exception):
                session_kv_server._post_gateway(
                    "http://gw", "/api/sessions", {"a": 1}, GATEWAY_HEADERS, 10.0
                )
        self.assertEqual(len(rec.urls), 1)

    def test_a_second_404_is_not_retried_again(self):
        """One fallback, not a loop: the plain path answering 404 is a real answer."""
        rec = Recorder(http_error(404), http_error(404))
        with patch("urllib.request.urlopen", rec):
            with self.assertRaises(Exception):
                session_kv_server._post_gateway(
                    "http://gw", "/api/sessions", {"a": 1}, GATEWAY_HEADERS, 10.0
                )
        self.assertEqual(len(rec.urls), 2)

    def test_conflict_still_counts_as_created(self):
        """409 means the session already exists, which was always acceptable."""
        rec = Recorder(http_error(409))
        with patch("urllib.request.urlopen", rec):
            self.assertTrue(
                session_kv_server._create_gateway_session("http://gw", "k8s-evt-1", GATEWAY_HEADERS)
            )

    def test_conflict_is_not_mistaken_for_a_missing_prefix(self):
        """409 has to reach the caller, not be retried onto `default`."""
        rec = Recorder(http_error(409))
        with patch("urllib.request.urlopen", rec):
            with self.assertRaises(Exception):
                session_kv_server._post_gateway(
                    "http://gw", "/api/sessions", {"a": 1}, GATEWAY_HEADERS, 10.0
                )
        self.assertEqual(len(rec.urls), 1)


class TriageDatabaseCase(unittest.TestCase):
    """The temp database is shared by the whole file, so clear what we assert on."""

    def setUp(self):
        import sqlite3

        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute("DELETE FROM incidents")
                conn.execute("DELETE FROM session_metadata")

    def seed_session(self, session_id):
        import sqlite3

        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                    (session_id, json.dumps({})),
                )

    def store_incident(self, chat_id, thread_id, report="the RCA"):
        import sqlite3

        with sqlite3.connect(temp_db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO incidents (chat_id, thread_id, report) VALUES (?, ?, ?)",
                    (chat_id, thread_id, report),
                )


class TestRegisterSessionRoutingReturnsChatId(TriageDatabaseCase):
    """The caller needs the chat_id back to look the incident up afterwards."""

    def test_google_chat_chat_id_is_returned(self):
        self.seed_session("k8s-evt-1")
        self.assertEqual(
            session_kv_server._register_session_routing(
                "k8s-evt-1", "google_chat", "spaces/AAAA/threads/BBBB"
            ),
            "spaces/AAAA",
        )

    def test_slack_chat_id_is_returned(self):
        self.seed_session("k8s-evt-2")
        with patch.dict(os.environ, {"SLACK_HOME_CHANNEL": "C123"}, clear=False):
            self.assertEqual(
                session_kv_server._register_session_routing("k8s-evt-2", "slack", "1699999.1234"),
                "C123",
            )

    def test_an_unknown_session_returns_none(self):
        """No row to update means no chat_id, and nothing to look up later."""
        self.assertIsNone(
            session_kv_server._register_session_routing(
                "nope", "google_chat", "spaces/A/threads/B"
            )
        )

    def test_the_returned_chat_id_is_the_one_written_to_metadata(self):
        import sqlite3

        self.seed_session("k8s-evt-3")
        thread = "spaces/CCCC/threads/DDDD"
        returned = session_kv_server._register_session_routing("k8s-evt-3", "google_chat", thread)
        with sqlite3.connect(temp_db_path) as conn:
            stored = json.loads(
                conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?", ("k8s-evt-3",)
                ).fetchone()[0]
            )
        self.assertEqual(returned, stored["chat_id"])
        self.assertEqual(stored["thread_id"], thread)


class TestIncidentStored(TriageDatabaseCase):
    """A row is the one durable proof the RCA reached a human."""

    def test_false_when_the_turn_delivered_nothing(self):
        self.assertFalse(session_kv_server._incident_stored("spaces/A", "spaces/A/threads/B"))

    def test_true_once_send_notification_has_written_its_row(self):
        self.store_incident("spaces/A", "spaces/A/threads/B")
        self.assertTrue(session_kv_server._incident_stored("spaces/A", "spaces/A/threads/B"))

    def test_another_threads_incident_does_not_count(self):
        self.store_incident("spaces/A", "spaces/A/threads/OTHER")
        self.assertFalse(session_kv_server._incident_stored("spaces/A", "spaces/A/threads/B"))

    def test_an_unreadable_database_is_not_read_as_delivered(self):
        with patch.object(session_kv_server, "SESSION_KV_DB_PATH", "/nonexistent/dir/x.db"):
            self.assertFalse(session_kv_server._incident_stored("spaces/A", "spaces/A/threads/B"))


class TestUndeliveredTriageIsLogged(TriageDatabaseCase):
    """A triage that lost its report stops being indistinguishable from one that worked."""

    THREAD = "spaces/A/threads/B"

    def setUp(self):
        super().setUp()
        self.seed_session("k8s-evt-1")

    def _run_trigger(self, thread_id=THREAD):
        return [
            patch.object(session_kv_server, "get_active_platform", return_value="google_chat"),
            patch.object(session_kv_server, "_post_initial_alert", return_value=thread_id),
            patch.object(session_kv_server, "_create_gateway_session", return_value=True),
            patch.object(session_kv_server, "_start_agent_turn"),
        ]

    def _trigger(self, thread_id=THREAD):
        patches = self._run_trigger(thread_id)
        for p in patches:
            p.start()
        try:
            session_kv_server.trigger_agent_troubleshooter(
                "k8s-evt-1", "alert", {"reason": "Failed"}
            )
        finally:
            for p in patches:
                p.stop()

    def test_a_turn_that_stored_no_incident_warns(self):
        with self.assertLogs(session_kv_server.logger, level="WARNING") as caught:
            self._trigger()
        self.assertTrue(
            any("no incident stored" in line for line in caught.output),
            f"expected a warning naming the undelivered thread, got: {caught.output}",
        )

    def test_a_delivered_turn_is_quiet(self):
        self.store_incident("spaces/A", self.THREAD)
        with patch.object(session_kv_server.logger, "warning") as warn:
            self._trigger()
        warn.assert_not_called()

    def test_no_chat_thread_means_no_warning(self):
        """An alert that never posted has no thread it could have delivered into."""
        with patch.object(session_kv_server.logger, "warning") as warn:
            self._trigger(thread_id=None)
        warn.assert_not_called()


if __name__ == "__main__":
    # Clean up temp database file on exit
    try:
        unittest.main()
    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
