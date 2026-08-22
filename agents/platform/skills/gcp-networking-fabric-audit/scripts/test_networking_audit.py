#!/usr/bin/env python3
"""Unit tests for networking_audit.py."""

import unittest
from unittest.mock import patch

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import networking_audit

class TestNetworkingAudit(unittest.TestCase):
    @patch("networking_audit.run_gcloud_json")
    def test_audit_project_networking_rejected_psc(self, mock_gcloud):
        mock_gcloud.return_value = [
            {
                "name": "psc-ep-1",
                "region": "projects/p/regions/us-central1",
                "target": "projects/p/regions/us-central1/serviceAttachments/sa-1",
                "pscConnectionStatus": "REJECTED"
            },
            {
                "name": "psc-ep-2",
                "region": "projects/p/regions/us-central1",
                "target": "projects/p/regions/us-central1/serviceAttachments/sa-2",
                "pscConnectionStatus": "ACCEPTED"
            }
        ]

        findings = networking_audit.audit_project_networking("test-proj")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "psc-routing-deadlock")
        self.assertEqual(findings[0]["object"], "ForwardingRule/psc-ep-1")

    @patch("networking_audit.run_gcloud_json")
    def test_audit_project_networking_empty(self, mock_gcloud):
        mock_gcloud.return_value = []
        findings = networking_audit.audit_project_networking("test-proj")
        self.assertEqual(findings, [])

if __name__ == "__main__":
    unittest.main()
