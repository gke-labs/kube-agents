#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the deterministic k8s-event-watcher daily activity summary."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from eod_report_generator import (
    filter_and_aggregate_events,
    generate_markdown_report,
    load_config,
)


class TestEODWatcherRecap(unittest.TestCase):

    def setUp(self):
        self.config = {
            "version": "v1",
            "filters": {
                "min_event_count": 1,
                "include_namespaces": [],
                "exclude_namespaces": ["kube-system"],
                "allowed_reasons": [],
            },
            "sections": {
                "telemetry_summary": True,
                "workload_breakdown": True,
                "action_items": True,
            },
            "formatting": {
                "verbosity": "detailed",
            },
        }

    def test_dynamic_triage_fix_extraction(self):
        sample_dedup_data = {
            "uid1|OOMKilled": {
                "count": 14,
                "namespace": "prod-api",
                "name": "payment-api-64d8988cb7-r76jr",
                "session_id": "k8s-evt-123",
                "message": "Memory cgroup out of memory",
            },
        }

        mock_triage_incidents = [
            {
                "chat_id": "chat-1",
                "thread_id": "prod-api/payment-api",
                "report": """# Triage Incident Report
### Root Cause
Payment API batch worker exhausted container memory limit of 512Mi.

### Remediation
Bump container memory limit from 512Mi to 1Gi in k8s deployment manifest.
""",
            }
        ]

        summary = filter_and_aggregate_events(sample_dedup_data, self.config, incidents=mock_triage_incidents)

        self.assertEqual(summary["total_seen"], 14)
        self.assertEqual(summary["unique_incidents"], 1)
        # Check that the actionable fix was extracted dynamically from the triage report
        self.assertIn("Bump container memory limit from 512Mi to 1Gi", summary["entries"][0]["actionable_fix"])

        report = generate_markdown_report(summary, mock_triage_incidents, self.config, cluster_name="test-cluster")
        self.assertIn("k8s-event-watcher Daily Activity Recap", report)
        self.assertIn("Bump container memory limit from 512Mi to 1Gi", report)


if __name__ == "__main__":
    unittest.main()
