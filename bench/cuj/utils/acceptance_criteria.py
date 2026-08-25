"""Backend-independent acceptance criteria for live CUJ scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class AcceptanceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    requirement: str
    proof: str


@dataclass(frozen=True)
class AcceptanceResult:
    criterion: AcceptanceCriterion
    status: AcceptanceStatus
    observed: Any
    blocked_by: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is AcceptanceStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.criterion.id,
            "status": self.status.value,
            "passed": self.passed,
            "requirement": self.criterion.requirement,
            "expected": self.criterion.proof,
            "observed": self.observed,
            "missingProof": list(self.blocked_by),
        }


class AcceptanceCriteria:
    def __init__(self, criteria: Iterable[AcceptanceCriterion]) -> None:
        ordered = tuple(criteria)
        self._criteria = {item.id: item for item in ordered}
        if not ordered:
            raise ValueError("at least one acceptance criterion is required")
        if len(self._criteria) != len(ordered):
            raise ValueError("acceptance criterion ids must be unique")
        self._order = tuple(item.id for item in ordered)
        self._results: dict[str, AcceptanceResult] = {}

    def record(
        self,
        criterion_id: str,
        met: bool,
        observed: Any,
        *,
        blocked_by: tuple[str, ...] = (),
    ) -> None:
        if criterion_id in self._results:
            raise ValueError(f"acceptance criterion {criterion_id} was already recorded")
        try:
            criterion = self._criteria[criterion_id]
        except KeyError as exc:
            raise ValueError(f"unknown acceptance criterion {criterion_id}") from exc
        status = (
            AcceptanceStatus.PASSED
            if met and not blocked_by
            else AcceptanceStatus.FAILED
        )
        self._results[criterion_id] = AcceptanceResult(
            criterion,
            status,
            observed,
            tuple(dict.fromkeys(blocked_by)),
        )

    @property
    def results(self) -> tuple[AcceptanceResult, ...]:
        missing = [item for item in self._order if item not in self._results]
        if missing:
            raise ValueError(f"acceptance criteria were not evaluated: {missing}")
        return tuple(self._results[item] for item in self._order)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.results)

    def summary(self) -> dict[str, Any]:
        results = self.results
        return {
            "passed": self.passed,
            "counts": {
                status.value: sum(item.status is status for item in results)
                for status in AcceptanceStatus
            },
            "criteria": [item.to_dict() for item in results],
        }

    def report_lines(self) -> list[str]:
        lines: list[str] = []
        for result in self.results:
            label = "PASS " if result.passed else "FAIL "
            lines.extend(
                [
                    f"{label}  {result.criterion.id} — {result.status.value.upper()}",
                    f"      Acceptance criterion: {result.criterion.requirement}",
                    f"      Proof required: {result.criterion.proof}",
                    f"      Observed: {_concise(result.observed)}",
                ]
            )
            if result.blocked_by:
                lines.append(f"      Missing proof: {', '.join(result.blocked_by)}")
        return lines

    def failure_summary(self) -> str:
        failures: list[str] = []
        for item in self.results:
            if item.passed:
                continue
            reason = (
                f"; missing proof: {', '.join(item.blocked_by)}"
                if item.blocked_by
                else ""
            )
            failures.append(
                f"{item.criterion.id} ({item.status.value}{reason}): "
                f"{item.criterion.requirement}"
            )
        return "; ".join(failures)


def _concise(value: Any, limit: int = 240) -> str:
    rendered = json.dumps(value, sort_keys=True, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."
