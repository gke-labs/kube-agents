"""Load `runner/contract/*.schema.json` and validate against them.

The JSON files are the contract; this module is the only thing that reads them,
so a runner written in another language can ignore it entirely and validate the
same files with its own tooling.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema_mini import validate

CONTRACT_VERSION = "v1alpha1"

CONTRACT_DIR = Path(__file__).resolve().parent / "contract"

# Event type names, so a typo is an AttributeError at import rather than a
# silently unmatched string at runtime.
RUN_STARTED = "run.started"
CHECKLIST = "checklist"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
MESSAGE = "message"
ARTIFACT = "artifact"
RUN_FINISHED = "run.finished"

EVENT_TYPES = frozenset(
    {RUN_STARTED, CHECKLIST, TOOL_CALL, TOOL_RESULT, MESSAGE, ARTIFACT, RUN_FINISHED}
)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "budget_exceeded", "refused"})


class ContractViolation(Exception):
    """A request or event does not satisfy the contract."""


@lru_cache(maxsize=None)
def _schema(name: str) -> dict[str, Any]:
    path = CONTRACT_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"contract schema {path} is missing -- runner/contract/ is the source of truth "
            "and this module cannot validate without it"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def request_schema() -> dict[str, Any]:
    return _schema("run_request")


def event_schema() -> dict[str, Any]:
    return _schema("run_event")


def request_errors(request: Any) -> list[str]:
    return validate(request, request_schema())


def event_errors(event: Any) -> list[str]:
    return validate(event, event_schema())


def check_request(request: Any) -> None:
    """Raise ``ContractViolation`` if ``request`` is not a valid RunRequest."""
    errors = request_errors(request)
    if errors:
        raise ContractViolation("invalid RunRequest:\n  " + "\n  ".join(errors))


def check_event(event: Any) -> None:
    """Raise ``ContractViolation`` if ``event`` is not a valid RunEvent."""
    errors = event_errors(event)
    if errors:
        raise ContractViolation("invalid RunEvent:\n  " + "\n  ".join(errors))


def new_request(
    *,
    run_id: str,
    subject: str,
    issuer: str,
    profile: str,
    input_text: str,
    workspace_mode: str = "none",
    workspace_path: str | None = None,
    conversation: str | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid RunRequest.

    A convenience for tests and for control-plane code that would otherwise
    hand-assemble the same nested literal; the contract is the schema, not this
    signature.
    """
    workspace: dict[str, Any] = {"mode": workspace_mode}
    if workspace_path is not None:
        workspace["path"] = workspace_path
    task: dict[str, Any] = {"input": input_text}
    if conversation is not None:
        task["conversation"] = conversation
    return {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "principal": {"subject": subject, "issuer": issuer},
        "profile": {"name": profile},
        "task": task,
        "workspace": workspace,
        "budget": dict(budget) if budget else {},
    }
