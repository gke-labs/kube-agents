"""Unit tests for the guardrail-exit fix installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The scenario under test is card ``t_d3efabef`` on 2026-08-07: four runs, 38
minutes, 208 web searches, no output. Each run ended ``⚠️ Tool guardrail halted
web_search: loop_web_search_cap`` and the worker exited rc=0 without ever
touching the board.
"""

import ast
import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from apply_kanban_guardrail_exit import (
    CLASSIFY_ANCHOR,
    CLI_ANCHOR,
    CLI_RELATIVE,
    FINALIZER_ANCHOR,
    FINALIZER_RELATIVE,
    HALT_ANCHOR,
    LOOP_RELATIVE,
    apply,
)
from kanban_guardrail_exit import (
    BLOCK_REASON_MAX_CHARS,
    DETECTOR,
    OUTCOME,
    RATE_LIMIT_BLOCK_KIND,
    RATE_LIMIT_REASON_PREFIX,
    RETRIES_EXHAUSTED_EXIT_REASON,
    block_rate_limited_worker,
    guardrail_halt_nudge,
    is_rate_limit_exhaustion,
    missing_terminal_error,
    rate_limit_block_reason,
    record_missing_terminal_call,
    record_rate_limit_block,
    should_block_rate_limited,
    should_record_missing_terminal,
    task_is_still_running,
    worker_run_id,
)

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
"""


class Decision:
    """Stands in for ``ToolGuardrailDecision``."""

    def __init__(self, tool_name="web_search", code="loop_web_search_cap"):
        self.tool_name = tool_name
        self.code = code


STOCK_NUDGE = "[System: You are a Hermes kanban worker. …]"


def nudge_returning(value):
    def build(*, messages, attempts):
        return value

    return build


def board(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for tid, status in rows:
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (tid, status))
    return conn


class GuardrailHaltNudgeTest(unittest.TestCase):
    def test_the_stock_nudge_gains_the_tool_is_gone_instruction(self):
        """Without it the model retries the blocked tool and burns the budget."""
        out = guardrail_halt_nudge(
            nudge_returning(STOCK_NUDGE),
            messages=[],
            attempts=0,
            decision=Decision(),
        )
        self.assertTrue(out.startswith(STOCK_NUDGE))
        self.assertIn("web_search", out)
        self.assertIn("loop_web_search_cap", out)
        self.assertIn("do not try again", out)
        self.assertIn("kanban_complete", out)
        self.assertIn("kanban_block", out)

    def test_no_nudge_means_no_nudge(self):
        """Upstream returns None for non-workers and for a spent budget."""
        self.assertIsNone(
            guardrail_halt_nudge(
                nudge_returning(None), messages=[], attempts=0, decision=Decision()
            )
        )

    def test_an_empty_nudge_is_not_extended_into_a_real_one(self):
        self.assertIsNone(
            guardrail_halt_nudge(
                nudge_returning(""), messages=[], attempts=0, decision=Decision()
            )
        )

    def test_a_raising_helper_falls_through_to_the_old_exit(self):
        """A broken nudge must never wedge a worker."""

        def boom(*, messages, attempts):
            raise RuntimeError("no")

        self.assertIsNone(
            guardrail_halt_nudge(boom, messages=[], attempts=0, decision=Decision())
        )

    def test_a_decision_missing_its_fields_still_produces_text(self):
        out = guardrail_halt_nudge(
            nudge_returning(STOCK_NUDGE),
            messages=[],
            attempts=0,
            decision=Decision(tool_name=None, code=None),
        )
        self.assertIn("that tool", out)
        self.assertIn("tool guardrail", out)

    def test_attempts_and_messages_reach_the_upstream_helper(self):
        seen = {}

        def build(*, messages, attempts):
            seen["messages"] = messages
            seen["attempts"] = attempts
            return STOCK_NUDGE

        msgs = [{"role": "user", "content": "hi"}]
        guardrail_halt_nudge(build, messages=msgs, attempts=1, decision=Decision())
        self.assertIs(seen["messages"], msgs)
        self.assertEqual(seen["attempts"], 1)


def leaked(**overrides):
    kwargs = dict(
        task_id="t_d3efabef",
        interrupted=False,
        failed=False,
        iteration_limit_fallback=False,
        goal_mode=False,
        delegated_child=False,
    )
    kwargs.update(overrides)
    return should_record_missing_terminal(**kwargs)


class ShouldRecordTest(unittest.TestCase):
    def test_the_incident_shape_is_a_leak(self):
        self.assertTrue(leaked())

    def test_the_transcript_is_not_consulted(self):
        """A terminal tool call in `messages` must not suppress the check.

        It used to. A REJECTED kanban_complete still lands as a tool message
        named kanban_complete, and session_called_kanban_terminal matches on the
        name alone, so one refusal from kanban_result_required silenced this
        backstop for the rest of the run and the card leaked. The board says
        whether the card moved; the transcript only says what was attempted.
        """
        self.assertTrue(leaked())
        with self.assertRaises(TypeError):
            should_record_missing_terminal(
                task_id="t_d3efabef",
                interrupted=False,
                failed=False,
                iteration_limit_fallback=False,
                session_called_terminal=lambda messages: True,
            )

    def test_a_non_worker_is_never_touched(self):
        self.assertFalse(leaked(task_id=None))
        self.assertFalse(leaked(task_id=""))

    def test_goal_mode_is_excluded(self):
        """Its intermediate turns end without a terminal call by design.

        ``_run_kanban_goal_loop_q`` calls ``run_conversation`` once per turn, so
        recording a failure on turn 1 of N would release the claim and close the
        run underneath a loop that is working correctly.
        """
        self.assertFalse(leaked(goal_mode=True))

    def test_goal_mode_defaults_to_the_environment(self):
        import os

        prior = os.environ.get("HERMES_KANBAN_GOAL_MODE")
        try:
            os.environ["HERMES_KANBAN_GOAL_MODE"] = "1"
            self.assertFalse(leaked(goal_mode=None))
            os.environ["HERMES_KANBAN_GOAL_MODE"] = "0"
            self.assertTrue(leaked(goal_mode=None))
            del os.environ["HERMES_KANBAN_GOAL_MODE"]
            self.assertTrue(leaked(goal_mode=None))
        finally:
            os.environ.pop("HERMES_KANBAN_GOAL_MODE", None)
            if prior is not None:
                os.environ["HERMES_KANBAN_GOAL_MODE"] = prior

    def test_the_budget_path_records_its_own_failure(self):
        self.assertFalse(leaked(iteration_limit_fallback=True))

    def test_a_failed_turn_already_exits_nonzero(self):
        self.assertFalse(leaked(failed=True))

    def test_an_interrupt_is_the_callers_business(self):
        self.assertFalse(leaked(interrupted=True))


class DelegatedChildExclusionTest(unittest.TestCase):
    """A delegate_task child inherits its parent's card and owns none of its own.

    The child runs ``run_conversation`` in the parent's process, so it reaches
    ``finalize_turn`` with the parent's ``HERMES_KANBAN_TASK`` set. Recording
    there charges the PARENT a ``timed_out`` and releases its claim mid-run —
    twice, if it delegates twice — after which the parent completes a card it no
    longer holds. Upstream draws the same line: ``_reject_delegated_child_mutation``
    refuses every board mutation from a child for exactly this reason.
    """

    def test_a_delegated_child_is_not_a_leak(self):
        self.assertFalse(leaked(delegated_child=True))

    def test_an_explicit_false_still_leaks(self):
        self.assertTrue(leaked(delegated_child=False))

    def test_it_defaults_to_the_real_delegation_context(self):
        """None must reach ``agent.delegation_context``, not silently pass.

        The parameter exists for the tests; the runtime never passes it, so a
        default that did not consult the real module would leave the fix inert
        in the only place it matters.
        """
        module = importlib.import_module("kanban_guardrail_exit")
        fake = types.ModuleType("agent.delegation_context")
        fake.is_delegated_child_context = lambda: True
        agent_pkg = types.ModuleType("agent")
        agent_pkg.delegation_context = fake
        with mock.patch.dict(
            sys.modules,
            {"agent": agent_pkg, "agent.delegation_context": fake},
        ):
            self.assertTrue(module.is_delegated_child(on_unknown=True))
            self.assertFalse(
                module.should_record_missing_terminal(
                    task_id="t_d3efabef",
                    interrupted=False,
                    failed=False,
                    iteration_limit_fallback=False,
                    goal_mode=False,
                    cron_run=False,
                )
            )

    def test_a_host_without_hermes_is_not_a_delegated_child(self):
        """An absent module is a structural fact, not uncertainty.

        ``on_unknown`` covers a reader that raises; it deliberately does not
        cover a reader that is not there. Answering True for an absent module
        would disable the backstop everywhere this code is importable but Hermes
        is not — including these tests.
        """
        module = importlib.import_module("kanban_guardrail_exit")
        with mock.patch.dict(sys.modules, {"agent.delegation_context": None}):
            self.assertFalse(module.is_delegated_child(on_unknown=True))

    def test_a_raising_reader_withholds_the_write(self):
        """Uncertainty must not charge a failure to a card someone is working.

        This is the ``on_unknown=True`` half of the asymmetry: the same reader,
        raising the same way, leaves ``kanban_worker_tools`` answering False.
        """
        module = importlib.import_module("kanban_guardrail_exit")

        def boom():
            raise RuntimeError("no delegation context")

        fake = types.ModuleType("agent.delegation_context")
        fake.is_delegated_child_context = boom
        agent_pkg = types.ModuleType("agent")
        agent_pkg.delegation_context = fake
        with mock.patch.dict(
            sys.modules,
            {"agent": agent_pkg, "agent.delegation_context": fake},
        ):
            self.assertTrue(module.is_delegated_child(on_unknown=True))
            self.assertFalse(leaked(delegated_child=None, cron_run=False))


class CronRunExclusionTest(unittest.TestCase):
    """A dispatched cron run has no card of its own, so it cannot leak one.

    ``cronjob(action='run')`` executes in-process inside the dispatching worker
    and inherits ``HERMES_KANBAN_TASK`` — cron_run_scope leaves it set on purpose,
    to keep the dispatcher's claim heart-beating through a long audit. That id is
    the caller's card, and the run is barred from terminating it. Charging the
    caller a ``timed_out`` for that would release its claim while it is still
    blocked on the run.
    """

    def test_a_cron_run_is_not_a_leak(self):
        self.assertFalse(leaked(cron_run=True))

    def test_an_explicit_false_still_leaks(self):
        # The caller can overrule the ambient marker; nothing does today, but
        # the parameter is what makes the rest of these tests hermetic.
        self.assertTrue(leaked(cron_run=False))

    def test_it_defaults_to_the_real_cron_run_scope(self):
        from cron_run_scope import cron_run_scope

        self.assertTrue(leaked(cron_run=None))
        with cron_run_scope("fleet-audit"):
            self.assertFalse(leaked(cron_run=None))
        self.assertTrue(leaked(cron_run=None))

    def test_a_forked_run_is_recognised_from_the_environment_alone(self):
        # current_cron_job falls back to the environment, which is the one
        # thing a ContextVar cannot do — cross a fork. Nothing in the image
        # writes the marker there today (cron_run_scope deliberately does not;
        # see its docstring), so this covers a process launched with it already
        # set rather than anything the scope produces.
        import os

        prior = os.environ.get("HERMES_KANBAN_CRON_RUN")
        try:
            os.environ["HERMES_KANBAN_CRON_RUN"] = "fleet-audit"
            self.assertFalse(leaked(cron_run=None))
        finally:
            os.environ.pop("HERMES_KANBAN_CRON_RUN", None)
            if prior is not None:
                os.environ["HERMES_KANBAN_CRON_RUN"] = prior
        self.assertTrue(leaked(cron_run=None))

    def test_an_unreadable_marker_withholds_the_write(self):
        # Same rule as the transcript check above, stated from the other side:
        # this function writes to a live board, so it does nothing it cannot
        # justify — hence in_cron_run(on_unknown=True). kanban_ownership
        # re-imports the reader per call, so replacing the module attribute is
        # enough.
        import cron_run_scope

        def boom(environ=None):
            raise RuntimeError("no")

        original = cron_run_scope.current_cron_job
        cron_run_scope.current_cron_job = boom
        try:
            self.assertFalse(leaked(cron_run=None))
        finally:
            cron_run_scope.current_cron_job = original
        self.assertTrue(leaked(cron_run=None))

    def test_the_nudge_is_deliberately_left_alone(self):
        # The exclusion is scoped to the board write. in_cron_run can only
        # over-report — the env half of the marker is process-wide, so a worker
        # running alongside somebody else's dispatch reads it as its own — and
        # silencing the nudge on that worker would undo the fix. Wasting two
        # turns on a cron run that gets refused is the cheaper error.
        from cron_run_scope import cron_run_scope

        with cron_run_scope("fleet-audit"):
            self.assertIn(
                "do not try again",
                guardrail_halt_nudge(
                    nudge_returning(STOCK_NUDGE),
                    messages=[],
                    attempts=0,
                    decision=Decision(),
                ),
            )


class TaskIsStillRunningTest(unittest.TestCase):
    def test_running_is_running(self):
        self.assertTrue(task_is_still_running(board([("t1", "running")]), "t1"))

    def test_done_and_blocked_are_terminal(self):
        """The board outranks the transcript: compaction can drop a tool call."""
        self.assertFalse(task_is_still_running(board([("t1", "done")]), "t1"))
        self.assertFalse(task_is_still_running(board([("t1", "blocked")]), "t1"))

    def test_a_missing_card_is_not_running(self):
        self.assertFalse(task_is_still_running(board([]), "t1"))

    def test_a_broken_connection_is_not_running(self):
        class Boom:
            def execute(self, *a):
                raise sqlite3.OperationalError("no such table: tasks")

        self.assertFalse(task_is_still_running(Boom(), "t1"))

    def test_a_plain_tuple_row_factory_works(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO tasks (id, status) VALUES ('t1', 'running')")
        self.assertTrue(task_is_still_running(conn, "t1"))


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, conn, task_id, **kwargs):
        self.calls.append((task_id, kwargs))


class ClosableConn:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    def execute(self, *a, **kw):
        return self._conn.execute(*a, **kw)

    def close(self):
        self.closed = True


class RecordMissingTerminalTest(unittest.TestCase):
    def _record(self, status, reason="guardrail_halt"):
        conn = ClosableConn(board([("t1", status)]))
        rec = Recorder()
        did = record_missing_terminal_call(
            task_id="t1",
            turn_exit_reason=reason,
            connect=lambda: conn,
            record_failure=rec,
        )
        return did, rec, conn

    def test_a_running_card_is_charged_a_timed_out_failure(self):
        did, rec, conn = self._record("running")
        self.assertTrue(did)
        self.assertEqual(len(rec.calls), 1)
        task_id, kwargs = rec.calls[0]
        self.assertEqual(task_id, "t1")
        self.assertEqual(kwargs["outcome"], OUTCOME)
        self.assertTrue(kwargs["release_claim"])
        self.assertTrue(kwargs["end_run"])
        self.assertTrue(conn.closed)

    def test_the_exit_reason_is_carried_into_the_error_and_the_payload(self):
        """An unexplained protocol_violation is what this replaces."""
        did, rec, _ = self._record("running", reason="empty_response_exhausted")
        _, kwargs = rec.calls[0]
        self.assertIn("empty_response_exhausted", kwargs["error"])
        self.assertEqual(
            kwargs["event_payload_extra"],
            {
                "turn_exit_reason": "empty_response_exhausted",
                "detector": DETECTOR,
            },
        )

    def test_a_card_a_terminal_tool_already_moved_is_left_alone(self):
        for status in ("done", "blocked", "ready", "todo"):
            did, rec, conn = self._record(status)
            self.assertFalse(did, status)
            self.assertEqual(rec.calls, [], status)
            self.assertTrue(conn.closed, status)

    def test_the_connection_is_closed_even_when_recording_raises(self):
        conn = ClosableConn(board([("t1", "running")]))

        def boom(*a, **kw):
            raise RuntimeError("db locked")

        with self.assertRaises(RuntimeError):
            record_missing_terminal_call(
                task_id="t1",
                turn_exit_reason="guardrail_halt",
                connect=lambda: conn,
                record_failure=boom,
            )
        self.assertTrue(conn.closed)

    def test_the_error_text_names_the_contract_that_was_broken(self):
        text = missing_terminal_error("guardrail_halt")
        self.assertIn("terminal kanban call", text)
        self.assertIn("guardrail_halt", text)


STORM_ERROR = (
    "Error code: 429 - {'error': {'message': 'litellm.RateLimitError: "
    'VertexAIException - {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
    "\"retryDelay\": \"54s\"}]}}', 'type': None, 'code': '429'}}"
)

STORM_FAILURE = ("rate_limit", STORM_ERROR)


class Blocker:
    """Stands in for ``hermes_cli.kanban_db.block_task``."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, conn, task_id, **kwargs):
        self.calls.append((task_id, kwargs))
        return self.ok


