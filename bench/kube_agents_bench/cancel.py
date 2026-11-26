"""Cancellation marker detection for eval runs."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

CANCEL_MARKER_ENV = "KUBE_AGENTS_CANCEL_MARKER"
DEFAULT_CANCEL_MARKER = "cancelled"

_CANCEL_LOGGED = False


def reset_cancel_log_state() -> None:
    """Reset the one-shot log flag (for tests)."""
    global _CANCEL_LOGGED
    _CANCEL_LOGGED = False


def cancel_marker_path() -> Path | None:
    """Return the Path to the cancellation marker, if configured."""
    raw = os.environ.get(CANCEL_MARKER_ENV)
    if raw:
        return Path(raw)
    state_dir = os.environ.get("STATE_DIR") or os.environ.get("EVAL_STATE_DIR")
    if state_dir:
        return Path(state_dir) / DEFAULT_CANCEL_MARKER
    return None


def is_cancelled() -> bool:
    """True when this eval run has been cancelled."""
    marker = cancel_marker_path()
    return marker is not None and marker.exists()


def _log_cancel_once() -> None:
    global _CANCEL_LOGGED
    if not _CANCEL_LOGGED:
        _CANCEL_LOGGED = True
        print(
            "run cancelled: devops-bench judge and verification scores are off for this run",
            file=sys.stderr,
        )


def wrap_evaluator_applies(evaluator_cls: type) -> None:
    """Wrap an evaluator's applies method to return False if cancelled."""
    orig_applies = evaluator_cls.applies
    if getattr(orig_applies, "_is_cancel_guarded", False):
        return

    def _applies_wrapper(self: Any, ctx: Any, _orig=orig_applies) -> bool:
        if is_cancelled():
            _log_cancel_once()
            return False
        return _orig(self, ctx)

    _applies_wrapper._is_cancel_guarded = True  # type: ignore[attr-defined]
    evaluator_cls.applies = _applies_wrapper  # type: ignore[method-assign]


def install_cancel_scoring_guard() -> None:
    """Guard devops-bench evaluators so they do not run when the run was cancelled."""
    try:
        from devops_bench.metrics.base import METRICS  # type: ignore[import-not-found,import-untyped]
        import devops_bench.metrics.pipeline as pipeline  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        return

    for evaluator_cls in list(METRICS.values()):
        wrap_evaluator_applies(evaluator_cls)

    orig_evaluate_batch = pipeline.evaluate_metrics_batch
    if not getattr(orig_evaluate_batch, "_is_cancel_guarded", False):

        def _evaluate_batch_wrapper(
            detailed_results: list[dict[str, Any]],
            judge_model: Any,
            *,
            use_mcp: bool | None = None,
            _orig=orig_evaluate_batch,
        ) -> None:
            if is_cancelled():
                _log_cancel_once()
                return
            _orig(detailed_results, judge_model, use_mcp=use_mcp)

        _evaluate_batch_wrapper._is_cancel_guarded = True  # type: ignore[attr-defined]
        pipeline.evaluate_metrics_batch = _evaluate_batch_wrapper
        try:
            import devops_bench.metrics  # type: ignore[import-not-found,import-untyped]

            devops_bench.metrics.evaluate_metrics_batch = _evaluate_batch_wrapper
        except (ImportError, AttributeError):
            pass
