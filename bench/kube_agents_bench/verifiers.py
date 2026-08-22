# Copyright 2026 The Kubernetes Authors.
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

"""Leaf verifiers over the agent's transcript rather than cluster state.

Upstream's verifiers all read the cluster, which answers "did the world end
up right". These two answer the other half of a task's exact checks: did the
*report* name the thing we planted, and did the agent *call* the tools it
claims to have used. Both read the per-run stash in
:mod:`kube_agents_bench.transcript`, and both fail closed: an empty stash is
``status="error"`` — the check could not be evaluated — never a pass or a
fail, so ``VerificationCoverage`` drops below 1.0 and the gate catches it.

Registered under the ``devops_bench.verifiers`` entry-point group in
``pyproject.toml`` (the same mechanism ``devops_bench.agents`` already uses
for the harness), so devops-bench discovers them without a fork.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import Field

from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult

from kube_agents_bench import transcript

__all__ = ["ReportContainsVerifier", "ToolCalledVerifier"]

_NO_TRANSCRIPT_REASON = (
    "no transcript stashed for this run: the harness did not complete an "
    "agent execution (kube_agents_bench.transcript is empty), so this check "
    "could not be evaluated"
)


@VERIFIERS.register("report_contains")
class ReportContainsVerifier(BaseVerifier):
    """Exact phrase checks against the agent's answer.

    Case-insensitive substring matching, deliberately: the task author chose
    the phrase (a planted defect's name, a required noun), so an exact match
    is fair, and case is the one variation a correct report may legitimately
    introduce. Anything fuzzier belongs to the judge, not to a blocking check.

    ``scope`` picks the text under test. The default, ``final``, is what the
    user ultimately receives: the delegating turn's own closing message plus,
    when work was delegated, the delivered card results and artifacts — the
    worker's actual answer, with the router's intermediate poll recitals
    excluded. ``full`` is the accumulated output: every settled closer on top
    of all of that. ``full`` therefore passes a required phrase the agent
    merely QUOTED in progress chatter and false-fails a forbidden phrase that
    only appears in quoted material — reach for it only when the check
    genuinely concerns the whole transcript.
    """

    type: Literal["report_contains"]
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    # At least ONE must appear. For a concept with several legitimate
    # spellings ("HPA" / "HorizontalPodAutoscaler"), all-of required_phrases
    # would punish a correct report for choosing the other name.
    any_of_phrases: list[str] = Field(default_factory=list)
    scope: Literal["final", "full"] = "final"

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        snap = transcript.get()
        if snap is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_TRANSCRIPT_REASON,
            )
        text = (snap.final_message if self.scope == "final" else snap.output).lower()
        missing = [p for p in self.required_phrases if p.lower() not in text]
        present = [p for p in self.forbidden_phrases if p.lower() in text]
        any_of_miss = bool(self.any_of_phrases) and not any(
            p.lower() in text for p in self.any_of_phrases
        )
        if missing or present or any_of_miss:
            parts = []
            if missing:
                parts.append(f"required phrases absent from the report: {missing}")
            if present:
                parts.append(f"forbidden phrases present in the report: {present}")
            if any_of_miss:
                parts.append(
                    f"none of the alternative phrasings present: {self.any_of_phrases}"
                )
            return VerificationResult(
                success=False,
                elapsed_time=time.monotonic() - start,
                reason="; ".join(parts),
            )
        return VerificationResult(
            success=True,
            elapsed_time=time.monotonic() - start,
            reason=(
                f"report contains all {len(self.required_phrases)} required "
                f"phrase(s) and none of {len(self.forbidden_phrases)} forbidden"
            ),
        )


@VERIFIERS.register("tool_called")
class ToolCalledVerifier(BaseVerifier):
    """Count trajectory entries whose tool name is in ``tool_names``.

    THE TRAJECTORY IS THE ROUTER'S, NOT THE FLEET'S. By this harness's
    design, ``result.trajectory`` holds only the delegating turn's calls:
    poll-turn calls are the harness's own bookkeeping and are kept out
    (``_fold_status_turn``), and a delegated worker's calls never reach it
    at all. This verifier can therefore assert what the ROUTER did
    (``kanban_create`` is the router's own call) and nothing about what a
    worker did on a cluster — a mutation safeguard built on it would be
    blind to the very calls it fears. Use a cluster-state check
    (``resource_property``) for those.

    Passes when at least ``minimum_calls`` matching calls were made. Wrapped
    in a ``none`` compound, it is the safeguard shape "this tool was never
    called", within the router-only limits above. Names match the harness's
    canonical trajectory entries (``ToolCall.to_dict()["name"]``), e.g.
    ``kanban_create``.
    """

    type: Literal["tool_called"]
    tool_names: list[str] = Field(min_length=1)
    minimum_calls: int = Field(default=1, ge=1)
    # Objectives set this: a call the harness marked status="error" produced
    # no effect (kanban_create that failed filed no card), so counting it
    # would pass a check whose subject never happened. Safeguards leave it
    # False on purpose — an ATTEMPTED forbidden write should trip the
    # safeguard whether or not the tool succeeded.
    require_success: bool = False

    def verify(self, timeout_sec: float) -> VerificationResult:
        start = time.monotonic()
        snap = transcript.get()
        if snap is None:
            return VerificationResult(
                success=False,
                status="error",
                elapsed_time=time.monotonic() - start,
                reason=_NO_TRANSCRIPT_REASON,
            )
        wanted = set(self.tool_names)
        calls = [
            entry
            for entry in snap.trajectory
            if isinstance(entry, dict)
            and entry.get("name") in wanted
            and not (self.require_success and entry.get("status") == "error")
        ]
        count = len(calls)
        ok = count >= self.minimum_calls
        return VerificationResult(
            success=ok,
            elapsed_time=time.monotonic() - start,
            reason=(
                f"{count} call(s) to {sorted(wanted)} in the trajectory"
                f" (minimum {self.minimum_calls})"
            ),
            raw={"matching_calls": count},
        )