class RateLimitPredicateTest(unittest.TestCase):
    def test_the_storm_exit_is_a_rate_limit_exhaustion(self):
        self.assertTrue(
            is_rate_limit_exhaustion(RETRIES_EXHAUSTED_EXIT_REASON, STORM_FAILURE)
        )

    def test_the_other_leaking_exits_are_not(self):
        for reason in (
            "guardrail_halt",
            "partial_stream_recovery",
            "empty_response_exhausted",
            "local_processing_error",
        ):
            self.assertFalse(is_rate_limit_exhaustion(reason, STORM_FAILURE), reason)

    def test_a_retries_exhaustion_on_something_else_keeps_timed_out(self):
        for failure in (
            ("context_overflow", "too long"),
            ("server_error", "500"),
            ("billing", "credits exhausted"),
            None,
            ("rate_limit",),
            "rate_limit",
        ):
            self.assertFalse(
                is_rate_limit_exhaustion(RETRIES_EXHAUSTED_EXIT_REASON, failure),
                repr(failure),
            )

    def test_the_reason_opens_with_the_prefix_and_carries_the_text(self):
        reason = rate_limit_block_reason("rate_limit", STORM_ERROR)
        self.assertTrue(reason.startswith(RATE_LIMIT_REASON_PREFIX))
        self.assertIn("failure_reason=rate_limit", reason)
        self.assertIn("RESOURCE_EXHAUSTED", reason)
        self.assertIn('"retryDelay": "54s"', reason)

    def test_the_reason_is_bounded_and_single_line(self):
        reason = rate_limit_block_reason("rate_limit", "x\n" * 5000)
        self.assertNotIn("\n", reason)
        self.assertLessEqual(
            len(reason), BLOCK_REASON_MAX_CHARS + len(RATE_LIMIT_REASON_PREFIX) + 40
        )

    def test_missing_text_is_said_rather_than_left_blank(self):
        reason = rate_limit_block_reason("rate_limit", None)
        self.assertIn("no provider error text", reason)

    def test_the_run_id_is_read_from_the_dispatchers_environment(self):
        self.assertEqual(worker_run_id({"HERMES_KANBAN_RUN_ID": "42"}), 42)
        self.assertIsNone(worker_run_id({}))
        self.assertIsNone(worker_run_id({"HERMES_KANBAN_RUN_ID": ""}))
        self.assertIsNone(worker_run_id({"HERMES_KANBAN_RUN_ID": "abc"}))


