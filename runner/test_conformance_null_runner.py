"""The null runner against the conformance suite, plus the suite's own teeth.

Two halves. The first runs `RunnerConformanceTests` against `NullRunner`, in
both its echo and scripted modes -- that is the milestone's exit criterion. The
second checks that the suite *fails* runners that violate the contract, because
a conformance suite nothing can fail is decoration.
"""

from __future__ import annotations

import unittest

import schema
from conformance import RunnerConformanceTests
from null_runner import NullRunner


class NullRunnerConformance(RunnerConformanceTests, unittest.TestCase):
    def make_runner(self):
        return NullRunner()


class ScriptedNullRunnerConformance(RunnerConformanceTests, unittest.TestCase):
    """The same suite over a stream that actually uses the tool events."""

    def make_runner(self):
        return NullRunner.scripted(
            {
                "type": schema.CHECKLIST,
                "items": [{"id": "1", "title": "List the nodes", "status": "active"}],
            },
            {
                "type": schema.TOOL_CALL,
                "call_id": "call-1",
                "name": "kubectl",
                "arguments": {"argv": ["get", "nodes"]},
            },
            {
                "type": schema.TOOL_RESULT,
                "call_id": "call-1",
                "status": "completed",
                "output": "node-1  Ready",
            },
            {"type": schema.ARTIFACT, "kind": "file", "ref": "report.md"},
            {"type": schema.MESSAGE, "role": "assistant", "text": "One node, ready."},
            {
                "type": schema.CHECKLIST,
                "items": [{"id": "1", "title": "List the nodes", "status": "done"}],
            },
        )


class NullRunnerBehaviour(unittest.TestCase):
    def test_the_echo_run_returns_the_task_input(self):
        request = schema.new_request(
            run_id="r1",
            subject="user:a",
            issuer="system",
            profile="platform",
            input_text="how many nodes?",
        )
        messages = [e for e in NullRunner().run(request) if e["type"] == schema.MESSAGE]
        self.assertEqual(["how many nodes?"], [m["text"] for m in messages])

    def test_run_started_echoes_the_resolved_profile(self):
        request = schema.new_request(
            run_id="r1", subject="user:a", issuer="system", profile="platform", input_text="hi"
        )
        first = next(iter(NullRunner().run(request)))
        self.assertEqual("platform", first["profile"])

    def test_a_script_may_not_supply_its_own_run_started(self):
        runner = NullRunner.scripted({"type": schema.RUN_STARTED})
        request = schema.new_request(
            run_id="r1", subject="user:a", issuer="system", profile="p", input_text="hi"
        )
        with self.assertRaises(ValueError):
            list(runner.run(request))

    def test_a_script_may_supply_its_own_terminal_event(self):
        runner = NullRunner.scripted(
            {"type": schema.RUN_FINISHED, "status": "failed", "error": "the cluster was gone"}
        )
        request = schema.new_request(
            run_id="r1", subject="user:a", issuer="system", profile="p", input_text="hi"
        )
        events = list(runner.run(request))
        self.assertEqual(1, [e["type"] for e in events].count(schema.RUN_FINISHED))
        self.assertEqual("failed", events[-1]["status"])

    def test_a_request_without_a_run_id_cannot_produce_a_stream(self):
        # Distinct from a refusal: a refusal is itself a stream, and a stream
        # needs a run_id in every envelope.
        with self.assertRaises(ValueError):
            list(NullRunner().run({"contract_version": schema.CONTRACT_VERSION}))

    def test_a_refusal_names_the_schema_violation(self):
        request = schema.new_request(
            run_id="r1", subject="user:a", issuer="system", profile="p", input_text="hi"
        )
        del request["budget"]
        final = list(NullRunner().run(request))[-1]
        self.assertEqual("refused", final["status"])
        self.assertIn("budget", final["error"])


class _BadRunner:
    """Base for the deliberately-broken runners below."""

    def __init__(self, events):
        self._events = events

    def run(self, request):
        return iter(self._events)


