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

One caveat, and it is why consumers must fail closed: in a multi-task
process, when the eval harness raises AFTER task A completed but BEFORE task
B's ``execute_agent`` ever calls :meth:`KubeAgentsHarness.run`, B's
verification still runs and the stash still holds A's transcript. The
``clear()`` at the top of ``run()`` cannot help there, because ``run()``
never fired for B — the stale window is real and this module cannot close it
from below. Two mitigations, neither a fix: a verifier finding ``None`` must
return ``status="error"`` (never pass, never fail) so the *empty* case
surfaces as ``VerificationCoverage < 1.0``; and every snapshot carries
``seq`` plus the prompt's head, so harness-side code that knows the current
task (this module's consumers do not — a verifier is constructed from the
spec alone and never sees the prompt) can detect the mismatch, and a human
reading a suspicious result can see at a glance which prompt produced it. A
prompt-hash comparison inside the verifiers is NOT feasible for that reason;
the docstring is the mitigation until the upstream ``verify()`` context
argument exists.

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
        seq: Monotonic per-process counter, stamped at ``set()`` time. The
            staleness marker for the module-docstring caveat: verifiers
            cannot check it (a verifier never sees the prompt or the task),
            but harness-side code that knows the current task can, and it
            dates a snapshot unambiguously in a debug session.
        prompt_head: First 64 characters of the prompt that produced this
            transcript, for human correlation only — never matched against.
    """

    output: str
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    prompt_head: str = ""
    # What the user ultimately receives: the delegating turn's closing
    # message plus, when work was delegated, the delivered card results and
    # artifacts (composed by the harness's _append_final; poll-turn recitals
    # excluded). ``output`` additionally accumulates every settled closer, so
    # a phrase check against it passes on progress chatter; checks default to
    # this field instead.
    final_message: str = ""


_current: TranscriptSnapshot | None = None
_seq = 0


def set(  # noqa: A001 - deliberate, matches get/clear
    output: str,
    trajectory: list[dict[str, Any]],
    prompt: str = "",
    final_message: str = "",
) -> None:
    """Stash the just-finished run's transcript for the verifiers."""
    global _current, _seq
    _seq += 1
    _current = TranscriptSnapshot(
        output=output,
        trajectory=list(trajectory),
        seq=_seq,
        prompt_head=prompt[:64],
        final_message=final_message or output,
    )


def get() -> TranscriptSnapshot | None:
    """The current run's transcript, or ``None`` when no run has finished."""
    return _current


def clear() -> None:
    """Empty the stash. Called before each run so stale data cannot be graded."""
    global _current
    _current = None