def blockable(**overrides):
    kwargs = dict(
        task_id="t_storm",
        failure_reason="rate_limit",
        goal_mode=False,
        cron_run=False,
        delegated_child=False,
    )
    kwargs.update(overrides)
    return should_block_rate_limited(**kwargs)


class ShouldBlockRateLimitedTest(unittest.TestCase):
    def test_the_storm_shape_is_blockable(self):
        self.assertTrue(blockable())

    def test_a_non_worker_is_never_touched(self):
        self.assertFalse(blockable(task_id=None))
        self.assertFalse(blockable(task_id=""))

    def test_only_a_rate_limit_qualifies(self):
        """billing keeps the stock exit-75 path: a credit wall does not clear."""
        for reason in ("billing", "context_overflow", "server_error", None, ""):
            self.assertFalse(blockable(failure_reason=reason), repr(reason))

    def test_the_backstops_exclusions_apply(self):
        self.assertFalse(blockable(goal_mode=True))
        self.assertFalse(blockable(cron_run=True))
        self.assertFalse(blockable(delegated_child=True))

    def test_goal_mode_defaults_to_the_environment(self):
        import os

        prior = os.environ.get("HERMES_KANBAN_GOAL_MODE")
        try:
            os.environ["HERMES_KANBAN_GOAL_MODE"] = "1"
            self.assertFalse(blockable(goal_mode=None))
            del os.environ["HERMES_KANBAN_GOAL_MODE"]
            self.assertTrue(blockable(goal_mode=None))
        finally:
            os.environ.pop("HERMES_KANBAN_GOAL_MODE", None)
            if prior is not None:
                os.environ["HERMES_KANBAN_GOAL_MODE"] = prior

    def test_the_cron_and_delegation_defaults_are_the_real_readers(self):
        from cron_run_scope import cron_run_scope

        self.assertTrue(blockable(cron_run=None, delegated_child=None))
        with cron_run_scope("fleet-audit"):
            self.assertFalse(blockable(cron_run=None, delegated_child=None))
        self.assertTrue(blockable(cron_run=None, delegated_child=None))


