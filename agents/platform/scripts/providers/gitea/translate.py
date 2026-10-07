#!/usr/bin/env python3
"""Gitea's JSON, turned into the concepts every forge has under another name.

This is where Gitea's vocabulary stops. Gitea's REST API was modelled on
GitHub's, so most fields share a name, and that is the trap: the places they
differ are where a caller written against one would read the other wrong.

- A merged pull request is `merged: true`, with `state: "closed"`.
- Draft is not a field a client sets. Gitea derives it from a title prefix
  (`WIP:` by default), so the translation reads the prefix, reports `draft`,
  and hands back the title without it -- the title the caller wrote.
- A user carries no `type`. Gitea's own automation principals (the Actions
  user, the ghost of a deleted account) have negative ids, which is what
  `is_automation` reads.
- An inline review comment carries `position` where GitHub has `line`.

Kept apart from `forge.py` because the two answer different questions, and
because the recorded responses under `testdata/providers/gitea/` are tested
against this file alone.
"""

from __future__ import annotations

from typing import Any

# The title prefixes Gitea reads as "work in progress" with its default
# `[repository.pull-request] WORK_IN_PROGRESS_PREFIXES`. Compared without case,
# as Gitea compares them. `DRAFT_PREFIX` is the one this forge writes.
DRAFT_PREFIXES = ("WIP:", "[WIP]")
DRAFT_PREFIX = "WIP: "


def actor(node: dict[str, Any] | None) -> str:
    """A login. Gitea marks nothing onto an automation's login, so it is as sent."""
    return ((node or {}).get("login") or "").strip()


def is_automation(node: dict[str, Any] | None) -> bool:
    """Whether this author is one of Gitea's own principals rather than an account.

    Gitea gives its built-in principals negative ids: the Actions bot is -2 and
    a deleted account's ghost is -1. A bot that is an ordinary account (a
    token-holding service user) is indistinguishable from a person here, which
    is also true of the API itself.
    """
    ident = (node or {}).get("id")
    return isinstance(ident, int) and not isinstance(ident, bool) and ident < 0


def split_draft(title: str) -> tuple[str, bool]:
    """The title without its work-in-progress prefix, and whether it had one."""
    stripped = (title or "").lstrip()
    for prefix in DRAFT_PREFIXES:
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix):].lstrip(), True
    return title or "", False


def _labels(node: dict[str, Any]) -> list[str]:
    return [
        item.get("name", "")
        for item in (node.get("labels") or [])
        if isinstance(item, dict)
    ]


def proposal(node: dict[str, Any]) -> dict[str, Any]:
    """A pull request as a proposal.

    `sourceRepo` is the head repository's `full_name`, empty when the fork it
    came from has been deleted -- the same reading GitHub's translation gives,
    and for the same reason: a caller deciding whether it opened this from its
    own branch must not take a stranger's same-named branch for it.
    `sourceRevision` is the head sha as of this read.
    """
    if node.get("merged") or node.get("merged_at"):
        state = "merged"
    else:
        state = "open" if node.get("state") == "open" else "closed"
    title, prefixed = split_draft(node.get("title") or "")
    head = node.get("head") or {}
    return {
        "number": node.get("number"),
        "title": title,
        "state": state,
        "draft": bool(node.get("draft")) or prefixed,
        "author": actor(node.get("user")),
        "labels": _labels(node),
        "source": head.get("ref") or "",
        "sourceRepo": ((head.get("repo") or {}).get("full_name")) or "",
        "sourceRevision": head.get("sha") or "",
        "target": ((node.get("base") or {}).get("ref")) or "",
        "url": node.get("html_url") or "",
        "created": node.get("created_at") or "",
        "updated": node.get("updated_at") or "",
        "closed": node.get("closed_at") or node.get("merged_at") or "",
        "body": node.get("body") or "",
    }


def issue(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": node.get("number"),
        "title": node.get("title") or "",
        "state": node.get("state") or "",
        "author": actor(node.get("user")),
        "labels": _labels(node),
        "assignees": [actor(person) for person in (node.get("assignees") or [])],
        "url": node.get("html_url") or "",
        "created": node.get("created_at") or "",
        "updated": node.get("updated_at") or "",
        "body": node.get("body") or "",
    }


def comment(node: dict[str, Any], kind: str = "issue") -> dict[str, Any]:
    """One utterance, from whichever of Gitea's three endpoints produced it.

    `kind` is the caller's, as on GitHub, and `ref` is `kind` and `id`
    together. On Gitea a conversation comment and an inline review comment are
    rows of one table and cannot share an id, but a review's id is from another
    table and can, so the pair is still the identity to key on.
    """
    ident = node.get("id")
    line = node.get("position") or node.get("original_position") or None
    return {
        "id": ident,
        "ref": f"{kind}-{ident}",
        "kind": kind,
        "author": actor(node.get("user")),
        "bot": is_automation(node.get("user")),
        "created": node.get("submitted_at") or node.get("created_at") or "",
        "body": node.get("body") or "",
        "url": node.get("html_url") or "",
        "path": node.get("path") or "",
        "line": line,
    }


def commit(node: dict[str, Any]) -> dict[str, Any]:
    """One commit on a proposal's source branch, dated by its committer."""
    inner = node.get("commit") or {}
    return {
        "sha": node.get("sha") or "",
        "author": actor(node.get("author")) or ((inner.get("author") or {}).get("name") or ""),
        "committed": ((inner.get("committer") or {}).get("date")) or "",
        "message": inner.get("message") or "",
        "url": node.get("html_url") or "",
    }


def label(node: dict[str, Any]) -> dict[str, Any]:
    """A label. Gitea answers its colour with a leading `#`; the protocol has none."""
    return {
        "name": node.get("name") or "",
        "color": (node.get("color") or "").lstrip("#"),
        "description": node.get("description") or "",
    }
