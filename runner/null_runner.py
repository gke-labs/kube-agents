"""A runner that does no work, so the contract can be tested without a model.

Two jobs. It is the reference implementation -- the shortest thing that passes
the conformance suite, which is what makes the suite's difficulty honest. And it
is a fixture generator: `scripted()` replays a canned sequence, so a consumer of
the event stream (a dispatcher, a chat gateway, a bench client) can be tested
against a run that is deterministic and does not need a cluster.

It is not a mock in the usual sense. It validates its input and refuses bad
requests exactly as a real runner must, because "the null runner accepted it"
should mean the request was well formed.
"""

from __future__ import annotations

import itertools
from typing import Any, Iterable, Iterator

import schema


class NullRunner:
    """Emits a deterministic, contract-valid stream for any valid request."""

    def __init__(self, script: Iterable[dict[str, Any]] | None = None) -> None:
        """``script`` is a sequence of partial events -- type and its own
        fields, with the envelope left off. ``run()`` stamps
        ``contract_version``, ``run_id`` and ``seq`` onto each. Omit it for the
        default echo behaviour.
        """
        self._script = list(script) if script is not None else None

    @classmethod
    def scripted(cls, *events: dict[str, Any]) -> NullRunner:
        """``NullRunner.scripted({"type": "message", ...}, ...)``."""
        return cls(events)

    def run(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """The contract's single method: a request in, an event stream out.

        Lazy on purpose. A runner that builds the whole stream before yielding
        cannot report progress, and a consumer written against an eager stub
        would deadlock against a real one.
        """
        run_id = request.get("run_id") if isinstance(request, dict) else None
        if not isinstance(run_id, str) or not run_id:
            # No envelope can be built without it, so there is no valid stream
            # in which to report the problem.
            raise ValueError("request has no usable 'run_id'; cannot emit a contract-valid stream")

        counter = itertools.count()

        def emit(body: dict[str, Any]) -> dict[str, Any]:
            event = {
                "contract_version": schema.CONTRACT_VERSION,
                "run_id": run_id,
                "seq": next(counter),
                **body,
            }
            # Self-check rather than trust: the reference implementation
            # emitting an invalid event would make every conformance run a
            # false negative somewhere else.
            schema.check_event(event)
            return event

        errors = schema.request_errors(request)
        if errors:
            yield emit({"type": schema.RUN_STARTED})
            yield emit(
                {
                    "type": schema.RUN_FINISHED,
                    "status": "refused",
                    "error": "invalid RunRequest: " + "; ".join(errors),
                }
            )
            return

        yield emit({"type": schema.RUN_STARTED, "profile": request["profile"]["name"]})

        if self._script is not None:
            saw_terminal = False
            for body in self._script:
                if body.get("type") == schema.RUN_STARTED:
                    raise ValueError("a script must not contain its own run.started event")
                saw_terminal = body.get("type") == schema.RUN_FINISHED
                yield emit(dict(body))
            if not saw_terminal:
                yield emit({"type": schema.RUN_FINISHED, "status": "completed"})
            return

        yield emit(
            {
                "type": schema.CHECKLIST,
                "items": [{"id": "echo", "title": "Echo the task input", "status": "active"}],
            }
        )
        yield emit({"type": schema.MESSAGE, "role": "assistant", "text": request["task"]["input"]})
        yield emit(
            {
                "type": schema.CHECKLIST,
                "items": [{"id": "echo", "title": "Echo the task input", "status": "done"}],
            }
        )
        yield emit(
            {
                "type": schema.RUN_FINISHED,
                "status": "completed",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "tool_calls": 0},
            }
        )
