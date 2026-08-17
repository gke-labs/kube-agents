"""Unit tests for the shared ownership predicates installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The whole point of this module is that the two callers want opposite answers
from the same uncertainty, so every predicate is exercised at both polarities,
and the two failure modes are kept apart: an *absent* reader module is a
structural fact about the process and must not consult ``on_unknown``, while a
reader that *raises* is genuine uncertainty and must.
"""

import os
import sys
import types
import unittest
from unittest import mock

from kanban_ownership import (
    CRON_RUN_ENV,
    WORKER_TASK_ENV,
    in_cron_run,
    is_delegated_child,
    is_dispatcher_worker,
)


def with_delegation_context(reader):
    """Install a fake ``agent.delegation_context`` whose reader is *reader*.

    ``_delegation_reader`` imports per call, so a fake in ``sys.modules`` is
    enough to exercise the branch on a host with no Hermes.
    """
    fake = types.ModuleType("agent.delegation_context")
    fake.is_delegated_child_context = reader
    agent_pkg = types.ModuleType("agent")
    agent_pkg.delegation_context = fake
    return mock.patch.dict(
        sys.modules, {"agent": agent_pkg, "agent.delegation_context": fake}
    )


def no_delegation_context():
    """Make the import fail the way it does on a host without Hermes."""
    return mock.patch.dict(sys.modules, {"agent.delegation_context": None})


def boom(*args, **kwargs):
    raise RuntimeError("reader is present and broken")


class IsDelegatedChildTest(unittest.TestCase):
    def test_the_real_reader_is_believed_at_either_polarity(self):
        for answer in (True, False):
            with with_delegation_context(lambda: answer):
                self.assertIs(is_delegated_child(on_unknown=True), answer)
                self.assertIs(is_delegated_child(on_unknown=False), answer)

    def test_an_absent_module_is_not_uncertainty(self):
        """No delegation machinery in the process means no delegated child.

        This is the case that must ignore ``on_unknown`` — otherwise the answer
        flips on every host where this module imports but Hermes does not,
        including this test suite.
        """
        with no_delegation_context():
            self.assertFalse(is_delegated_child(on_unknown=True))
            self.assertFalse(is_delegated_child(on_unknown=False))

    def test_a_raising_reader_is_uncertainty_and_defers_to_the_caller(self):
        with with_delegation_context(boom):
            self.assertTrue(is_delegated_child(on_unknown=True))
            self.assertFalse(is_delegated_child(on_unknown=False))

    def test_a_truthy_non_bool_is_normalised(self):
        with with_delegation_context(lambda: "yes"):
            self.assertIs(is_delegated_child(on_unknown=False), True)

    def test_the_polarity_must_be_stated(self):
        """No default: there is no answer that is safe in both directions."""
        with self.assertRaises(TypeError):
            is_delegated_child()
        with self.assertRaises(TypeError):
            is_delegated_child(False)


class InCronRunTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.environ.pop, CRON_RUN_ENV, None)
        os.environ.pop(CRON_RUN_ENV, None)

    def test_the_real_scope_is_believed_at_either_polarity(self):
        from cron_run_scope import cron_run_scope

        self.assertFalse(in_cron_run(on_unknown=True))
        with cron_run_scope("fleet-audit"):
            self.assertTrue(in_cron_run(on_unknown=True))
            self.assertTrue(in_cron_run(on_unknown=False))
        self.assertFalse(in_cron_run(on_unknown=False))

    def test_a_raising_reader_is_uncertainty_and_defers_to_the_caller(self):
        # _cron_reader re-imports per call, so replacing the module attribute
        # is enough to make the reader raise.
        import cron_run_scope

        original = cron_run_scope.current_cron_job
        cron_run_scope.current_cron_job = boom
        try:
            self.assertTrue(in_cron_run(on_unknown=True))
            self.assertFalse(in_cron_run(on_unknown=False))
        finally:
            cron_run_scope.current_cron_job = original
        self.assertFalse(in_cron_run(on_unknown=True))

    def test_an_absent_module_falls_back_to_the_environment(self):
        """The env half of the marker is the one thing a ContextVar cannot cross
        a fork with, so an unresolvable module is not uncertainty either — the
        evidence is still readable.
        """
        with mock.patch.dict(
            sys.modules, {"tools.cron_run_scope": None, "cron_run_scope": None}
        ):
            self.assertFalse(in_cron_run(on_unknown=True))
            os.environ[CRON_RUN_ENV] = "fleet-audit"
            self.assertTrue(in_cron_run(on_unknown=False))

    def test_a_forked_run_is_recognised_through_the_real_reader_too(self):
        # current_cron_job consults the env var itself, so the fallback above is
        # only reached when the module is missing entirely.
        os.environ[CRON_RUN_ENV] = "fleet-audit"
        self.assertTrue(in_cron_run(on_unknown=False))

    def test_the_polarity_must_be_stated(self):
        with self.assertRaises(TypeError):
            in_cron_run()
        with self.assertRaises(TypeError):
            in_cron_run(True)


class IsDispatcherWorkerTest(unittest.TestCase):
    def test_it_reads_the_variable_the_dispatcher_exports(self):
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: "t_c31a1f00"}):
            self.assertTrue(is_dispatcher_worker())

    def test_an_empty_task_id_is_not_a_worker(self):
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: ""}):
            self.assertFalse(is_dispatcher_worker())

    def test_an_unset_variable_is_not_a_worker(self):
        env = {k: v for k, v in os.environ.items() if k != WORKER_TASK_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(is_dispatcher_worker())

    def test_it_takes_no_polarity(self):
        """An environment read has no failure mode to be uncertain about.

        It also has no precision — the variable is inherited by a delegated
        child and by a cron run — which is why the callers compose it with the
        other two predicates rather than using it alone.
        """
        with self.assertRaises(TypeError):
            is_dispatcher_worker(on_unknown=True)


class CallerPolarityTest(unittest.TestCase):
    """The asymmetry the shared module exists to make visible.

    Same uncertainty, opposite answers, because a wrong answer costs the two
    callers different things: one strands a card whose worker has lost its
    terminal tools, the other charges a failure to a card somebody is still
    working. Asserted here so a future edit cannot quietly align them.
    """

    def test_the_two_callers_disagree_about_a_raising_reader(self):
        import kanban_guardrail_exit
        import kanban_worker_tools

        with with_delegation_context(boom):
            with mock.patch.dict(os.environ, {WORKER_TASK_ENV: "t_c31a1f00"}):
                self.assertTrue(kanban_worker_tools.check_kanban_worker_mode())
            self.assertFalse(
                kanban_guardrail_exit.should_record_missing_terminal(
                    task_id="t_d3efabef",
                    interrupted=False,
                    failed=False,
                    iteration_limit_fallback=False,
                    goal_mode=False,
                    cron_run=False,
                )
            )

    def test_and_agree_about_an_absent_module(self):
        import kanban_guardrail_exit
        import kanban_worker_tools

        with no_delegation_context():
            with mock.patch.dict(os.environ, {WORKER_TASK_ENV: "t_c31a1f00"}):
                self.assertTrue(kanban_worker_tools.check_kanban_worker_mode())
            self.assertTrue(
                kanban_guardrail_exit.should_record_missing_terminal(
                    task_id="t_d3efabef",
                    interrupted=False,
                    failed=False,
                    iteration_limit_fallback=False,
                    goal_mode=False,
                    cron_run=False,
                )
            )


if __name__ == "__main__":
    unittest.main()
