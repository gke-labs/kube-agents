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

"""Per-run transcript stash bridging the harness to the text/trace verifiers.

devops-bench's ``BaseVerifier.verify()`` receives only a timeout: every
upstream verifier reads cluster state, so none of them needed the agent's
transcript, and the eval harness persists the transcript only *after*
verification has run. The verifiers in :mod:`kube_agents_bench.verifiers`
need it *during* verification, and both ends of that gap are code this
package owns — the harness that produces the transcript and the verifiers
that consume it — so a module-level stash closes it without an upstream
change.

Safe because task execution is single-threaded: ``DefaultEvalHarness.run``
iterates tasks serially and ``execute_agent`` / ``_run_verification`` run
sequentially on the same thread (the only thread upstream spawns anywhere is
the chaos-scenario daemon). One process, one task in flight, one stash.

One caveat, and it is why consumers must fail closed: on the harness's
exception path, verification still runs when infrastructure came up but the
harness raised *before* ``execute_agent`` — and the stash then holds the
PREVIOUS task's transcript. :meth:`KubeAgentsHarness.run` clears the stash
before executing so that window holds ``None`` rather than stale data, and a
verifier finding ``None`` must return ``status="error"`` (never pass, never
fail) so the miss surfaces as ``VerificationCoverage < 1.0`` instead of a
silently graded wrong transcript.

The clean long-term fix is an upstream ``verify()`` context argument; this
module is the seam to delete when that lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["TranscriptSnapshot", "clear", "get", "set"]


@dataclass(frozen=True)
class TranscriptSnapshot:
    """What the verifiers may read from the finished agent run.

    Attributes:
        output: Final assistant text, verbatim from ``AgentResult.output``.
        trajectory: Ordered ``ToolCall.to_dict()`` entries from
            ``AgentResult.trajectory``; each carries ``name`` / ``args`` /
            ``result`` / ``status``.
    """

    output: str
    trajectory: list[dict[str, Any]] = field(default_factory=list)


_current: TranscriptSnapshot | None = None


def set(output: str, trajectory: list[dict[str, Any]]) -> None:  # noqa: A001 - deliberate, matches get/clear
    """Stash the just-finished run's transcript for the verifiers."""
    global _current
    _current = TranscriptSnapshot(output=output, trajectory=list(trajectory))


def get() -> TranscriptSnapshot | None:
    """The current run's transcript, or ``None`` when no run has finished."""
    return _current


def clear() -> None:
    """Empty the stash. Called before each run so stale data cannot be graded."""
    global _current
    _current = None
