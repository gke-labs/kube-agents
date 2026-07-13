import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from identity_resolver import CloudIdentityResolver


class TestCloudIdentityResolver(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_session.db")
        self.resolver = CloudIdentityResolver(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_email(self):
        self.assertEqual(self.resolver.get_user_groups(""), [])

    @patch.object(CloudIdentityResolver, "_query_cloud_identity")
    def test_cache_miss_and_hit(self, mock_query):
        mock_query.return_value = ["sre-team@example.com", "dev-team@example.com"]

        # First call: Cache miss -> Should call API
        groups = self.resolver.get_user_groups("alice@example.com")
        self.assertEqual(groups, ["sre-team@example.com", "dev-team@example.com"])
        self.assertEqual(mock_query.call_count, 1)

        # Second call: Cache hit -> Should NOT call API
        groups_cached = self.resolver.get_user_groups("alice@example.com")
        self.assertEqual(groups_cached, ["sre-team@example.com", "dev-team@example.com"])
        self.assertEqual(mock_query.call_count, 1)

    @patch.object(CloudIdentityResolver, "_query_cloud_identity")
    def test_cache_expiration(self, mock_query):
        mock_query.return_value = ["sre-team@example.com"]

        expired_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO user_group_cache (user_email, groups_json, expires_at) VALUES (?, ?, ?)",
                ("bob@example.com", '["old-group@example.com"]', expired_time)
            )

        groups = self.resolver.get_user_groups("bob@example.com")
        self.assertEqual(groups, ["sre-team@example.com"])
        self.assertEqual(mock_query.call_count, 1)


if __name__ == "__main__":
    unittest.main()
