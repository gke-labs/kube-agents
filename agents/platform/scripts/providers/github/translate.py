#!/usr/bin/env python3
"""GitHub's JSON, turned into the concepts every forge has under another name.

This is where GitHub's vocabulary stops. A caller that received `head.ref`,
`author_association` and `merged_at` would be a GitHub client wearing a neutral
URL, and the second forge would be a second client rather than a second class.

Kept apart from `forge.py` because the two answer different questions --
`forge.py` says which call to make, this says what came back means -- and
because the fixtures that hold GitHub's real response shapes are tested against
this file alone.
"""

from __future__ import annotations

from typing import Any


def actor(node: dict[str, Any] | None) -> str:
    """A login, with the App suffix removed.

    `[bot]` comes off here rather than at the caller. GitHub's REST and GraphQL
    APIs disagree about whether an App login carries the suffix, and the cost of
    comparing an unnormalised one is recorded in this repository's history: an
    agent that answers its own comments forever.
    """
    login = ((node or {}).get("login") or "").strip()
    return login.removesuffix("[bot]")


def proposal(node: dict[str, Any]) -> dict[str, Any]:
    """A pull request as a proposal.

    Three states, not GitHub's two plus a timestamp. Closed and merged are
    different outcomes on every forge, and a caller should not have to know
    that GitHub encodes the difference in a nullable date field.
    """
    if node.get("merged_at"):
        state = "merged"
    else:
        state = "open" if node.get("state") == "open" else "closed"
    return {
        "number": node.get("number"),
        "title": node.get("title") or "",
        "state": state,
        "draft": bool(node.get("draft")),
        "author": actor(node.get("user")),
        "source": ((node.get("head") or {}).get("ref")) or "",
        "target": ((node.get("base") or {}).get("ref")) or "",
        "url": node.get("html_url") or "",
        "created": node.get("created_at") or "",
        "updated": node.get("updated_at") or "",
        "body": node.get("body") or "",
    }


def issue(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": node.get("number"),
        "title": node.get("title") or "",
        "state": node.get("state") or "",
        "author": actor(node.get("user")),
        "labels": [
            label.get("name", "")
            for label in (node.get("labels") or [])
            if isinstance(label, dict)
        ],
        "assignees": [actor(person) for person in (node.get("assignees") or [])],
        "url": node.get("html_url") or "",
        "created": node.get("created_at") or "",
        "updated": node.get("updated_at") or "",
        "body": node.get("body") or "",
    }


def comment(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "author": actor(node.get("user")),
        "created": node.get("created_at") or "",
        "body": node.get("body") or "",
        "url": node.get("html_url") or "",
    }