def _envelope(seq, **body):
    return {"contract_version": schema.CONTRACT_VERSION, "run_id": "conformance-run", "seq": seq, **body}


class ConformanceSuiteHasTeeth(unittest.TestCase):
    """Each broken runner must fail the specific test that names its defect."""

    def _run_one(self, runner, test_name):
        case = type(
            "Case",
            (RunnerConformanceTests, unittest.TestCase),
            {"make_runner": lambda _self: runner},
        )(test_name)
        return unittest.TestResult(), case

    def assert_fails(self, events, test_name):
        result, case = self._run_one(_BadRunner(events), test_name)
        case.run(result)
        self.assertTrue(
            result.failures or result.errors,
            f"{test_name} passed a runner that should have failed it",
        )

    def test_the_harness_itself_is_not_the_thing_failing(self):
        """Control for every assert_fails above.

        Each of those asserts only that *something* went wrong, so a typo in a
        test name or a broken dynamic subclass would satisfy all of them while
        checking nothing. Running the same names against the conforming null
        runner is what distinguishes "the contract caught it" from "the harness
        is broken".
        """
        names = [n for n in dir(RunnerConformanceTests) if n.startswith("test_")]
        self.assertGreater(len(names), 10, "the suite should have more than a handful of tests")
        for name in names:
            with self.subTest(name):
                result, case = self._run_one(NullRunner(), name)
                case.run(result)
                self.assertEqual(
                    ([], []),
                    (result.failures, result.errors),
                    f"the null runner should pass {name}: {result.failures or result.errors}",
                )

    def test_catches_a_missing_terminal_event(self):
        self.assert_fails(
            [_envelope(0, type=schema.RUN_STARTED)],
            "test_the_last_event_is_run_finished",
        )

    def test_catches_two_terminal_events(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(1, type=schema.RUN_FINISHED, status="completed"),
                _envelope(2, type=schema.RUN_FINISHED, status="failed", error="again"),
            ],
            "test_run_finished_occurs_exactly_once",
        )

    def test_catches_a_gap_in_the_sequence(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(7, type=schema.RUN_FINISHED, status="completed"),
            ],
            "test_seq_starts_at_zero_and_increments_by_one",
        )

    def test_catches_an_orphaned_tool_result(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(1, type=schema.TOOL_RESULT, call_id="ghost", status="completed", output=""),
                _envelope(2, type=schema.RUN_FINISHED, status="completed"),
            ],
            "test_every_tool_result_answers_an_earlier_tool_call",
        )

    def test_catches_a_reused_call_id(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(1, type=schema.TOOL_CALL, call_id="c", name="kubectl", arguments={}),
                _envelope(2, type=schema.TOOL_CALL, call_id="c", name="kubectl", arguments={}),
                _envelope(3, type=schema.RUN_FINISHED, status="completed"),
            ],
            "test_call_ids_are_unique_within_a_run",
        )

    def test_catches_a_silent_failure(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(1, type=schema.RUN_FINISHED, status="failed"),
            ],
            "test_a_failure_status_carries_an_explanation",
        )

    def test_catches_an_invalid_event(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(1, type=schema.RUN_FINISHED, status="exploded"),
            ],
            "test_every_event_validates_against_the_schema",
        )

    def test_catches_a_runner_that_guesses_at_an_unknown_contract_version(self):
        self.assert_fails(
            [
                _envelope(0, type=schema.RUN_STARTED),
                _envelope(1, type=schema.RUN_FINISHED, status="completed"),
            ],
            "test_an_unknown_contract_version_is_refused_not_guessed",
        )

    def test_catches_an_eager_stream(self):
        class Eager:
            def run(self, request):
                return [_envelope(0, type=schema.RUN_STARTED)]

        result, case = self._run_one(Eager(), "test_the_stream_is_lazy")
        case.run(result)
        self.assertTrue(result.failures or result.errors)


if __name__ == "__main__":
    unittest.main()
