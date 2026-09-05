#!/usr/bin/env python3
"""The seven validators every forge's verbs run on their arguments.

Shared rather than per-forge because the thing being validated is the caller's
request, not the forge's API. `limit` bounds a page because a listing nobody
reads to the end is a listing that should not have been fetched; `branch`
refuses `HEAD` because pushing a ref by that name breaks every later clone.
Neither fact belongs to any one forge.

Two of these run twice on the same value -- once in the sandbox client, once
here -- and that is deliberate. The client's copy turns a mistake into a
message before a request is spent; this copy is the one that is load-bearing,
because it is the one on the side that holds the credential.
"""

from __future__ import annotations

from typing import Any

from workspace_paths import WorkspaceError

from .identity import BRANCH_RE, SHA_RE

# How many items a listing returns. One page, deliberately: paginating walks
# every page of an issue tracker, which is minutes of API calls and a response
# no caller reads to the end. A truncated listing says that it is truncated.
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


def validate_branch(value: Any, field: str = "branch") -> str:
    if not isinstance(value, str) or not BRANCH_RE.match(value.strip()):
        raise WorkspaceError(f"{field} is not an acceptable branch name")
    value = value.strip()
    if (
        value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith(".lock")
        # `git rev-parse --abbrev-ref HEAD` answers "HEAD" on a detached head,
        # so a client that forwards its answer unchecked asks to publish a
        # branch by that name. It is a legal ref, which is the problem: pushing
        # it creates refs/heads/HEAD and makes `HEAD` ambiguous in every later
        # clone. The sandbox client refuses this too; the broker does not trust
        # it to.
        or value == "HEAD"
    ):
        raise WorkspaceError(f"{field} is not an acceptable branch name")
    return value


def validate_revision(value: Any, field: str = "baseRevision") -> str:
    if not isinstance(value, str) or not SHA_RE.match(value.strip()):
        raise WorkspaceError(f"{field} must be a full 40-character revision id")
    return value.strip()


def validate_text(value: Any, field: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise WorkspaceError(f"{field} must be a string")
    if required and not value.strip():
        raise WorkspaceError(f"{field} must not be empty")
    return value


def validate_number(value: Any, field: str = "number") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkspaceError(f"{field} must be a positive item number")
    return value


def validate_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkspaceError("limit must be a positive number of items")
    return min(value, MAX_PAGE_SIZE)


def validate_state(value: Any) -> str:
    # Absent means "open"; present and not a string is a mistake and says so.
    # Folding the two -- coercing anything non-string to the default -- answers
    # a caller that sent `{"state": 3}` with the open ones and no indication
    # that the filter it asked for was dropped.
    if value is None:
        return "open"
    if not isinstance(value, str):
        raise WorkspaceError("state must be one of open, closed, all")
    state = value.strip().lower() or "open"
    if state not in {"open", "closed", "all"}:
        raise WorkspaceError("state must be one of open, closed, all")
    return state


def validate_labels(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(label, str) and label.strip() for label in value
    ):
        raise WorkspaceError("labels must be a list of non-empty strings")
    return [label.strip() for label in value]