class RecordRateLimitBlockTest(unittest.TestCase):
    def _block(self, status, run_id=7, ok=True):
        conn = ClosableConn(board([("t_storm", status)]))
        blocker = Blocker(ok=ok)
        did = record_rate_limit_block(
            task_id="t_storm",
            failure_reason="rate_limit",
            error_text=STORM_ERROR,
            connect=lambda: conn,
            block_task=blocker,
            run_id=run_id,
        )
        return did, blocker, conn

    def test_a_running_card_is_blocked_transient_with_the_provider_text(self):
        did, blocker, conn = self._block("running")
        self.assertTrue(did)
        self.assertEqual(len(blocker.calls), 1)
        task_id, kwargs = blocker.calls[0]
        self.assertEqual(task_id, "t_storm")
        self.assertEqual(kwargs["kind"], RATE_LIMIT_BLOCK_KIND)
        self.assertEqual(kwargs["expected_run_id"], 7)
        self.assertIn("RESOURCE_EXHAUSTED", kwargs["reason"])
        self.assertTrue(kwargs["reason"].startswith(RATE_LIMIT_REASON_PREFIX))
        self.assertTrue(conn.closed)

    def test_a_card_a_terminal_tool_already_moved_is_left_alone(self):
        for status in ("done", "blocked", "ready", "todo"):
            did, blocker, conn = self._block(status)
            self.assertFalse(did, status)
            self.assertEqual(blocker.calls, [], status)
            self.assertTrue(conn.closed, status)

    def test_a_refused_block_is_reported_as_not_done(self):
        """block_task returns False when the run moved under its transaction."""
        did, blocker, _ = self._block("running", ok=False)
        self.assertFalse(did)
        self.assertEqual(len(blocker.calls), 1)

    def test_no_run_id_means_no_run_pin(self):
        _, blocker, _ = self._block("running", run_id=None)
        self.assertIsNone(blocker.calls[0][1]["expected_run_id"])

    def test_the_connection_is_closed_even_when_blocking_raises(self):
        conn = ClosableConn(board([("t_storm", "running")]))

        def boom(*a, **kw):
            raise RuntimeError("db locked")

        with self.assertRaises(RuntimeError):
            record_rate_limit_block(
                task_id="t_storm",
                failure_reason="rate_limit",
                error_text=STORM_ERROR,
                connect=lambda: conn,
                block_task=boom,
            )
        self.assertTrue(conn.closed)


