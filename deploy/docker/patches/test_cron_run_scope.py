"""Unit tests for the cron-run scope installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import concurrent.futures
import contextvars
import os
import unittest

from cron_run_scope import (
    CRON_RESPONSE_LIMIT,
    CRON_RUN_ENV,
    WORKER_TASK_ENV,
    clip_cron_response,
    cron_ownership_violation,
    cron_run_scope,
    current_cron_job,
    missing_task_id_error,
    resolve_default_task_id,
)

# The card the kanban dispatcher scoped the worker to, and the job the worker
# then dispatched with cronjob(action='run') — the 2026-08-04 shape.
CALLER_CARD = "t_a1b2c3d4"
JOB_ID = "fleet-wide-cost-analysis"
DISPATCH_ENV = {WORKER_TASK_ENV: CALLER_CARD, CRON_RUN_ENV: JOB_ID}
WORKER_ENV = {WORKER_TASK_ENV: CALLER_CARD}


class CronRunScopeTest(unittest.TestCase):
    """The marker env var must be set for the run and gone afterwards."""

    def setUp(self):
        self.addCleanup(os.environ.pop, CRON_RUN_ENV, None)
        os.environ.pop(CRON_RUN_ENV, None)

    def test_the_marker_is_set_during_the_run_and_cleared_after(self):
        with cron_run_scope(JOB_ID):
            self.assertEqual(os.environ[CRON_RUN_ENV], JOB_ID)
        self.assertNotIn(CRON_RUN_ENV, os.environ)

    def test_the_marker_is_cleared_when_the_run_raises(self):
        with self.assertRaises(RuntimeError):
            with cron_run_scope(JOB_ID):
                raise RuntimeError("job blew up")
        self.assertNotIn(CRON_RUN_ENV, os.environ)

    def test_a_prior_value_is_restored_rather_than_dropped(self):
        os.environ[CRON_RUN_ENV] = "outer-job"
        with cron_run_scope(JOB_ID):
            self.assertEqual(os.environ[CRON_RUN_ENV], JOB_ID)
        self.assertEqual(os.environ[CRON_RUN_ENV], "outer-job")

    def test_the_workers_task_env_survives_the_scope(self):
        # heartbeat_current_worker_from_env() reads HERMES_KANBAN_TASK on every
        # call to hold the dispatcher's 15-minute claim; a 20-minute compliance
        # run would lose the card if the scope stripped it.
        os.environ[WORKER_TASK_ENV] = CALLER_CARD
        self.addCleanup(os.environ.pop, WORKER_TASK_ENV, None)
        with cron_run_scope(JOB_ID):
            self.assertEqual(os.environ[WORKER_TASK_ENV], CALLER_CARD)
        self.assertEqual(os.environ[WORKER_TASK_ENV], CALLER_CARD)

    def test_an_empty_job_id_still_marks_the_run(self):
        with cron_run_scope(""):
            self.assertTrue(os.environ[CRON_RUN_ENV])

    def test_the_marker_reaches_the_cron_agents_worker_thread(self):
        # cron/scheduler.py:run_job submits agent.run_conversation to a
        # single-worker pool through contextvars.copy_context(). Reproduce that
        # exact hop: the marker must survive it, or the kanban tools the cron
        # agent calls would not see it.
        with cron_run_scope(JOB_ID):
            context = contextvars.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                seen = pool.submit(context.run, current_cron_job).result()
        self.assertEqual(seen, JOB_ID)

    def test_the_marker_does_not_leak_into_a_sibling_context(self):
        # A context copied before the scope is what an unrelated concurrent
        # session holds; it must still resolve to no cron run.
        sibling = contextvars.copy_context()
        with cron_run_scope(JOB_ID):
            # environ={} isolates the context var from the process env, which
            # the scope also sets as the cross-process fallback.
            self.assertEqual(sibling.run(current_cron_job, {}), "")

    def test_the_context_var_wins_over_a_stale_env_value(self):
        os.environ[CRON_RUN_ENV] = "stale-job"
        with cron_run_scope(JOB_ID):
            self.assertEqual(current_cron_job(), JOB_ID)
        self.assertEqual(os.environ[CRON_RUN_ENV], "stale-job")

    def test_the_env_fallback_answers_when_no_context_var_is_set(self):
        # What a shelled-out `hermes kanban …` sees: a fresh process with the
        # inherited environment and no context.
        self.assertEqual(current_cron_job(DISPATCH_ENV), JOB_ID)
        self.assertEqual(current_cron_job({}), "")


class ResolveDefaultTaskIdTest(unittest.TestCase):
    """The ambient card must not be inherited by a cron run."""

    def test_an_explicit_argument_always_wins(self):
        self.assertEqual(
            resolve_default_task_id("t_other", DISPATCH_ENV), "t_other"
        )
        self.assertEqual(
            resolve_default_task_id("t_other", WORKER_ENV), "t_other"
        )

    def test_a_worker_still_falls_back_to_its_own_card(self):
        self.assertEqual(resolve_default_task_id(None, WORKER_ENV), CALLER_CARD)

    def test_a_cron_run_gets_no_fallback(self):
        # This is the defect: kanban_complete with no task_id resolved to the
        # caller's card and closed it out from under a blocked dispatcher.
        self.assertIsNone(resolve_default_task_id(None, DISPATCH_ENV))

    def test_no_env_at_all_resolves_to_nothing(self):
        self.assertIsNone(resolve_default_task_id(None, {}))
        self.assertIsNone(resolve_default_task_id("", {}))


class CronOwnershipViolationTest(unittest.TestCase):
    """A cron run naming its caller's card explicitly is rejected too."""

    def test_the_callers_card_is_refused(self):
        err = cron_ownership_violation(CALLER_CARD, DISPATCH_ENV)
        self.assertIsNotNone(err)
        self.assertIn(JOB_ID, err)
        self.assertIn(CALLER_CARD, err)

    def test_another_task_is_left_to_the_existing_rules(self):
        self.assertIsNone(cron_ownership_violation("t_sibling", DISPATCH_ENV))

    def test_a_worker_outside_a_cron_run_is_unaffected(self):
        self.assertIsNone(cron_ownership_violation(CALLER_CARD, WORKER_ENV))

    def test_a_cron_run_with_no_ambient_card_is_unaffected(self):
        self.assertIsNone(
            cron_ownership_violation(CALLER_CARD, {CRON_RUN_ENV: JOB_ID})
        )

    def test_a_missing_task_id_is_not_a_violation(self):
        # resolve_default_task_id already returned None; the handler's own
        # "task_id is required" guard owns that path.
        self.assertIsNone(cron_ownership_violation(None, DISPATCH_ENV))


