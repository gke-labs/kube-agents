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

"""Deterministic Customer-Experience (CX) Quality Scorer for kube-agents evaluations."""

from __future__ import annotations

import re
from typing import Any

_MARKDOWN_NOISE = str.maketrans("", "", "*_`#")

_STUB_PATTERNS = (
    "checking ",
    "i am checking",
    "let me check",
    "looking into",
    "filed a task",
    "task has been created",
    "currently checking",
    "querying ",
    "inspecting ",
    "gathering ",
)

_TERMINAL_CONTENT_MARKERS = (
    "```",
    "|",
    "\n-",
    "\n*",
    "Status:",
    "Active",
    "Version:",
    "NAMESPACE",
    "NAME",
    "READY",
    "AGE",
)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.translate(_MARKDOWN_NOISE)).strip().lower()


def _contains_word(keyword: str, text: str) -> bool:
    """Checks for keyword presence using word boundaries."""
    escaped = re.escape(keyword.lower().strip())
    return bool(re.search(rf"(?:\b|^){escaped}(?:\b|$)", text.lower()))


def evaluate_response_quality(
    prompt_id: int,
    output_text: str,
    target_criteria: str = "",
    required_keywords: list[str] | None = None,
    forbidden_keywords: list[str] | None = None,
    min_length: int = 15,
) -> tuple[bool, str]:
    """Evaluates an agent response deterministically against quality and thread hygiene rules.

    Returns:
        (passed, explanation)
    """
    if not output_text or len(output_text.strip()) < min_length:
        return False, f"Fail: Response truncated or too short (<{min_length} chars)"

    lowered = normalize_text(output_text)

    # 1. Delegation stub check: agent must return completed diagnostic, not a turn-1 stub.
    # Rejects turn-1 status messages regardless of length unless substantive diagnostic output exists.
    is_stub = any(p in lowered for p in _STUB_PATTERNS)
    has_diagnostic_content = any(c in output_text for c in _TERMINAL_CONTENT_MARKERS)
    if is_stub and not has_diagnostic_content:
        return False, "Fail: Delegation stub returned instead of completed diagnostic"

    # 2. Clarification loop check: agent should default to host cluster rather than halting
    clarification_markers = ["which cluster did you mean", "could you clarify which cluster"]
    if any(m in lowered for m in clarification_markers):
        return False, "Fail: Clarification loop triggered instead of host cluster default"

    # 3. Prompt #1: Backing Model & Provider Identity
    if prompt_id == 1:
        leaks = []
        if _contains_word("custom", lowered) and "vertex" not in lowered:
            leaks.append("Internal LiteLLM 'custom' provider placeholder leaked")
        if "model-default" in lowered:
            leaks.append("Internal LiteLLM router alias 'model-default' leaked")
        if leaks:
            return False, f"Fail: {'; '.join(leaks)}"
        has_model = any(m in lowered for m in ("gemini", "claude"))
        has_provider = any(p in lowered for p in ("vertex", "google"))
        if not (has_model and has_provider):
            return False, "Fail: Backing model/provider identity unrecognized"

    # 4. Prompt #21: Agent Substrate Architectural Plan
    if prompt_id == 21:
        has_substrate = "substrate" in lowered or "agent substrate" in lowered
        has_cluster_part = any(k in lowered for k in ["cluster", "crd", "operator", "node", "infrastructure", "namespace", "helm", "rbac", "ate.dev"])
        has_workload_part = any(k in lowered for k in ["workload", "actor", "worker", "pod", "sandbox", "agent pool"])
        has_plan = any(k in lowered for k in ["plan", "staging", "phase", "step", "prerequisite", "before enabling", "prior to", "verification"])
        if not (has_substrate and has_cluster_part and has_workload_part and has_plan):
            missing = []
            if not has_substrate:
                missing.append("Substrate recognition")
            if not has_cluster_part:
                missing.append("Cluster part (CRDs/node labels/operator)")
            if not has_workload_part:
                missing.append("Workload part (Workers/Actors/sandboxes)")
            if not has_plan:
                missing.append("/plan staging prior to enabling")
            return False, f"Fail: Incomplete substrate plan, missing {', '.join(missing)}"

    # 5. General required keyword presence (anchored with word boundaries)
    if required_keywords:
        missing_req = [k for k in required_keywords if not _contains_word(k, lowered)]
        if missing_req:
            return False, f"Fail: Missing required keyword(s): {', '.join(missing_req)}"

    # 6. General forbidden keyword absence (anchored with word boundaries)
    if forbidden_keywords:
        present_forb = [k for k in forbidden_keywords if _contains_word(k, lowered)]
        if present_forb:
            return False, f"Fail: Contains forbidden keyword(s): {', '.join(present_forb)}"

    return True, "Pass: Response meets quality and hygiene criteria"


try:
    from inspect_ai.scorer import Score, Scorer, Target, accuracy, scorer, stderr
    from inspect_ai.solver import TaskState

    @scorer(metrics=[accuracy(), stderr()])
    def kube_agents_cx_scorer() -> Scorer:
        """Inspect AI scorer implementation of CX quality checks."""
        async def score(state: TaskState, target: Target) -> Score:
            output = state.output.completion
            meta = state.metadata or {}
            pid = meta.get("id", 0)
            req = meta.get("required", [])
            forb = meta.get("forbidden", [])
            min_len = meta.get("min_length", 15)

            passed, explanation = evaluate_response_quality(
                prompt_id=pid,
                output_text=output,
                required_keywords=req,
                forbidden_keywords=forb,
                min_length=min_len,
            )
            return Score(
                value=1.0 if passed else 0.0,
                explanation=explanation,
            )

        return score

except ImportError:
    # Allow importing evaluate_response_quality standalone without inspect_ai installed
    pass