class FinalizerRateLimitBranchTest(unittest.TestCase):
    """The finalize_turn site: retries exhausted with no response after a 429."""

    def _record(self, reason, last_api_failure, block_task=None):
        conn = ClosableConn(board([("t_storm", "running")]))
        rec = Recorder()
        did = record_missing_terminal_call(
            task_id="t_storm",
            turn_exit_reason=reason,
            connect=lambda: conn,
            record_failure=rec,
            block_task=block_task,
            last_api_failure=last_api_failure,
            run_id=7,
        )
        return did, rec

    def test_a_429_exhaustion_blocks_instead_of_charging_timed_out(self):
        blocker = Blocker()
        did, rec = self._record(
            RETRIES_EXHAUSTED_EXIT_REASON, STORM_FAILURE, block_task=blocker
        )
        self.assertTrue(did)
        self.assertEqual(rec.calls, [])
        self.assertEqual(len(blocker.calls), 1)
        _, kwargs = blocker.calls[0]
        self.assertEqual(kwargs["kind"], RATE_LIMIT_BLOCK_KIND)
        self.assertEqual(kwargs["expected_run_id"], 7)
        self.assertIn("RESOURCE_EXHAUSTED", kwargs["reason"])

    def test_a_non_429_exhaustion_keeps_timed_out(self):
        blocker = Blocker()
        did, rec = self._record(
            RETRIES_EXHAUSTED_EXIT_REASON,
            ("context_overflow", "too long"),
            block_task=blocker,
        )
        self.assertTrue(did)
        self.assertEqual(blocker.calls, [])
        self.assertEqual(rec.calls[0][1]["outcome"], OUTCOME)

    def test_the_other_exits_keep_timed_out_whatever_was_stashed(self):
        blocker = Blocker()
        did, rec = self._record("guardrail_halt", STORM_FAILURE, block_task=blocker)
        self.assertTrue(did)
        self.assertEqual(blocker.calls, [])
        self.assertEqual(rec.calls[0][1]["outcome"], OUTCOME)

    def test_without_block_task_the_old_contract_holds(self):
        """A caller that does not inject block_task gets the timed_out path."""
        did, rec = self._record(RETRIES_EXHAUSTED_EXIT_REASON, STORM_FAILURE)
        self.assertTrue(did)
        self.assertEqual(rec.calls[0][1]["outcome"], OUTCOME)


