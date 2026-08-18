from __future__ import annotations

import pytest

from cuj.milestones import Milestone, MilestoneStatus, MilestoneSuite


def definitions() -> tuple[Milestone, ...]:
    return (
        Milestone("start", "journey starts", "successful root run"),
        Milestone("delegate", "work is delegated", "completed task", ("start",)),
        Milestone("evidence", "evidence is returned", "tool evidence", ("delegate",)),
    )


def test_records_passed_and_failed_milestones() -> None:
    suite = MilestoneSuite(definitions())
    suite.record("start", True, "completed")
    suite.record("delegate", True, ["task"])
    suite.record("evidence", False, [])

    assert [item.status for item in suite.results] == [
        MilestoneStatus.PASSED,
        MilestoneStatus.PASSED,
        MilestoneStatus.FAILED,
    ]
    assert suite.summary()["counts"] == {"passed": 2, "failed": 1, "blocked": 0}


def test_failed_dependency_blocks_downstream_milestones() -> None:
    suite = MilestoneSuite(definitions())
    suite.record("start", False, "transport failed")
    suite.record("delegate", False, [])
    suite.record("evidence", False, [])

    assert suite.results[0].status is MilestoneStatus.FAILED
    assert suite.results[1].status is MilestoneStatus.BLOCKED
    assert suite.results[1].blocked_by == ("start",)
    assert suite.results[2].status is MilestoneStatus.BLOCKED
    assert suite.summary()["counts"] == {"passed": 0, "failed": 1, "blocked": 2}


def test_requires_dependencies_to_be_recorded_first() -> None:
    suite = MilestoneSuite(definitions())

    with pytest.raises(ValueError, match="evaluated before dependencies"):
        suite.record("delegate", True, ["task"])
