"""Display labels for Task Kanban card states."""

from __future__ import annotations

from typing import Protocol, Sequence

BLOCKED_STATUS = "blocked"
TRIAGE_STATUS = "triage"
DONE_STATUS = "done"
GAVE_UP_LABEL = "failed after all attempts"
BLOCKED_LABEL = "blocked"
BLOCK_KIND_LABELS = {
    "needs_input": "blocked · needs input",
    "capability": "blocked · missing capability",
    "transient": "blocked · temporary failure",
}
DONE_LABEL = "done"
DONE_WITH_PR_LABEL = "done · with a pull request"


class CardState(Protocol):
    status: str
    block_kind: str
    gave_up: bool
    has_pr: bool


def display_status(task: CardState) -> str:
    """Return the label that shows the state of one card."""
    # The dispatcher's breaker keeps block_kind. Thus examine gave_up first.
    if task.gave_up:
        return GAVE_UP_LABEL
    if task.status == BLOCKED_STATUS:
        return BLOCK_KIND_LABELS.get(task.block_kind, BLOCKED_LABEL)
    if task.status == DONE_STATUS:
        return DONE_WITH_PR_LABEL if task.has_pr else DONE_LABEL
    return task.status


def waits_on_person(task: CardState) -> bool:
    """Return True when the card continues only after a person acts."""
    return (task.status == BLOCKED_STATUS and not task.gave_up) or (
        task.status == TRIAGE_STATUS
    )


def status_filter_options(tasks: Sequence[CardState]) -> list[str]:
    """Return each raw status and each label that the board shows."""
    return sorted(
        {task.status for task in tasks} | {display_status(task) for task in tasks}
    )


def matches_status(task: CardState, selected: str) -> bool:
    """Return True when the raw status or the label of the card is selected."""
    # A raw status selects all its labels, for example "done" and
    # "done · with a pull request".
    return selected in (task.status, display_status(task))