class MissingTaskIdErrorTest(unittest.TestCase):
    """The stock advice is wrong inside a cron run — the env var is the trap."""

    def test_outside_a_cron_run_the_stock_message_is_unchanged(self):
        self.assertEqual(
            missing_task_id_error(WORKER_ENV),
            "task_id is required (or set HERMES_KANBAN_TASK in the env)",
        )
        self.assertIn("HERMES_KANBAN_TASK", missing_task_id_error({}))

    def test_inside_a_cron_run_it_names_the_job_and_points_at_the_response(self):
        msg = missing_task_id_error(DISPATCH_ENV)
        self.assertIn(JOB_ID, msg)
        self.assertIn("final response", msg)
        # Must not tell the run to set the very env var that caused the bug.
        self.assertNotIn("set HERMES_KANBAN_TASK", msg)


class ClipCronResponseTest(unittest.TestCase):
    """The report carried back must keep its ledger URL clickable."""

    LEDGER_URL = "https://github.com/gke-agentic/adamparco-infra/issues/27"

    def test_a_real_report_survives_whole(self):
        report = (
            "Fleet-wide cost analysis complete: 12 findings (3 major, 9 minor) "
            "across 3 clusters. Ledger updated at " + self.LEDGER_URL
        )
        self.assertEqual(clip_cron_response(report), report)

    def test_an_oversized_report_never_severs_the_url(self):
        filler = " ".join(f"finding{i}" for i in range(2000))
        clipped = clip_cron_response(filler + " " + self.LEDGER_URL)
        self.assertLessEqual(len(clipped), CRON_RESPONSE_LIMIT)
        self.assertNotIn("https://github.com/gke-agentic/adamparco-infra/is", clipped)

    def test_an_empty_report_is_an_empty_string(self):
        self.assertEqual(clip_cron_response(None), "")
        self.assertEqual(clip_cron_response("   "), "")

    def test_the_budget_is_roomier_than_the_chat_handoff(self):
        self.assertGreaterEqual(CRON_RESPONSE_LIMIT, 4000)


if __name__ == "__main__":
    unittest.main()
