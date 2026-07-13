import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from session_manager import SessionManager

platform_plugin_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "platform", "defaults", "plugins", "session_store")
)
if platform_plugin_dir not in sys.path:
    sys.path.insert(0, platform_plugin_dir)

from store import SessionMetadata, SessionMetadataStore, write_session_metadata


class TestSessionStoreGroups(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_session.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_session_metadata_to_dict_includes_groups(self):
        metadata = SessionMetadata(
            session_id="session-123",
            platform="google_chat",
            user_id="user@example.com",
            user_email="user@example.com",
            user_groups=["sre-team@example.com", "dev-team@example.com"],
        )
        data = metadata.to_dict()
        self.assertEqual(data["user_groups"], ["sre-team@example.com", "dev-team@example.com"])

    def test_session_manager_delegation_headers(self):
        os.environ["SESSION_KV_DB_PATH"] = self.db_path
        metadata = {
            "session_id": "session-123",
            "user_email": "alice@example.com",
            "user_groups": ["sre-team@example.com"],
        }
        write_session_metadata("session-123", metadata)

        sm = SessionManager(db_path=self.db_path)
        ctx = sm.current_context("session-123")
        headers = sm.delegation_headers(ctx)

        self.assertEqual(headers.get("X-Hermes-User-Email"), "alice@example.com")
        self.assertEqual(headers.get("X-Hermes-User-Groups"), '["sre-team@example.com"]')


if __name__ == "__main__":
    unittest.main()