class BlockRateLimitedWorkerTest(unittest.TestCase):
    """The cli.py site: the failed result run_conversation returns on a 429."""

    ENV = {"HERMES_KANBAN_TASK": "t_storm", "HERMES_KANBAN_RUN_ID": "7"}

    def _run(self, result, status="running", environ=None, **overrides):
        conn = ClosableConn(board([("t_storm", status)]))
        blocker = Blocker()
        kwargs = dict(
            connect=lambda: conn,
            block_task=blocker,
            environ=self.ENV if environ is None else environ,
            cron_run=False,
            delegated_child=False,
        )
        kwargs.update(overrides)
        return block_rate_limited_worker(result, **kwargs), blocker

    def test_the_storm_result_blocks_the_card(self):
        did, blocker = self._run(
            {"failed": True, "failure_reason": "rate_limit", "error": STORM_ERROR}
        )
        self.assertTrue(did)
        task_id, kwargs = blocker.calls[0]
        self.assertEqual(task_id, "t_storm")
        self.assertEqual(kwargs["kind"], RATE_LIMIT_BLOCK_KIND)
        self.assertEqual(kwargs["expected_run_id"], 7)
        self.assertIn("RESOURCE_EXHAUSTED", kwargs["reason"])

    def test_a_result_that_did_not_fail_is_not_this_sites_business(self):
        for result in (
            {"failed": False, "failure_reason": "rate_limit"},
            {"completed": True},
            None,
            "not a dict",
        ):
            did, blocker = self._run(result)
            self.assertFalse(did, repr(result))
            self.assertEqual(blocker.calls, [], repr(result))

    def test_billing_and_real_failures_keep_the_stock_exit(self):
        for reason in ("billing", "server_error", "context_overflow", None):
            did, blocker = self._run({"failed": True, "failure_reason": reason})
            self.assertFalse(did, repr(reason))
            self.assertEqual(blocker.calls, [], repr(reason))

    def test_a_non_worker_process_is_untouched(self):
        did, blocker = self._run(
            {"failed": True, "failure_reason": "rate_limit"}, environ={}
        )
        self.assertFalse(did)
        self.assertEqual(blocker.calls, [])

    def test_goal_mode_is_read_from_the_environment_given(self):
        env = dict(self.ENV, HERMES_KANBAN_GOAL_MODE="1")
        did, blocker = self._run(
            {"failed": True, "failure_reason": "rate_limit"}, environ=env
        )
        self.assertFalse(did)
        self.assertEqual(blocker.calls, [])

    def test_a_card_already_moved_is_left_alone(self):
        did, blocker = self._run(
            {"failed": True, "failure_reason": "rate_limit"}, status="blocked"
        )
        self.assertFalse(did)
        self.assertEqual(blocker.calls, [])

    def test_missing_error_text_still_blocks_with_a_legible_reason(self):
        did, blocker = self._run({"failed": True, "failure_reason": "rate_limit"})
        self.assertTrue(did)
        self.assertIn("no provider error text", blocker.calls[0][1]["reason"])


