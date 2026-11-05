"""Unit tests for the cron-run scope installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import concurrent.futures
import contextlib
import contextvars
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import apply_cron_run_scope
import cron_job_template
import kanban_origin
import cron_run_scope as cron_run_scope_module
from cron_run_scope import (
    CRON_RESPONSE_LIMIT,
    CRON_RISK_ENV,
    CRON_RUN_ENV,
    WORKER_TASK_ENV,
    clip_cron_response,
    cron_ownership_violation,
    cron_run_scope,
    current_cron_job,
    current_cron_origin,
    current_cron_risk,
    missing_task_id_error,
)

# The card the kanban dispatcher scoped the worker to, and the job the worker
# then dispatched with cronjob(action='run') — the 2026-08-04 shape.
CALLER_CARD = "t_a1b2c3d4"
JOB_ID = "fleet-wide-cost-analysis"
DISPATCH_ENV = {WORKER_TASK_ENV: CALLER_CARD, CRON_RUN_ENV: JOB_ID}
WORKER_ENV = {WORKER_TASK_ENV: CALLER_CARD}
# A shipped fleet-audit job, as the template and the tick's claimed record carry it.
TICK_JOB = {"prompt": "Run the fleet cost audit.", "skills": ["fleet-audit"]}


class CronRunScopeTest(unittest.TestCase):
    """The marker must cover the run, and reach nothing else."""

    def setUp(self):
        self.addCleanup(os.environ.pop, CRON_RUN_ENV, None)
        self.addCleanup(os.environ.pop, CRON_RISK_ENV, None)
        os.environ.pop(CRON_RUN_ENV, None)
        os.environ.pop(CRON_RISK_ENV, None)

    def test_the_marker_is_set_during_the_run_and_cleared_after(self):
        self.assertEqual(current_cron_job(), "")
        self.assertEqual(current_cron_risk(), "high")
        with cron_run_scope(JOB_ID, risk="low"):
            self.assertEqual(current_cron_job(), JOB_ID)
            self.assertEqual(current_cron_risk(), "low")
        self.assertEqual(current_cron_job(), "")
        self.assertEqual(current_cron_risk(), "high")

    def test_default_risk_is_high(self):
        with cron_run_scope(JOB_ID):
            self.assertEqual(current_cron_risk(), "high")

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


# The applier is exercised against a miniature of the four real files. The
# real anchors are asserted against the shipped image by
# verify_cron_run_scope.py; what these miniatures carry is the SHAPE upstream
# has at v2026.9.14, which is the thing a version bump moves.
#
# The wrapper/body split (v2026.8.19) is the whole reason this test exists.
# `run_one_job` stopped being the execute→deliver→mark body and became a wrapper
# that delegates through `_run_with_fire_claim_heartbeat`; putting the out-param
# on the wrapper alone leaves both anchors matched, the file parsing, and the
# body raising NameError on the first real tick. v2026.9.14 then split the body
# itself: save/deliver moved into `_save_compose_deliver` over a `_RunDelivery`
# record and the bookkeeping tail into `_finish_completed_run`, so the report is
# written from the seam between them and the saved path rides on the record.
def _finish_execution(execution_id, success=True, error=None):
    """The scheduler stub's ledger close: a failed run fails the test where it happened.

    The stub's body, like upstream's, swallows any exception into
    ``finish_execution(..., success=False)``, so a stub that breaks inside
    ``_save_compose_deliver`` would otherwise pass every origin check.
    """
    if not success:
        raise AssertionError(f"the stub run {execution_id} failed inside the scheduler body: {error!r}")


SCHEDULER_STUB = '''import contextlib
from dataclasses import dataclass
from typing import Optional


def run_one_job(
    job: dict, *, adapters=None, loop=None, verbose: bool = False,
    extra_prompt: Optional[str] = None, cancel_event=None,
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
                execution_token=execution_token))
    finally:
        pass


class _FireOwnership:
    @contextlib.contextmanager
    def side_effect_fence(self):
        yield True


@dataclass
class _RunDelivery:
    job: dict
    success: bool
    error: Optional[str]
    delivery_error: Optional[str] = None
    side_effect_ownership_lost: bool = False


def _save_compose_deliver(d, fence, final_response, output, *, adapters, loop, verbose, execution_token):
    job = d.job
    with fence.side_effect_fence() as owns_output:
        if not owns_output:
            raise RuntimeError
        output_file = save_job_output(job["id"], output)
    if verbose:
        logger.info("Output saved to: %s", output_file)
    d.delivery_error = None


def _finish_completed_run(d, fire_owner, execution_id) -> bool:
    finish_execution(execution_id, success=d.success, error=d.error)
    return True


def _run_one_job_body(
    job: dict, *, adapters=None, loop=None, verbose: bool = False,
    extra_prompt: Optional[str] = None, fire_claim_lost=None,
    execution_token: Optional[object] = None,
) -> bool:
    execution_id = "x"
    delivery_error = None
    try:
        _deferred_agents: list = []
        _run_kwargs = {
            "defer_agent_teardown": _deferred_agents,
            "extra_prompt": extra_prompt,
            "execution_id": execution_id}
        if fire_claim_lost is not None:
            _run_kwargs["cancel_event"] = fire_claim_lost
        try:
            success, output, final_response, error = run_job(job, **_run_kwargs)
        except BaseException:
            raise
        fence = _FireOwnership()
        d = _RunDelivery(job=job, success=success, error=error)
        try:
            _save_compose_deliver(
                d, fence, final_response, output, adapters=adapters, loop=loop, verbose=verbose,
                execution_token=execution_token)
        finally:
            delivery_error = d.delivery_error
        fire_owner = None
        return _finish_completed_run(d, fire_owner, execution_id)
    except Exception as exc:
        finish_execution(execution_id, success=False, error=exc)
        return False


def _process_due_job(job: dict, adapters, loop, verbose: bool) -> bool:
    """Run one due job via the shared ``run_one_job`` body."""
    claimed = claim_job_for_fire(job["id"], return_job=True)
    if not claimed:
        finish_execution(
            job["execution_id"], success=False, error="Fire claim lost; execution was not started.")
        return True
    claimed_job = dict(claimed) if isinstance(claimed, dict) else dict(job)
    claimed_job["execution_id"] = job["execution_id"]
    claimed_job["_scheduled_instant"] = job.get("_scheduled_instant")
    return run_one_job(claimed_job, adapters=adapters, loop=loop, verbose=verbose)
'''

CRONJOB_STUB = '''import json
from typing import Any, Dict, Optional


def _dumps(payload):
    return json.dumps(payload, indent=2)


def _notify_provider_jobs_changed_safe() -> None:
    pass


def _run_claimed_job(job, extra_prompt=None):
    job_id = job["id"]
    try:
        from cron.scheduler import release_running_job, run_one_job, try_register_running_job
        try:
            with _run_heartbeat(str(job.get("name") or job_id)):
                processed = run_one_job(job, adapters=adapters, loop=gateway_loop, extra_prompt=extra_prompt)
        finally:
            release_running_job(job_id)
        refreshed = get_job(job_id) or {}
        ok = refreshed.get("last_status") == "ok"
        run_error = refreshed.get("last_error")
        return {"claimed": True, "success": bool(processed and ok), "error": run_error}
    except Exception as e:
        return {"claimed": True, "success": False, "error": str(e)}


def _action_create(a):
    try:
        job = create_job_with_scheduler_registration(
            prompt=a["prompt"] or "",
            reasoning_effort=a["reasoning_effort"],
            failure_deliver=a["failure_deliver"])
    except CronSchedulerRegistrationError as exc:
        _partial = exc.to_dict()
        return tool_error(_partial.pop("error"), success=False, **_partial)
    return _dumps({"success": True, "job_id": job["id"]})


def _action_update(job, a):
    updates = {"prompt": a["prompt"]}
    updated = update_job(job["id"], updates)
    _notify_provider_jobs_changed_safe()
    return _dumps({"success": True, "job": updated})


def _action_run(job: Dict[str, Any], a: Dict[str, Any]) -> str:
    exec_result = a["exec_result"]
    if exec_result is None:
        exec_result = _run_claimed_job(job)
    result = a["result"] or {}
    claimed = exec_result.get("claimed", False)
    if not claimed:
        result["execution_skipped"] = exec_result.get("error")
    elif exec_result.get("error"):
        result["execution_error"] = exec_result["error"]
    return _dumps({"success": True, "job": result})


def cronjob(
    action: str,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    failure_deliver: Optional[str] = None,
    result: Optional[dict] = None,
    exec_result: Optional[dict] = None,
    task_id: str = None,
    session_id: Optional[str] = None,
    paused: bool = False) -> str:
    a = dict(locals())
    del a["task_id"]
    if action == "create":
        return _action_create(a)
    if action == "run":
        return _action_run({"id": job_id}, a)
'''

JOBS_STUB = '''from typing import Any, Dict, Optional


def create_job(
    name: str,
    schedule: str,
    prompt: str,
    paused: bool = False,
    paused_reason: Optional[str] = None,
) -> Dict[str, Any]:
    job = {"name": name, "schedule": schedule, "prompt": prompt}
    with _jobs_lock():
        save_jobs(load_jobs() + [job])
    return job
'''

KANBAN_STUB = '''import os

from hermes_cli.config import cfg_get, load_config


class _Reject(Exception):
    pass


def _check(cond, message):
    if not cond:
        raise _Reject(message)


def _default_task_id(arg):
    return arg or os.environ.get("HERMES_KANBAN_TASK") or None


def _require_task_id(args: dict) -> str:
    tid = _default_task_id(args.get("task_id"))
    _check(tid, "task_id is required (or set HERMES_KANBAN_TASK in the env)")
    return tid


def _enforce_worker_task_ownership(tid: str) -> None:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and tid != env_tid:
        raise _Reject(
            f"worker is scoped to task {env_tid}; refusing to mutate {tid}.")
'''


class ApplierTest(unittest.TestCase):
    """The applier against the v2026.9.14 wrapper/body/phase-helper shape."""

    def _apply_all(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(SCHEDULER_STUB)
        (root / "cron" / "jobs.py").write_text(JOBS_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        apply_cron_run_scope.apply(root)
        return root

    def _apply(self) -> str:
        return (self._apply_all() / "cron" / "scheduler.py").read_text()

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
        self.assertIn('outcome["output_file"] = d.output_file', body)
        wrapper = scheduler[: scheduler.index("@dataclass")]
        self.assertNotIn("outcome[", wrapper)

    def test_the_saved_path_rides_the_delivery_record(self):
        """save_job_output moved out of the body in v2026.9.14; the path comes
        back on _RunDelivery, so the record must carry the field and the save
        phase must fill it as a string."""
        scheduler = self._apply()
        record = scheduler[scheduler.index("class _RunDelivery") :]
        record = record[: record.index("def _save_compose_deliver")]
        self.assertIn("output_file: Optional[str] = None", record)
        save = scheduler[scheduler.index("def _save_compose_deliver") :]
        save = save[: save.index("def _finish_completed_run")]
        self.assertIn("d.output_file = str(output_file)", save)

    def test_the_report_is_written_before_the_bookkeeping_tail(self):
        scheduler = self._apply()
        body = scheduler[scheduler.index("def _run_one_job_body(") :]
        self.assertLess(
            body.index('outcome["response"] = final_response'),
            body.index("return _finish_completed_run(d, fire_owner, execution_id)"),
        )

    def test_the_scoped_run_job_lands_in_the_body(self):
        scheduler = self._apply()
        body = scheduler[scheduler.index("def _run_one_job_body(") :]
        self.assertIn('with cron_run_scope(job["id"], risk=str(job.get("risk") or "high")):', body)

    def test_cronjob_create_accepts_risk_param(self):
        root = self._apply_all()
        cronjob = (root / "tools" / "cronjob_tools.py").read_text()
        ast.parse(cronjob)
        self.assertIn("risk: Optional[str] = None,", cronjob)
        self.assertIn('risk=a["risk"],', cronjob)

    def test_jobs_create_job_stamps_default_risk(self):
        root = self._apply_all()
        jobs = (root / "cron" / "jobs.py").read_text()
        ast.parse(jobs)
        self.assertIn("risk: Optional[str] = None,", jobs)
        self.assertIn('job["risk"] = _eff_risk', jobs)

    def test_jobs_create_job_clamps_and_validates_risk(self):
        import contextlib
        from typing import Any, Dict, Optional
        root = self._apply_all()
        jobs_code = (root / "cron" / "jobs.py").read_text()
        stored_jobs = []
        ns = {
            "load_jobs": lambda: stored_jobs,
            "save_jobs": lambda js: None,
            "_jobs_lock": contextlib.nullcontext,
            "Optional": Optional,
            "Dict": Dict,
            "Any": Any,
        }
        exec(jobs_code, ns)
        create_job = ns["create_job"]

        # Default is low
        j1 = create_job("test1", "* * * * *", "echo 1")
        self.assertEqual(j1["risk"], "low")

        # Explicit low is low
        j2 = create_job("test2", "* * * * *", "echo 2", risk="low")
        self.assertEqual(j2["risk"], "low")

        # Explicit high is high
        j3 = create_job("test3", "* * * * *", "echo 3", risk="high")
        self.assertEqual(j3["risk"], "high")

        # Invalid string fails closed to high
        j4 = create_job("test4", "* * * * *", "echo 4", risk="banana")
        self.assertEqual(j4["risk"], "high")

        # Inside high-risk cron run, low and invalid risk are clamped to high
        with cron_run_scope("watchdog-1", risk="high"):
            j5 = create_job("test5", "* * * * *", "echo 5", risk="low")
            self.assertEqual(j5["risk"], "high")
            j6 = create_job("test6", "* * * * *", "echo 6", risk="banana")
            self.assertEqual(j6["risk"], "high")

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
        (root / "cron" / "jobs.py").write_text(JOBS_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        with self.assertRaises(SystemExit) as ctx:
            apply_cron_run_scope.apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def _patched_namespace(self, root: Path, relative: str, **names) -> dict:
        """Exec one patched stub with the ``tools`` package aliased to these modules."""
        tools = types.ModuleType("tools")
        tools.__path__ = []
        modules = {
            "tools": tools,
            "tools.cron_run_scope": cron_run_scope_module,
            "tools.cron_job_template": cron_job_template,
        }
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        ns = {"__name__": f"patched_{relative.replace('/', '_')}", "logger": mock.Mock()}
        ns.update(names)
        exec(compile((root / relative).read_text(), relative, "exec"), ns)
        return ns

    def _scheduler(self, root: Path, seen: list, inside_run=None) -> dict:
        """The patched scheduler stub, whose ``run_job`` records the run's context.

        ``inside_run`` runs first inside ``run_job``, the way Hermes' own cron
        scope rebinds the session before the agent starts.
        """

        def run_job(job, **_kwargs):
            if inside_run is not None:
                inside_run()
            seen.append((current_cron_job(), current_cron_origin()))
            return True, "output", "final", None

        return self._patched_namespace(
            root,
            "cron/scheduler.py",
            run_job=run_job,
            _run_with_fire_claim_heartbeat=lambda job, run: run(None),
            claim_job_for_fire=lambda job_id, return_job=False: {"id": job_id, **TICK_JOB},
            finish_execution=_finish_execution,
            save_job_output=lambda job_id, output: Path("/tmp/out.md"),
        )

    def _template(self) -> None:
        template = Path(tempfile.mkdtemp()) / "jobs.json"
        template.write_text(json.dumps({"jobs": [{"id": JOB_ID, **TICK_JOB}]}))
        for patcher in (
            mock.patch.object(cron_job_template, "TEMPLATE_JOBS_PATH", str(template)),
            mock.patch.object(cron_job_template, "SESSION_KV_URL", "http://127.0.0.1:9"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_tick_run_sets_the_run_origin_for_its_scope(self):
        """The scheduler's tick (``_process_due_job``) sets the origin around the run, and only there."""
        self._template()
        seen: list = []
        ns = self._scheduler(self._apply_all(), seen)
        self.assertTrue(ns["_process_due_job"]({"id": JOB_ID, "execution_id": "e1"}, None, None, False))
        self.assertEqual(seen, [(JOB_ID, ("audit", ""))])
        self.assertIsNone(current_cron_origin())
        # A job that no longer matches the template and has no record is other.
        seen.clear()
        ns["claim_job_for_fire"] = lambda job_id, return_job=False: {"id": job_id, **TICK_JOB, "prompt": "edited"}
        ns["_process_due_job"]({"id": JOB_ID, "execution_id": "e2"}, None, None, False)
        self.assertEqual(seen, [(JOB_ID, ("other", ""))])
        self.assertIsNone(current_cron_origin())

    def _direct_tool(self, root: Path, seen: list, inside_run=None) -> dict:
        """The patched cronjob tool, over the patched scheduler stub."""
        scheduler = self._scheduler(root, seen, inside_run)
        fake_scheduler = types.ModuleType("cron.scheduler")
        fake_scheduler.run_one_job = scheduler["run_one_job"]
        fake_scheduler.release_running_job = lambda job_id: None
        fake_scheduler.try_register_running_job = lambda job_id: True
        cron_package = types.ModuleType("cron")
        cron_package.__path__ = []
        cron_package.scheduler = fake_scheduler
        patcher = mock.patch.dict(sys.modules, {"cron": cron_package, "cron.scheduler": fake_scheduler})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self._patched_namespace(
            root,
            "tools/cronjob_tools.py",
            _run_heartbeat=lambda name: contextlib.nullcontext(),
            adapters=None,
            gateway_loop=None,
            get_job=lambda job_id: {"last_status": "ok"},
        )

    def _asking_turn(self, records: dict) -> contextvars.ContextVar:
        """Bind ``kanban_origin``'s turn to a session ContextVar and these session records."""
        session: contextvars.ContextVar = contextvars.ContextVar("asking_session", default="")
        kanban_origin._ORIGIN_CACHE.clear()
        self.addCleanup(kanban_origin._ORIGIN_CACHE.clear)
        for patcher in (
            mock.patch.object(kanban_origin, "_session_id", lambda: session.get()),
            mock.patch.object(kanban_origin, "_record_origin", lambda sid: records.get(sid)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return session

    def test_a_direct_run_resolves_as_the_session_that_asked_for_it(self):
        """``cronjob(action='run')`` from a chat turn: the run reads the asking turn's origin.

        Inside the run Hermes rebinds the session to the cron agent's own
        ``cron_<job>_<ts>`` id, which has no record, so the origin must be
        captured before the run starts.
        """
        self._template()
        session = self._asking_turn({"sess-chat": ("chat", "slack")})
        seen: list = []
        tool = self._direct_tool(
            self._apply_all(), seen, inside_run=lambda: session.set(f"cron_{JOB_ID}_1"))
        session.set("sess-chat")
        raw = tool["cronjob"]("run", job_id=JOB_ID)
        self.assertTrue(json.loads(raw)["success"], raw)
        self.assertEqual(json.loads(raw)["job"].get("output_file"), "/tmp/out.md", "the run reached the body's tail")
        self.assertEqual(seen, [(JOB_ID, ("chat", "slack"))])
        self.assertIsNone(current_cron_origin())

    def test_a_direct_run_inside_a_kanban_worker_resolves_as_its_card(self):
        """A worker's turn has no session record; its card's stamp is the asking origin."""
        self._template()
        self._asking_turn({})
        seen: list = []
        tool = self._direct_tool(self._apply_all(), seen)
        with mock.patch.object(kanban_origin, "card_origin", return_value=("chat", "slack")):
            tool["cronjob"]("run", job_id=JOB_ID)
        self.assertEqual(seen, [(JOB_ID, ("chat", "slack"))])

    def test_a_direct_run_from_an_unresolved_turn_is_other_not_the_template(self):
        """An unresolved asking turn runs as ``other``, never as the tick's ``audit``."""
        self._template()
        self._asking_turn({})
        seen: list = []
        tool = self._direct_tool(self._apply_all(), seen)
        tool["cronjob"]("run", job_id=JOB_ID)
        self.assertEqual(seen, [(JOB_ID, ("other", ""))])

    def test_a_direct_run_dispatched_to_a_pool_keeps_the_asking_origin(self):
        """The background path: the pool copies the context at submit, as Hermes' does."""
        session = self._asking_turn({"sess-chat": ("chat", "slack")})
        seen: list = []
        tool = self._direct_tool(self._apply_all(), seen)
        run = tool["_run_claimed_job"]

        def background(job):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(contextvars.copy_context().run, run, job).result()

        tool["_run_claimed_job"] = background
        session.set("sess-chat")
        tool["cronjob"]("run", job_id=JOB_ID)
        self.assertEqual(seen, [(JOB_ID, ("chat", "slack"))])

    def test_a_direct_run_sets_no_run_origin(self):
        """``run_one_job`` outside the run action's scope runs under the run scope, with no origin."""
        self._template()
        seen: list = []
        root = self._apply_all()
        scheduler = self._scheduler(root, seen)
        scheduler["run_one_job"]({"id": JOB_ID, **TICK_JOB})
        self.assertEqual(seen, [(JOB_ID, None)])
        # Through the patched tool's claimed-run body too.
        seen.clear()
        fake_scheduler = types.ModuleType("cron.scheduler")
        fake_scheduler.run_one_job = scheduler["run_one_job"]
        fake_scheduler.release_running_job = lambda job_id: None
        fake_scheduler.try_register_running_job = lambda job_id: True
        cron_package = types.ModuleType("cron")
        cron_package.__path__ = []
        cron_package.scheduler = fake_scheduler
        patcher = mock.patch.dict(sys.modules, {"cron": cron_package, "cron.scheduler": fake_scheduler})
        patcher.start()
        self.addCleanup(patcher.stop)
        tool = self._patched_namespace(
            root,
            "tools/cronjob_tools.py",
            _run_heartbeat=lambda name: contextlib.nullcontext(),
            adapters=None,
            gateway_loop=None,
            get_job=lambda job_id: {"last_status": "ok"},
        )
        result = tool["_run_claimed_job"]({"id": JOB_ID, **TICK_JOB})
        self.assertTrue(result["claimed"], result)
        self.assertEqual(seen, [(JOB_ID, None)])

    def test_cronjob_create_and_update_record_the_origin(self):
        """``cronjob_manage`` create and update record the turn's origin for the job id."""

        class CronSchedulerRegistrationError(Exception):
            def to_dict(self):
                return {"error": "refused"}

        created = {"id": "j-new"}
        updated = {"id": "j-new", "prompt": "changed"}
        tool = self._patched_namespace(
            self._apply_all(),
            "tools/cronjob_tools.py",
            CronSchedulerRegistrationError=CronSchedulerRegistrationError,
            create_job_with_scheduler_registration=lambda **kw: created,
            update_job=lambda job_id, updates: updated,
            tool_error=lambda message, **kw: message,
        )
        args = {"prompt": "p", "reasoning_effort": None, "failure_deliver": None, "risk": None}
        with mock.patch.object(cron_job_template, "record_job_origin", return_value=True) as record:
            tool["_action_create"](args)
            tool["_action_update"]({"id": "j-new"}, args)
            self.assertEqual([c.args for c in record.call_args_list], [("j-new",), ("j-new",)])
            record.reset_mock()
            # A refused create, and an update of a job that is gone, record nothing.
            def refuse(**kw):
                raise CronSchedulerRegistrationError()

            tool["create_job_with_scheduler_registration"] = refuse
            tool["update_job"] = lambda job_id, updates: None
            self.assertEqual(tool["_action_create"](args), "refused")
            tool["_action_update"]({"id": "j-gone"}, args)
            record.assert_not_called()
        # A recorder that raises never fails the tool call.
        tool["create_job_with_scheduler_registration"] = lambda **kw: created
        with mock.patch.object(cron_job_template, "record_job_origin", side_effect=RuntimeError("boom")):
            self.assertIn("j-new", tool["_action_create"](args))

    def test_applying_twice_is_refused(self):
        """Deliberately not idempotent: the out-param is inserted, not
        substituted, so a second pass would append a second `outcome=None`."""
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(SCHEDULER_STUB)
        (root / "cron" / "jobs.py").write_text(JOBS_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        apply_cron_run_scope.apply(root)
        with self.assertRaises(SystemExit):
            apply_cron_run_scope.apply(root)


if __name__ == "__main__":
    unittest.main()
