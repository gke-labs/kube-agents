from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from admin_console.project_config import (
    build_project_candidates,
    is_valid_project_id,
    load_provisioned_target,
)


class ProjectConfigTest(unittest.TestCase):
    def test_loads_only_valid_deployment_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "vars.sh"
            state.write_text(
                "\n".join(
                    (
                        "export PROJECT_ID=test-project-01",
                        "export CLUSTER_NAME=test-cluster-01",
                        "export REGION=us-east4",
                        "export NAMESPACE=kubeagents-system",
                        "export API_KEY=must-not-be-read",
                    )
                ),
                encoding="utf-8",
            )

            target = load_provisioned_target(state)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.project_id, "test-project-01")
        self.assertEqual(target.cluster_name, "test-cluster-01")
        self.assertEqual(target.location, "us-east4")
        self.assertEqual(target.namespace, "kubeagents-system")

    def test_rejects_shell_expression_in_project_value(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "vars.sh"
            state.write_text(
                "export PROJECT_ID=$(touch /tmp/portal-must-not-execute)\n",
                encoding="utf-8",
            )
            self.assertIsNone(load_provisioned_target(state))

    def test_candidates_are_valid_and_deduplicated(self):
        self.assertTrue(is_valid_project_id("test-project-01"))
        self.assertFalse(is_valid_project_id("Not A Project"))
        candidates = build_project_candidates(
            None,
            "test-project-01",
            "test-project-01",
        )
        self.assertEqual(
            [(item.project_id, item.source) for item in candidates],
            [("test-project-01", "active gcloud configuration")],
        )

    def test_candidates_distinguish_saved_and_url_projects(self):
        candidates = build_project_candidates(
            None,
            "active-project-01",
            "url-project-01",
            "saved-project-01",
        )

        self.assertEqual(
            [(item.project_id, item.source) for item in candidates],
            [
                ("active-project-01", "active gcloud configuration"),
                ("saved-project-01", "saved connection"),
                ("url-project-01", "URL selection"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