# The applier is exercised against a miniature of the three real files. The real
# anchors are asserted against the shipped image by verify_kanban_guardrail_exit.py.
LOOP_STUB = '''import os

logger = None

# v2026.8.19 routed the loop's appends through this helper instead of calling
# list.append directly, which is the one line of the halt anchor that moved.
# The real one (agent/message_metadata.py) stamps a timestamp on the way past,
# so the nudge this patch inserts has to go through it too.
def append_message(messages, message):
    message.setdefault("timestamp", 0.0)
    messages.append(message)

def run_conversation(agent, messages):
    _pending_verification_response = None
    _pending_verification_response_previewed = False
    final_response = None
    while True:
        if agent.tool_calls:
            if agent._tool_guardrail_halt_decision is not None:
                    decision = agent._tool_guardrail_halt_decision
                    _turn_exit_reason = "guardrail_halt"
                    final_response = agent._toolguard_controlled_halt_response(decision)
                    agent._emit_status(
                        f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}"
                    )
                    append_message(messages, {"role": "assistant", "content": final_response})
                    break
        while True:
            try:
                response = agent.call()
            except Exception as api_error:
                approx_tokens = 0
                _ctx_len = 0
                api_messages = []
                classified = classify_api_error(
                    api_error,
                    provider=getattr(agent, "provider", "") or "",
                    model=getattr(agent, "model", "") or "",
                    approx_tokens=approx_tokens,
                    context_length=_ctx_len,
                    num_messages=len(api_messages) if api_messages else 0,
                )
                logger.debug("Error classified: reason=%s", classified.reason.value)
    return final_response
'''

CLI_STUB = '''import os
import sys

logger = None

def main(result):
                        _exit_code = 0
                        if isinstance(result, dict) and result.get("failed"):
                            _exit_code = 1
                            if os.environ.get("HERMES_KANBAN_TASK") and result.get(
                                "failure_reason"
                            ) in ("rate_limit", "billing"):
                                _exit_code = 75
                        sys.exit(_exit_code)
'''

FINALIZER_STUB = '''import os

def finalize_turn(agent, messages, interrupted, failed, _turn_exit_reason):
    logger = None
    iteration_limit_fallback = False

    # Determine if conversation completed successfully
    return {}
'''


def stage_tree():
    root = Path(tempfile.mkdtemp())
    (root / "agent").mkdir()
    (root / LOOP_RELATIVE).write_text(LOOP_STUB)
    (root / FINALIZER_RELATIVE).write_text(FINALIZER_STUB)
    (root / CLI_RELATIVE).write_text(CLI_STUB)
    return root


