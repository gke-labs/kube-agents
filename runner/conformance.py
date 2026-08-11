"""The conformance suite every runner must pass.

Subclass ``RunnerConformanceTests`` alongside ``unittest.TestCase`` and
implement ``make_runner()``. The suite is transport-agnostic and speaks plain
JSON dicts, so a runner in another language passes by being wrapped in a
subprocess or HTTP shim rather than by being rewritten in Python.

What the suite deliberately does not test: anything about the *content* of a
run. A runner that answers every question with "no" passes, and should -- the
contract is about the shape of the exchange. Answer quality is the bench's job
(``bench/``), and that separation is the point: a second runner can be judged
conformant long before it is judged good.

Hermes does not run this suite yet. Making it pass is M4.1, and until then the
contract's only conforming implementation is the null runner.
"""

from __future__ import annotations

import json
from typing import Any

import schema


class RunnerConformanceTests:
    """Mixin of contract requirements. Pair with ``unittest.TestCase``."""

    # ---- what a subclass provides ------------------------------------------

    def make_runner(self) -> Any:
        """Return an object with ``run(request) -> iterable of event dicts``."""
        raise NotImplementedError

    def make_request(self, **overrides: Any) -> dict[str, Any]:
        """A valid request. Override to pin a profile the runner can resolve."""
        request = schema.new_request(
            run_id="conformance-run",
            subject="user:conformance@example.com",
            issuer="system",
            profile="default",
            input_text="Reply with anything.",
        )
        request.update(overrides)
        return request

    # ---- helpers -----------------------------------------------------------

    def collect(self, request: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        request = self.make_request() if request is None else request
        return list(self.make_runner().run(request))

    # ---- the contract ------------------------------------------------------

    def test_every_event_validates_against_the_schema(self) -> None:
        for event in self.collect():
            errors = schema.event_errors(event)
            self.assertEqual([], errors, f"event {event!r} violates the schema")

    def test_the_stream_is_not_empty(self) -> None:
        self.assertTrue(self.collect(), "a runner must always account for a run it accepted")

    def test_the_first_event_is_run_started(self) -> None:
        self.assertEqual(schema.RUN_STARTED, self.collect()[0]["type"])

    def test_run_started_occurs_exactly_once(self) -> None:
        types = [event["type"] for event in self.collect()]
        self.assertEqual(1, types.count(schema.RUN_STARTED))

    def test_the_last_event_is_run_finished(self) -> None:
        self.assertEqual(schema.RUN_FINISHED, self.collect()[-1]["type"])

    def test_run_finished_occurs_exactly_once(self) -> None:
        # A run that reports a terminal state twice has told the control plane
        # two different things about the same run.
        types = [event["type"] for event in self.collect()]
        self.assertEqual(1, types.count(schema.RUN_FINISHED))

    def test_no_events_follow_run_finished(self) -> None:
        types = [event["type"] for event in self.collect()]
        self.assertEqual(len(types) - 1, types.index(schema.RUN_FINISHED))

    def test_seq_starts_at_zero_and_increments_by_one(self) -> None:
        seqs = [event["seq"] for event in self.collect()]
        self.assertEqual(list(range(len(seqs))), seqs)

    def test_every_event_carries_the_requests_run_id(self) -> None:
        request = self.make_request(run_id="a-distinctive-run-id")
        for event in self.collect(request):
            self.assertEqual("a-distinctive-run-id", event["run_id"])

    def test_every_event_declares_the_contract_version(self) -> None:
        for event in self.collect():
            self.assertEqual(schema.CONTRACT_VERSION, event["contract_version"])

    def test_every_tool_result_answers_an_earlier_tool_call(self) -> None:
        """An unmatched result is the shape parsing.py has to call an orphan.

        The contract removes the need for that recovery: a runner that cannot
        attribute a result must not emit one.
        """
        open_calls: set[str] = set()
        for event in self.collect():
            if event["type"] == schema.TOOL_CALL:
                self.assertNotIn(
                    event["call_id"], open_calls, "call_id reused before its result arrived"
                )
                open_calls.add(event["call_id"])
            elif event["type"] == schema.TOOL_RESULT:
                self.assertIn(
                    event["call_id"],
                    open_calls,
                    "tool_result with no preceding tool_call of that call_id",
                )
                open_calls.discard(event["call_id"])

    def test_call_ids_are_unique_within_a_run(self) -> None:
        ids = [e["call_id"] for e in self.collect() if e["type"] == schema.TOOL_CALL]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_event_is_json_serialisable(self) -> None:
        # The stream crosses a process boundary in every real deployment, so an
        # event holding a live object passes in-process and fails on the wire.
        for event in self.collect():
            round_tripped = json.loads(json.dumps(event))
            self.assertEqual(event, round_tripped)

    def test_a_failure_status_carries_an_explanation(self) -> None:
        final = self.collect()[-1]
        if final["status"] != "completed":
            self.assertTrue(
                final.get("error", "").strip(),
                "a non-completed run must say why; a bare terminal event is the defect "
                "this contract exists to prevent",
            )

    def test_an_unknown_contract_version_is_refused_not_guessed(self) -> None:
        request = self.make_request(contract_version="v9-from-the-future")
        events = self.collect(request)
        self.assertEqual(schema.RUN_FINISHED, events[-1]["type"])
        self.assertEqual("refused", events[-1]["status"])

    def test_a_malformed_request_is_refused_not_crashed(self) -> None:
        request = self.make_request()
        del request["principal"]
        events = self.collect(request)
        self.assertEqual("refused", events[-1]["status"])

    def test_a_refused_run_does_no_work(self) -> None:
        request = self.make_request()
        del request["principal"]
        types = [event["type"] for event in self.collect(request)]
        self.assertNotIn(schema.TOOL_CALL, types, "a refused request must not reach the tool plane")

    def test_the_run_is_repeatable_without_cross_contamination(self) -> None:
        """Two runs off one runner instance must not share sequence state.

        A counter hoisted to instance scope is the easy version of this bug, and
        it only shows up on the second run.
        """
        runner = self.make_runner()
        first = [e["seq"] for e in runner.run(self.make_request())]
        second = [e["seq"] for e in runner.run(self.make_request())]
        self.assertEqual(first, second)

    def test_the_stream_is_lazy(self) -> None:
        """Calling run() must not do the whole run.

        A runner that returns a fully-built list cannot stream progress, and a
        consumer written against it will hang the first time it meets one that
        does stream.
        """
        stream = self.make_runner().run(self.make_request())
        self.assertFalse(
            isinstance(stream, (list, tuple)),
            "run() must return an iterator, not a materialised sequence",
        )
