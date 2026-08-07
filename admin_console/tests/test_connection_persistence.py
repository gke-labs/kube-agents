from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from admin_console.connection_persistence import (
    delete_connection,
    load_connection,
    save_connection,
)
from admin_console.project_config import DeploymentTarget


class ConnectionPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "connection.json"
        self.environment = patch.dict(
            os.environ,
            {"KUBE_AGENTS_ADMIN_CONNECTION_STATE": str(self.state_path)},
        )
        self.environment.start()
        self.target = DeploymentTarget(
            "test-project-01",
            "test-cluster-01",
            "us-east4",
            "kubeagents-system",
            "manual selection",
        )
        self.verified_at = datetime(2026, 8, 5, 18, 30, tzinfo=UTC)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_round_trip_is_owner_only_and_contains_no_credentials(self):
        save_connection("admin@example.com", self.target, self.verified_at)

        restored = load_connection("admin@example.com")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.target, self.target)
        self.assertEqual(restored.verified_at, self.verified_at)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        payload = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("token", payload.lower())
        self.assertNotIn("credential", payload.lower())

    def test_state_is_bound_to_the_launcher_identity(self):
        save_connection("admin@example.com", self.target, self.verified_at)
        self.assertIsNone(load_connection("someone-else@example.com"))

    def test_insecure_or_malformed_state_is_not_loaded(self):
        self.state_path.write_text("{}", encoding="utf-8")
        self.state_path.chmod(0o644)
        self.assertIsNone(load_connection("admin@example.com"))

        self.state_path.chmod(0o600)
        self.state_path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        self.assertIsNone(load_connection("admin@example.com"))

    def test_delete_forgets_the_target(self):
        save_connection("admin@example.com", self.target, self.verified_at)
        delete_connection()
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
