"""Reusable dependency-aware milestone reporting for CUJ tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class MilestoneStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class Milestone:
    id: str
    requirement: str
    proof: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class MilestoneResult:
    milestone: Milestone
    status: MilestoneStatus
    observed: Any
    blocked_by: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is MilestoneStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.milestone.id,
            "status": self.status.value,
            "passed": self.passed,
            "requirement": self.milestone.requirement,
            "expected": self.milestone.proof,
            "observed": self.observed,
            "missingProof": list(self.blocked_by),
        }


class MilestoneSuite:
    def __init__(self, milestones: Iterable[Milestone]) -> None:
        ordered = tuple(milestones)
        self._milestones = {item.id: item for item in ordered}
        if len(self._milestones) != len(ordered):
            raise ValueError("milestone ids must be unique")
        for item in ordered:
            unknown = set(item.depends_on) - self._milestones.keys()
            if unknown:
                raise ValueError(
                    f"milestone {item.id} has unknown dependencies: {sorted(unknown)}"
                )
        self._order = tuple(item.id for item in ordered)
        self._results: dict[str, MilestoneResult] = {}

    def record(
        self,
        milestone_id: str,
        met: bool,
        observed: Any,
        *,
        blocked_by: tuple[str, ...] = (),
    ) -> None:
        if milestone_id in self._results:
            raise ValueError(f"milestone {milestone_id} was already recorded")
        try:
            milestone = self._milestones[milestone_id]
        except KeyError as exc:
            raise ValueError(f"unknown milestone {milestone_id}") from exc
        missing = [item for item in milestone.depends_on if item not in self._results]
        if missing:
            raise ValueError(
                f"milestone {milestone_id} evaluated before dependencies: {missing}"
            )
        dependency_blockers = tuple(
            item
            for item in milestone.depends_on
            if self._results[item].status is not MilestoneStatus.PASSED
        )
        blockers = tuple(dict.fromkeys((*dependency_blockers, *blocked_by)))
        status = (
            MilestoneStatus.PASSED
            if met and not blockers
            else MilestoneStatus.FAILED
        )
        self._results[milestone_id] = MilestoneResult(
            milestone,
            status,
            observed,
            blockers,
        )

    @property
    def results(self) -> tuple[MilestoneResult, ...]:
        missing = [item for item in self._order if item not in self._results]
        if missing:
            raise ValueError(f"milestones were not evaluated: {missing}")
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
                for status in MilestoneStatus
            },
            "milestones": [item.to_dict() for item in results],
        }

    def report_lines(self) -> list[str]:
        lines: list[str] = []
        for result in self.results:
            label = "PASS " if result.passed else "FAIL "
            lines.extend(
                [
                    f"{label}  {result.milestone.id} — {result.status.value.upper()}",
                    f"      CUJ requirement: {result.milestone.requirement}",
                    f"      Proof required: {result.milestone.proof}",
                    f"      Observed: {_concise(result.observed)}",
                ]
            )
            if result.blocked_by:
                lines.append(f"      Missing proof: {', '.join(result.blocked_by)}")
        return lines

    def failure_summary(self) -> str:
        return "; ".join(
            f"{item.milestone.id} ({item.status.value}): "
            f"{item.milestone.requirement}"
            for item in self.results
            if not item.passed
        )


def _concise(value: Any, limit: int = 240) -> str:
    rendered = json.dumps(value, sort_keys=True, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."
