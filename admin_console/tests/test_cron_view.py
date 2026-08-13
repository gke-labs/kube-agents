from __future__ import annotations

import unittest
from datetime import UTC, datetime

from admin_console.agent_runtime import AgentCronExecution, AgentCronJob
from admin_console.cron_view import group_cron_executions


def job(profile: str, job_id: str) -> AgentCronJob:
    return AgentCronJob(
        profile,
        job_id,
        "Fleet inventory",
        True,
        "scheduled",
        "every 5m",
        "Inspect the fleet",
        "",
        "agent",
        None,
        None,
        "ok",
        "",
        "active",
        None,
    )


def execution(
    execution_id: str,
    profile: str,
    job_id: str,
    hour: int,
    status: str,
) -> AgentCronExecution:
    started_at = datetime(2026, 8, 13, hour, tzinfo=UTC)
    return AgentCronExecution(
        execution_id,
        profile,
        job_id,
        "cron",
        status,
        started_at,
        started_at,
        started_at,
        "",
    )


class CronExecutionGroupTest(unittest.TestCase):
    def test_same_title_across_profiles_and_job_ids_uses_one_group(self):
        jobs = {
            ("default", "job-a"): job("default", "job-a"),
            ("cluster-a", "job-b"): job("cluster-a", "job-b"),
        }
        executions = (
            execution("run-a", "default", "job-a", 10, "completed"),
            execution("run-b", "cluster-a", "job-b", 11, "failed"),
        )

        groups = group_cron_executions(executions, jobs)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].title, "Fleet inventory")
        self.assertEqual(groups[0].profiles, ("cluster-a", "default"))
        self.assertEqual(groups[0].latest.execution_id, "run-b")
        self.assertEqual(groups[0].succeeded, 1)
        self.assertEqual(groups[0].failed, 1)


if __name__ == "__main__":
    unittest.main()
