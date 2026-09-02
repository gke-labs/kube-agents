"""Unit tests for the cron-run scope installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import concurrent.futures
import contextvars
import os
import tempfile
import threading
import unittest
from pathlib import Path

import apply_cron_run_scope
from cron_run_scope import (
    CRON_RESPONSE_LIMIT,
    CRON_RUN_ENV,
    WORKER_TASK_ENV,
    clip_cron_response,
    cron_ownership_violation,
    cron_run_scope,
    current_cron_job,
    missing_task_id_error,
)

# The card the kanban dispatcher scoped the worker to, and the job the worker
# then dispatched with cronjob(action='run') — the 2026-08-04 shape.
CALLER_CARD = "t_a1b2c3d4"
JOB_ID = "fleet-wide-cost-analysis"
DISPATCH_ENV = {WORKER_TASK_ENV: CALLER_CARD, CRON_RUN_ENV: JOB_ID}
WORKER_ENV = {WORKER_TASK_ENV: CALLER_CARD}


class CronRunScopeTest(unittest.TestCase):
    """The marker must cover the run, and reach nothing else."""

    def setUp(self):
        self.addCleanup(os.environ.pop, CRON_RUN_ENV, None)
        os.environ.pop(CRON_RUN_ENV, None)

    def test_the_marker_is_set_during_the_run_and_cleared_after(self):
        self.assertEqual(current_cron_job(), "")
        with cron_run_scope(JOB_ID):
            self.assertEqual(current_cron_job(), JOB_ID)
        self.assertEqual(current_cron_job(), "")

    def test_the_marker_is_cleared_when_the_run_raises(self):
        with self.assertRaises(RuntimeError):
            with cron_run_scope(JOB_ID):
                raise RuntimeError("job blew up")
        self.assertEqual(current_cron_job(), "")

    def test_the_scope_does_not_touch_the_environment(self):
        """The env half is gone, and its absence is the fix.

        os.environ is process-global; a cron run is not. Writing the marker
        there bled it into unrelated threads, was inherited for life by workers
        the dispatcher spawned mid-run, and — see the next test — leaked
        permanently under a non-nested interleaving.
        """
        with cron_run_scope(JOB_ID):
            self.assertNotIn(CRON_RUN_ENV, os.environ)
        self.assertNotIn(CRON_RUN_ENV, os.environ)

    def test_two_overlapping_runs_leave_nothing_behind(self):
        """A in, B in, A out, B out — the interleaving that used to wedge.

        Two `cronjob(action='run')` calls in one assistant turn reach this:
        agent/tool_executor.py runs tool calls on a pool and `cronjob` is not on
        its serial list. With the save/restore env pair, A popped a variable it
        never set and B then restored A's marker with no run active at all —
        after which every worker in the process was told it was a run of job A,
        so it could neither default to its own card nor name it explicitly, and
        the guardrail backstop was suppressed too. The card sat `running` with
        no result and no failure, forever.

        Held in a ContextVar the overlap is structurally impossible: each pool
        thread starts from its own context, so neither scope can observe or
        restore the other's value. Interleaved with real barriers rather than
        by calling __enter__/__exit__ by hand — out-of-order resets in ONE
        context would leak a ContextVar too, and `with` is what rules that out.
        """
        a_in, b_in, a_out = (threading.Event() for _ in range(3))
        seen = {}

        def run_a():
            with cron_run_scope("job-a"):
                a_in.set()
                b_in.wait(5)
                seen["a"] = current_cron_job()
            a_out.set()

        def run_b():
            a_in.wait(5)
            with cron_run_scope("job-b"):
                b_in.set()
                a_out.wait(5)
                # A has fully exited by now and must not have disturbed B.
                seen["b"] = current_cron_job()

        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(seen, {"a": "job-a", "b": "job-b"})
        self.assertEqual(current_cron_job(), "")
        self.assertNotIn(CRON_RUN_ENV, os.environ)

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
            self.assertTrue(current_cron_job())

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
        """An unrelated concurrent session must still resolve to no cron run.

        Reads the REAL os.environ, not an empty stand-in. Passing environ={}
        here was the whole defect: it isolated the assertion from the
        process-global half that was doing the leaking, so the test named after
        the leak could not observe it.
        """
        sibling = contextvars.copy_context()
        with cron_run_scope(JOB_ID):
            self.assertEqual(sibling.run(current_cron_job), "")

    def test_a_thread_outside_the_scope_sees_no_run(self):
        """The same claim across a real thread, which is how it happened.

        Under dispatch_in_gateway a second worker runs concurrently with
        somebody else's dispatch. It never entered the scope, so it must not
        read the marker — and while the marker lived in os.environ, it did.
        """
        with cron_run_scope(JOB_ID):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                # No copy_context(): a bare pool thread starts from an empty
                # context, exactly like an unrelated worker.
                seen = pool.submit(current_cron_job).result()
        self.assertEqual(seen, "")

    def test_the_context_var_wins_over_a_stale_env_value(self):
        os.environ[CRON_RUN_ENV] = "stale-job"
        with cron_run_scope(JOB_ID):
            self.assertEqual(current_cron_job(), JOB_ID)
        self.assertEqual(os.environ[CRON_RUN_ENV], "stale-job")

    def test_the_env_fallback_still_answers_when_something_sets_it(self):
        # Nothing in the image writes it today. Kept because it is the one
        # thing a ContextVar cannot do — cross a fork — so a process launched
        # with the marker already in its environment is still recognised.
        self.assertEqual(current_cron_job(DISPATCH_ENV), JOB_ID)
        self.assertEqual(current_cron_job({}), "")


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
        # _default_task_id already returned None; the handler's own
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


# The applier is exercised against a miniature of the three real files. The
# real anchors are asserted against the shipped image by
# verify_cron_run_scope.py; what these miniatures carry is the SHAPE the
# v2026.8.19 split introduced, which is the thing a version bump moves.
#
# The wrapper/body split is the whole reason this test exists. `run_one_job`
# stopped being the execute→deliver→mark body and became a wrapper that
# delegates through `_run_with_fire_claim_heartbeat`; putting the out-param on
# the wrapper alone leaves both anchors matched, the file parsing, and the body
# raising NameError on the first real tick.
SCHEDULER_STUB = '''from typing import Optional


def run_one_job(
    job: dict,
    *,
    adapters=None,
    loop=None,
    verbose: bool = False,
    extra_prompt: Optional[str] = None,
    cancel_event=None,
) -> bool:
    """Register the fire owner, then delegate."""
    execution_token = object()
    try:
        return _run_with_fire_claim_heartbeat(
            job,
            lambda lost_ownership: _run_one_job_body(
                job,
                adapters=adapters,
                loop=loop,
                verbose=verbose,
                extra_prompt=extra_prompt,
                fire_claim_lost=lost_ownership,
                execution_token=execution_token,
            ),
        )
    finally:
        pass


def _run_one_job_body(
    job: dict,
    *,
    adapters=None,
    loop=None,
    verbose: bool = False,
    extra_prompt: Optional[str] = None,
    fire_claim_lost=None,
    execution_token=None,
) -> bool:
    try:
        output = execute_job(job)
        if output:
            output_file = save_job_output(job["id"], output)
        finish_execution(
            execution_id,
            success=success,
            error=error,
            delivery_outcome=delivery_outcome,
        )
        return True
    except Exception:
        finish_execution(execution_id, success=False)
        return False
'''

CRONJOB_STUB = '''import json
from typing import Any, Dict


def _notify_provider_jobs_changed_safe() -> None:
    pass


def _run_claimed_job(job, job_id, adapters, gateway_loop, extra_prompt):
    with heartbeat:
        try:
            try:
                processed = run_one_job(
                    job, adapters=adapters, loop=gateway_loop,
                    extra_prompt=extra_prompt,
                )
            finally:
                unregister_running_job(job_id)
        finally:
            stop_heartbeat()
        refreshed = get_job(job_id) or {}
        ok = refreshed.get("last_status") == "ok"
        return {
            "claimed": True,
            "success": bool(processed and ok),
            "error": refreshed.get("last_error"),
        }


def cronjob(action, result, exec_result):
    if action == "run":
        if exec_result is not None:
            if exec_result.get("skipped"):
                result["skipped"] = True
            elif exec_result.get("error"):
                result["execution_error"] = exec_result["error"]
            return json.dumps({"success": True, "job": result}, indent=2)
'''

KANBAN_STUB = '''import os

from hermes_cli.config import cfg_get, load_config


def _task_scope_error(tid):
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    return None


def kanban_complete(task_id=None):
    if not task_id:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK in the env)")


def kanban_block(task_id=None):
    if not task_id:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK in the env)")
'''


class ApplierTest(unittest.TestCase):
    """The applier against the v2026.8.19 wrapper/body shape."""

    def _apply(self):
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(SCHEDULER_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        apply_cron_run_scope.apply(root)
        return (root / "cron" / "scheduler.py").read_text()

    @staticmethod
    def _keyword_only(source, name):
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return [arg.arg for arg in node.args.kwonlyargs]
        raise AssertionError(f"no def {name} in the patched source")

    def test_both_halves_take_the_out_param(self):
        """The wrapper is what callers name; the body is what the writes read."""
        scheduler = self._apply()
        ast.parse(scheduler)
        self.assertIn("outcome", self._keyword_only(scheduler, "run_one_job"))
        self.assertIn("outcome", self._keyword_only(scheduler, "_run_one_job_body"))

    def test_the_wrapper_forwards_it_across_the_split(self):
        """Both signatures can carry it and the body still see None.

        Nothing else notices: expect_keyword_only checks the body's parameter
        list, not its callers, and the file parses either way.
        """
        scheduler = self._apply()
        delegation = scheduler[scheduler.index("_run_one_job_body(") :]
        delegation = delegation[: delegation.index(")\n")]
        self.assertIn("outcome=outcome,", delegation)

    def test_the_writes_land_in_the_body_not_the_wrapper(self):
        scheduler = self._apply()
        body = scheduler[scheduler.index("def _run_one_job_body(") :]
        self.assertIn('outcome["response"] = final_response', body)
        self.assertIn('outcome["output_file"] = str(output_file)', body)

    def test_a_wrapper_that_stopped_delegating_is_fatal_not_silent(self):
        """The shape that shipped the NameError: no lambda to forward through."""
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(
            SCHEDULER_STUB.replace(
                "lambda lost_ownership: _run_one_job_body(", "_run_one_job_body("
            )
        )
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        with self.assertRaises(SystemExit) as ctx:
            apply_cron_run_scope.apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        """Deliberately not idempotent: the out-param is inserted, not
        substituted, so a second pass would append a second `outcome=None`."""
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(SCHEDULER_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        apply_cron_run_scope.apply(root)
        with self.assertRaises(SystemExit):
            apply_cron_run_scope.apply(root)


if __name__ == "__main__":
    unittest.main()
