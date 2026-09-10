#!/usr/bin/env python3
"""Unit tests for recommender_ingest.py."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import recommender_ingest


class TestRecommenderIngest(unittest.TestCase):

    def test_recommender_specs_scope(self):
        """Verify only non-overlapping GKE diagnosis checks are registered."""
        self.assertEqual(
            set(recommender_ingest.RECOMMENDER_SPECS.keys()),
            {"gke-webhook-readiness", "gke-security-posture-cve"},
        )
        for check, spec in recommender_ingest.RECOMMENDER_SPECS.items():
            self.assertEqual(
                spec["recommender"], "google.container.DiagnosisRecommender"
            )

    def test_get_target_projects_cli(self):
        """CLI argument takes priority."""
        self.assertEqual(
            recommender_ingest.get_target_projects("my-proj"), ["my-proj"]
        )

    def test_get_target_projects_env(self):
        """Environment variables are used when CLI is omitted."""
        with patch.dict(os.environ, {"MONITORED_PROJECT_IDS": "p1, p2"}):
            projs = recommender_ingest.get_target_projects(None)
            self.assertEqual(projs, ["p1", "p2"])

    @patch("recommender_ingest.query_recommender")
    @patch("recommender_ingest.get_project_locations")
    def test_audit_project_recommenders(self, mock_locs, mock_query):
        """Verify recommender recommendations are ingested and filtered."""
        mock_locs.return_value = (["us-central1-a"], ["us-central1"], ["us-central1"])
        mock_query.side_effect = [
            # gke-webhook-readiness
            [{
                "name": "projects/123/locations/us-central1/recommenders/google.container.DiagnosisRecommender/recommendations/rec-wh",
                "recommenderSubtype": "WEBHOOK_TIMEOUT",
                "description": "Admission webhook takes 10s",
                "stateInfo": {"state": "ACTIVE"},
                "primaryImpact": {"category": "RELIABILITY"},
            }],
            # gke-security-posture-cve
            [{
                "name": "projects/123/locations/us-central1/recommenders/google.container.DiagnosisRecommender/recommendations/rec-sec",
                "recommenderSubtype": "SECURITY_POSTURE_VULNERABILITY",
                "description": "High severity CVE in container",
                "stateInfo": {"state": "ACTIVE"},
                "primaryImpact": {"category": "SECURITY"},
            }],
        ]

        findings = recommender_ingest.audit_project_recommenders("test-proj")
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["check"], "gke-webhook-readiness")
        self.assertEqual(findings[0]["object"], "Recommender/rec-wh")
        self.assertEqual(findings[1]["check"], "gke-security-posture-cve")
        self.assertEqual(findings[1]["object"], "Recommender/rec-sec")


if __name__ == "__main__":
    unittest.main()
