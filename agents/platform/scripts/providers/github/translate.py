#!/usr/bin/env python3
"""GitHub's JSON, turned into the concepts every forge has under another name.

This is where GitHub's vocabulary stops. A caller that received `head.ref`,
`author_association` and `merged_at` would be a GitHub client wearing a neutral
URL, and the second forge would be a second client rather than a second class.

Kept apart from `forge.py` because the two answer different questions --
`forge.py` says which call to make, this says what came back means -- and
because the recorded responses under `testdata/providers/github/` are tested against
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


def is_automation(node: dict[str, Any] | None) -> bool:
    """Whether this author is an automation rather than a person.

    Asked here because `actor` above removes the suffix that is the only thing
    a caller could have read it off, and a caller above the boundary has no
    business knowing that GitHub spells it `[bot]` at all. The answer is part of
    a comment rather than derivable from it.

    `type` is what REST says and is the reliable one. The suffix is the
    fallback, for the GraphQL shapes that carry a login and no type.
    """
    node = node or {}
    if str(node.get("type") or "").strip().lower() == "bot":
        return True
    return str(node.get("login") or "").strip().endswith("[bot]")


def proposal(node: dict[str, Any]) -> dict[str, Any]:
    """A pull request as a proposal.

    Three states, not GitHub's two plus a timestamp. Closed and merged are
    different outcomes on every forge, and a caller should not have to know
    that GitHub encodes the difference in a nullable date field.

    `source` is a branch name and says nothing about where that branch lives.
    `sourceRepo` is the repository it lives in, and the two have to be read
    together: a proposal opened from a fork carries the bare branch name, so a
    caller deciding "did I open this, from a branch I wrote" on `source` alone
    accepts any fork's branch of the same name. It is `""` when the fork has
    been deleted, which is the forge saying it no longer knows -- distinct from
    naming this repository, and a caller that treats the two alike will amend a
    branch under a name a stranger chose.

    `sourceRevision` is that branch's tip as of this read, and is deliberately
    a property of the read rather than of the proposal: the question it
    answers -- did anything land here after the request I am replying to -- is
    only sound against a tip re-read at the moment of asking.
    """
    if node.get("merged_at"):
        state = "merged"
    else:
        state = "open" if node.get("state") == "open" else "closed"
    head = node.get("head") or {}
    return {
        "number": node.get("number"),
        "title": node.get("title") or "",
        "state": state,
        "draft": bool(node.get("draft")),
        "author": actor(node.get("user")),
        "labels": [
            item.get("name", "")
            for item in (node.get("labels") or [])
            if isinstance(item, dict)
        ],
        "source": head.get("ref") or "",
        "sourceRepo": ((head.get("repo") or {}).get("full_name")) or "",
        "sourceRevision": head.get("sha") or "",
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


def comment(node: dict[str, Any], kind: str = "issue") -> dict[str, Any]:
    """One utterance, from whichever of GitHub's three endpoints produced it.

    `kind` is the caller's, not the node's: GitHub's three comment shapes do
    not say which endpoint they came from, and the endpoint is what decides
    whether a reaction can be left on it (`proposal-acknowledge`) and whether
    `path`/`line` mean anything. A review's timestamp is `submitted_at`.

    `ref` is `id` and `kind` together, and it is what a caller should key
    bookkeeping on. `id` alone is unique only within the endpoint that issued
    it: a conversation comment and a review comment on the same proposal can
    carry the same number, so a marker recording that one had been answered
    would suppress the other. The pair is also exactly the identity
    `proposal-acknowledge` takes, so there is one notion of which comment is
    meant rather than two.
    """
    ident = node.get("id")
    return {
        "id": ident,
        "ref": f"{kind}-{ident}",
        "kind": kind,
        "author": actor(node.get("user")),
        # Beside the author rather than inside it: `author` has the `[bot]`
        # suffix taken off, and the bot-loop gate the sweep runs on this is the
        # one thing that suffix was ever read for.
        "bot": is_automation(node.get("user")),
        "created": node.get("submitted_at") or node.get("created_at") or "",
        "body": node.get("body") or "",
        "url": node.get("html_url") or "",
        "path": node.get("path") or "",
        "line": node.get("line"),
    }


def commit(node: dict[str, Any]) -> dict[str, Any]:
    """One commit on a proposal's source branch.

    The committer date, not the author date: a rebase or a cherry-pick keeps
    the author date of a commit written weeks ago, and the question a caller
    asks of this list is "did it land after the request", which only the
    committer date answers.
    """
    inner = node.get("commit") or {}
    return {
        "sha": node.get("sha") or "",
        "author": actor(node.get("author")) or ((inner.get("author") or {}).get("name") or ""),
        "committed": ((inner.get("committer") or {}).get("date")) or "",
        "message": inner.get("message") or "",
        "url": node.get("html_url") or "",
    }


def label(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": node.get("name") or "",
        "color": node.get("color") or "",
        "description": node.get("description") or "",
    }
