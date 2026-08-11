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
    FINALIZER_ANCHOR,
    FINALIZER_RELATIVE,
    HALT_ANCHOR,
    LOOP_RELATIVE,
    apply,
)
from kanban_guardrail_exit import (
    DETECTOR,
    OUTCOME,
    guardrail_halt_nudge,
    missing_terminal_error,
    record_missing_terminal_call,
    should_record_missing_terminal,
    task_is_still_running,
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


# The applier is exercised against a miniature of the two real files. The real
# anchors are asserted against the shipped image by verify_kanban_guardrail_exit.py.
LOOP_STUB = '''import os

logger = None

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
                    messages.append({"role": "assistant", "content": final_response})
                    break
    return final_response
'''

FINALIZER_STUB = '''import os

def finalize_turn(agent, messages, interrupted, failed, _turn_exit_reason):
    logger = None
    iteration_limit_fallback = False

    # Determine if conversation completed successfully
    return {}
'''


class ApplierTest(unittest.TestCase):
    def _apply(self):
        root = Path(tempfile.mkdtemp())
        (root / "agent").mkdir()
        (root / LOOP_RELATIVE).write_text(LOOP_STUB)
        (root / FINALIZER_RELATIVE).write_text(FINALIZER_STUB)
        apply(root)
        return (
            (root / LOOP_RELATIVE).read_text(),
            (root / FINALIZER_RELATIVE).read_text(),
        )

    def test_both_files_are_patched_and_stay_parseable(self):
        loop, finalizer = self._apply()
        ast.parse(loop)
        ast.parse(finalizer)
        self.assertIn("if _kanban_halt_nudge:", loop)
        self.assertIn("_kanban_should_record_missing(", finalizer)

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
        root = Path(tempfile.mkdtemp())
        (root / "agent").mkdir()
        (root / LOOP_RELATIVE).write_text("def run_conversation():\n    pass\n")
        (root / FINALIZER_RELATIVE).write_text(FINALIZER_STUB)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        """Deliberately not idempotent: a second run means the anchor moved."""
        root = Path(tempfile.mkdtemp())
        (root / "agent").mkdir()
        (root / LOOP_RELATIVE).write_text(LOOP_STUB)
        (root / FINALIZER_RELATIVE).write_text(FINALIZER_STUB)
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_the_anchors_are_the_ones_the_image_greps_for(self):
        self.assertIn("_turn_exit_reason = \"guardrail_halt\"", HALT_ANCHOR)
        self.assertIn("# Determine if conversation completed", FINALIZER_ANCHOR)


if __name__ == "__main__":
    unittest.main()
