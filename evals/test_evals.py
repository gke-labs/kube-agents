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

"""Unit tests for kube-agents eval dataset and deterministic scorers."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scorers import evaluate_response_quality

DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "kube_agents_eval_dataset.jsonl"


class TestKubeAgentsEvals(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DATASET_PATH.is_file(), f"Dataset file missing at {DATASET_PATH}")
        with open(DATASET_PATH, encoding="utf-8") as f:
            self.samples = [json.loads(line) for line in f if line.strip()]

    def test_dataset_coverage(self):
        """Verifies that all 21 benchmarking prompts are present in the dataset."""
        self.assertEqual(len(self.samples), 21, "Expected exactly 21 prompts in dataset")
        ids = [s["id"] for s in self.samples]
        self.assertEqual(ids, list(range(1, 22)))

    def test_prompt_1_scorer(self):
        """Tests Prompt #1 model identity assertions."""
        # Good response
        passed, msg = evaluate_response_quality(
            prompt_id=1,
            output_text="I am running on the gemini-3.8-flash model provided by Google Cloud Vertex AI (vertex_ai).",
        )
        self.assertTrue(passed, msg)

        # Leaked model-default
        passed, msg = evaluate_response_quality(
            prompt_id=1,
            output_text="I am currently running on model-default via the custom provider.",
        )
        self.assertFalse(passed)
        self.assertIn("model-default", msg)

    def test_prompt_21_scorer(self):
        """Tests Prompt #21 Agent Substrate planning assertions."""
        # Good response
        good_response = """
Here is my plan to install Agent Substrate on this cluster:

## Step 1: Cluster-level Infrastructure
- Install Substrate CRDs and operator in `substrate-system`.
- Configure node pool labels with `ate.dev/substrate-version=v1`.
- Verify RBAC service accounts and mutating admission webhooks.

## Step 2: Workload Execution Layer
- Deploy Worker compute pools for agent execution sandboxes.
- Define Actor custom resources for platform agents.

Please review this /plan before enabling or mutating cluster infrastructure.
"""
        passed, msg = evaluate_response_quality(
            prompt_id=21,
            output_text=good_response,
        )
        self.assertTrue(passed, msg)

        # Missing workload part
        bad_response = "I will install the substrate operator and CRDs right away."
        passed, msg = evaluate_response_quality(
            prompt_id=21,
            output_text=bad_response,
        )
        self.assertFalse(passed)
        self.assertIn("Workload part", msg)

    def test_delegation_stub_rejection(self):
        """Tests that opening delegation stubs are rejected."""
        passed, msg = evaluate_response_quality(
            prompt_id=9,
            output_text="checking pods in kubeagents-system.",
        )
        self.assertFalse(passed)
        self.assertIn("Delegation stub", msg)

    def test_narrative_stub_non_rejection(self):
        """Tests that full narrative answers containing words like 'querying' are not rejected as stubs."""
        passed, msg = evaluate_response_quality(
            prompt_id=19,
            output_text="Querying container logs is restricted by RBAC for platform-agent-gateway in namespace kubeagents-system.",
            required_keywords=["platform-agent-gateway"],
        )
        self.assertTrue(passed, msg)

    def test_negation_rejection(self):
        """Tests that negated status conditions do not satisfy positive requirements."""
        passed, msg = evaluate_response_quality(
            prompt_id=11,
            output_text="The platform-agent pod is not running in the cluster.",
            required_keywords=["platform-agent", "running"],
        )
        self.assertFalse(passed)
        self.assertIn("Missing required keyword", msg)

    def test_status_word_boundary(self):
        """Tests that substring matches like 'already' do not satisfy 'ready'."""
        passed, msg = evaluate_response_quality(
            prompt_id=10,
            output_text="The node is already prepared and configured.",
            required_keywords=["node", "ready"],
        )
        self.assertFalse(passed)
        self.assertIn("Missing required keyword", msg)


if __name__ == "__main__":
    unittest.main()
