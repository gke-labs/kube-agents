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

"""Judge a repetition's saved answer once more when a gated judged score is missing.

devops-bench scores every metric inside one ``try`` per metric, logs the
exception, and writes the record without the key when the judge call fails
(``metrics/pipeline.py``, ``evaluate_metrics_batch``). ``results.json`` then
comes back with ``OutcomeValidity`` absent, and rung 6 averages the repetitions
that kept one -- a mean over one or two scores held to a margin sized for
three (#2546).

The answer the judge needed is still in the record (``input``, ``output``,
``expected_output``), so the score can be asked for again without running the
agent again. ``hack/ci-eval-pr.sh`` calls this on each repetition's
``results.json`` right after devops-bench writes it, before the grading pass
reads it. Only the missing gated metrics are judged, and only once.

What it writes back, beside ``scores``:

- ``rejudged``: the metric names this filled in, so a reader of the record can
  tell a second-pass score from a first-pass one;
- ``judge_errors``: per metric still missing, the exception the judge raised on
  the second pass (``type``, ``root_type``, ``message``), kept so the cause can
  be named -- a provider 429 is not a bad answer -- rather than guessed.

When a score is filled, the composite ``OutcomeScore`` is assembled again the
way devops-bench assembles it, since a task with no deterministic correctness
key takes its correctness from ``OutcomeValidity``.

Records the harness marked as infrastructure are left alone: the scorer sets
them aside whatever their scores say, so judging them again spends quota on
nothing.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kube_agents_bench.gate import _judged_metrics
from kube_agents_bench.scoring import DELEGATION_CEILING_MARKER, INFRA_FAILURE_MARKER, score_value

REJUDGED_KEY = "rejudged"
JUDGE_ERRORS_KEY = "judge_errors"
OUTCOME_VALIDITY = "OutcomeValidity"
CATASTROPHIC_KEY = "VerificationCatastrophic"
USE_MCP_ENV_VAR = "BENCH_USE_MCP"
# Long enough to keep a provider's status line and error code, short enough
# that a stack of HTML from a proxy does not bloat the record.
ERROR_MESSAGE_LIMIT = 500
EXIT_UNREADABLE = 1
PARTIAL_SUFFIX = ".rejudge-partial"

Rejudger = Callable[[dict[str, Any], Any], dict[str, Any]]


def _use_mcp() -> bool:
    from devops_bench.core import get_bool

    return get_bool(USE_MCP_ENV_VAR, True)


def _outcome_validity(record: dict[str, Any], judge: Any) -> dict[str, Any]:
    """Score OutcomeValidity on the saved answer; raises what the judge raises.

    ``measure`` is called directly rather than through devops-bench's batch
    pipeline, whose per-metric ``except`` keeps only a log line -- the exception
    type is what tells a 429 from a bad answer.
    """
    from devops_bench.metrics.outcome_validity import build_outcome_validity_metric
    from devops_bench.metrics.pipeline import _build_context

    ctx = _build_context(record, judge, _use_mcp())
    metric = build_outcome_validity_metric(judge, generation_only=ctx.generation_only)
    metric.measure(ctx.outcome_case)
    return {"score": metric.score, "success": metric.is_successful(), "reason": metric.reason}


REJUDGERS: dict[str, Rejudger] = {OUTCOME_VALIDITY: _outcome_validity}


def _default_judge() -> Any:
    from devops_bench.metrics.geval import get_judge_model

    return get_judge_model()


def _describe(exc: BaseException) -> dict[str, str]:
    # Bounded by what it has seen: the interpreter keeps __context__ acyclic
    # but never checks an explicit __cause__, so a third-party chain can loop.
    root = exc
    seen = {id(exc)}
    while (nxt := root.__cause__ or root.__context__) is not None and id(nxt) not in seen:
        seen.add(id(nxt))
        root = nxt
    return {
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "root_type": f"{type(root).__module__}.{type(root).__qualname__}",
        "message": str(exc)[:ERROR_MESSAGE_LIMIT],
    }


def is_infra(record: dict[str, Any]) -> bool:
    error = record.get("error") or record.get("errors")
    return error is not None and any(m in str(error) for m in (INFRA_FAILURE_MARKER, DELEGATION_CEILING_MARKER))


def _tripped_a_safeguard(scores: dict[str, Any]) -> bool:
    # The scorer grades this repetition blocked and leaves it out of the
    # judged mean, so a filled score would count for nothing.
    catastrophic = score_value(scores, CATASTROPHIC_KEY)
    return catastrophic is not None and catastrophic < 1.0


def missing_metrics(record: dict[str, Any], metrics: tuple[str, ...]) -> list[str]:
    """Gated metrics a scored, non-infrastructure record has no score for."""
    scores = record.get("scores")
    if not scores or is_infra(record) or _tripped_a_safeguard(scores):
        return []
    return [m for m in metrics if m not in scores]


def rejudge_record(
    record: dict[str, Any],
    metrics: tuple[str, ...],
    judge_factory: Callable[[], Any],
    rejudgers: dict[str, Rejudger] = REJUDGERS,
) -> list[str]:
    """Fill in missing gated scores in place; return the lines worth logging."""
    lines: list[str] = []
    judge = None
    for name in missing_metrics(record, metrics):
        rejudger = rejudgers.get(name)
        if rejudger is None:
            lines.append(f"{name}: missing, and there is no re-judge for it")
            continue
        try:
            if judge is None:
                judge = judge_factory()
            entry = rejudger(record, judge)
        except Exception as exc:  # noqa: BLE001 - recorded in the record, never raised
            described = _describe(exc)
            record.setdefault(JUDGE_ERRORS_KEY, {})[name] = described
            lines.append(f"{name}: still missing after a re-judge ({described['type']}: {described['message']})")
            continue
        record["scores"][name] = entry
        errors = record.get(JUDGE_ERRORS_KEY, {})
        errors.pop(name, None)
        if not errors:
            record.pop(JUDGE_ERRORS_KEY, None)
        record.setdefault(REJUDGED_KEY, []).append(name)
        lines.append(f"{name}: re-judged, score {entry['score']}")
    if REJUDGED_KEY in record:
        _refresh_outcome_score(record["scores"])
    return lines


def _refresh_outcome_score(scores: dict[str, Any]) -> None:
    try:
        from devops_bench.metrics.pipeline import _finalize_outcome_score

        _finalize_outcome_score(scores)
    except Exception as exc:  # noqa: BLE001 - the composite is reported, never gated
        print(f"rejudge: OutcomeScore not recomputed ({type(exc).__name__}: {exc})", file=sys.stderr)


def rejudge_file(
    path: Path,
    metrics: tuple[str, ...],
    judge_factory: Callable[[], Any] = _default_judge,
) -> list[str]:
    """Re-judge every record in one ``results.json``; rewrite it only on a change."""
    loaded = json.loads(path.read_text(encoding="utf-8"))
    records = [loaded] if isinstance(loaded, dict) else loaded
    lines: list[str] = []
    for record in records:
        lines.extend(rejudge_record(record, metrics, judge_factory))
    if lines:
        # Replaced, not rewritten in place: a job killed mid-write must leave
        # the record devops-bench wrote, not a truncated one the grader reads
        # as no run at all.
        partial = path.with_name(path.name + PARTIAL_SUFFIX)
        try:
            partial.write_text(json.dumps(loaded, indent=2), encoding="utf-8")
            os.replace(partial, path)
        except OSError:
            partial.unlink(missing_ok=True)
            raise
    return lines


def main(argv: list[str] | None = None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    metrics = _judged_metrics()
    status = 0
    for raw in paths:
        path = Path(raw)
        try:
            lines = rejudge_file(path, metrics)
        except (OSError, ValueError) as exc:
            print(f"rejudge: cannot read or rewrite {path}: {exc}", file=sys.stderr)
            status = EXIT_UNREADABLE
            continue
        for line in lines:
            print(f"rejudge {path.parent.name}: {line}")
    return status


if __name__ == "__main__":
    sys.exit(main())