class ApplierTest(unittest.TestCase):
    def _apply(self):
        root = stage_tree()
        apply(root)
        return (
            (root / LOOP_RELATIVE).read_text(),
            (root / FINALIZER_RELATIVE).read_text(),
        )

    def _apply_all(self):
        root = stage_tree()
        apply(root)
        return (
            (root / LOOP_RELATIVE).read_text(),
            (root / FINALIZER_RELATIVE).read_text(),
            (root / CLI_RELATIVE).read_text(),
        )

    def test_all_files_are_patched_and_stay_parseable(self):
        loop, finalizer, cli = self._apply_all()
        ast.parse(loop)
        ast.parse(finalizer)
        ast.parse(cli)
        self.assertIn("if _kanban_halt_nudge:", loop)
        self.assertIn("_kanban_should_record_missing(", finalizer)
        self.assertIn("agent._kube_last_api_failure = (", loop)
        self.assertIn("_kube_block_rate_limited(result)", cli)

    def test_the_stash_follows_the_classification(self):
        loop, _, _ = self._apply_all()
        self.assertLess(
            loop.index("classified = classify_api_error("),
            loop.index("agent._kube_last_api_failure = ("),
        )
        self.assertLess(
            loop.index("agent._kube_last_api_failure = ("),
            loop.index("Error classified"),
        )

    def test_the_stash_cannot_raise_out_of_the_error_handler(self):
        loop, _, _ = self._apply_all()
        body = loop[loop.index("agent._kube_last_api_failure = (") :]
        self.assertIn("except Exception:", body[: body.index("logger.debug")])
        self.assertIn("agent._kube_last_api_failure = None", body)

    def test_the_finalizer_passes_the_block_path_and_the_stash(self):
        _, finalizer, _ = self._apply_all()
        call = finalizer[finalizer.index("_kanban_record_missing_terminal(") :]
        call = call[: call.index("):")]
        self.assertIn("block_task=_kb.block_task", call)
        self.assertIn(
            'last_api_failure=getattr(agent, "_kube_last_api_failure", None)', call
        )
        self.assertIn("run_id=_kube_worker_run_id()", call)

    def test_the_cli_block_runs_before_the_exit_code_is_decided(self):
        _, _, cli = self._apply_all()
        self.assertLess(
            cli.index("_kube_block_rate_limited(result)"),
            cli.index("_exit_code = 0"),
        )
        self.assertEqual(cli.count(CLI_ANCHOR), 1)

    def test_the_cli_block_cannot_change_the_exit_code(self):
        """Exit 75 is the reaper's contract; the block is additive."""
        _, _, cli = self._apply_all()
        block = cli[cli.index("kube-agents patch: a worker") : cli.index("_exit_code = 0")]
        self.assertNotIn("_exit_code", block)
        self.assertNotIn("sys.exit", block)
        self.assertIn("except Exception:", block)

    def test_the_nudge_runs_before_the_break_it_replaces(self):
        loop, _ = self._apply()
        self.assertLess(
            loop.index("if _kanban_halt_nudge:"),
            loop.index("\n                    break\n"),
        )

    def test_the_halt_decision_is_cleared_before_continuing(self):
        """reset_for_turn clears it per turn, not per iteration."""
        loop, _ = self._apply()
        body = loop[loop.index("if _kanban_halt_nudge:") :]
        self.assertLess(
            body.index("agent._tool_guardrail_halt_decision = None"),
            body.index("continue"),
        )

    def test_the_nudge_is_appended_through_the_stamping_helper(self):
        """A raw list.append leaves one undated message in a dated transcript."""
        loop, _ = self._apply()
        body = loop[loop.index("if _kanban_halt_nudge:") :]
        self.assertIn("append_message(messages, {", body)
        self.assertNotIn("messages.append(", body)

    def test_the_exit_reason_is_taken_back_off_guardrail_halt(self):
        loop, _ = self._apply()
        body = loop[loop.index("if _kanban_halt_nudge:") :]
        self.assertIn('_turn_exit_reason = "unknown"', body)

    def test_the_search_counter_is_never_reset(self):
        """Resetting it would hand out another 50 searches, not fix the exit."""
        loop, _ = self._apply()
        self.assertNotIn("_turn_web_search_count", loop)

    def test_the_backstop_runs_before_the_completed_determination(self):
        _, finalizer = self._apply()
        self.assertLess(
            finalizer.index("_kanban_should_record_missing("),
            finalizer.index("# Determine if conversation completed successfully"),
        )

    def test_a_missing_anchor_is_fatal_not_silent(self):
        root = stage_tree()
        (root / LOOP_RELATIVE).write_text("def run_conversation():\n    pass\n")
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_a_missing_cli_anchor_is_fatal_too(self):
        root = stage_tree()
        (root / CLI_RELATIVE).write_text("def main():\n    pass\n")
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        """Deliberately not idempotent: a second run means the anchor moved."""
        root = stage_tree()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_the_anchors_are_the_ones_the_image_greps_for(self):
        self.assertIn("_turn_exit_reason = \"guardrail_halt\"", HALT_ANCHOR)
        self.assertIn("# Determine if conversation completed", FINALIZER_ANCHOR)
        self.assertIn("classified = classify_api_error(", CLASSIFY_ANCHOR)
        self.assertIn("_exit_code = 0", CLI_ANCHOR)


if __name__ == "__main__":
    unittest.main()
