#!/usr/bin/env python3
"""Bitbucket Data Center's JSON, turned into the concepts every forge has.

The differences from the neutral shapes, absorbed here:

- **Times are milliseconds since the epoch**, not ISO-8601 strings. They are
  written as UTC ISO-8601, as every other forge's are.
- **The login is the user's `name`** (the username). It is what the
  instance's `whoami` servlet answers and what its user search filters on, so
  one spelling serves the ownership checks and `can_write`.
- **A branch is a ref.** `fromRef` and `toRef` carry `refs/heads/x` as `id` and
  `x` as `displayId`; the neutral `source` and `target` are the second.
- **A repository is `KEY/slug`**, from a ref's `repository.project.key` and
  `repository.slug`.
- **Three states,** `OPEN`, `MERGED` and `DECLINED`; the last is `closed`.
- **No labels.**
- **Comments are activities.** General comments are not listed by a comments
  route (it needs a file path), so they come from the activity stream, where a
  `COMMENTED` row carries a comment and its replies. A comment with an
  `anchor` is on a line of the diff.
"""

from __future__ import annotations

import datetime
from typing import Any, Iterable

#: The neutral comment kinds a Data Center comment becomes.
CONVERSATION = "issue"
DIFF_NOTE = "review_comment"

#: The user type of a service account, which project and repository tokens
#: act as (verify live). Its comments are automation's.
SERVICE = "SERVICE"

STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed"}

#: The activity action that carries a comment.
COMMENTED = "COMMENTED"


def instant(millis: Any) -> str:
    """Milliseconds since the epoch as UTC ISO-8601, or "" for anything else."""
    if isinstance(millis, bool) or not isinstance(millis, (int, float)):
        return ""
    moment = datetime.datetime.fromtimestamp(millis / 1000, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def user(node: dict[str, Any] | None) -> dict[str, Any]:
    """The user object of a participant (`{user: ...}`) or of a user itself."""
    node = node or {}
    inner = node.get("user")
    return inner if isinstance(inner, dict) else node


def actor(node: dict[str, Any] | None) -> str:
    """A user as its login: the `name`."""
    return str(user(node).get("name") or "").strip()


def is_automation(node: dict[str, Any] | None) -> bool:
    return str(user(node).get("type") or "") == SERVICE


def repository(ref: dict[str, Any] | None) -> str:
    """`KEY/slug` of the repository a ref is in, or ""."""
    holder = (ref or {}).get("repository") or {}
    key = str((holder.get("project") or {}).get("key") or "")
    slug = str(holder.get("slug") or "")
    return f"{key}/{slug}" if key and slug else ""


def _same(left: str, right: str) -> bool:
    return bool(left) and left.casefold() == right.casefold()


def _self_link(node: dict[str, Any]) -> str:
    links = (node.get("links") or {}).get("self") or []
    if isinstance(links, list) and links and isinstance(links[0], dict):
        return str(links[0].get("href") or "")
    return ""


def proposal(node: dict[str, Any], repo: str = "") -> dict[str, Any]:
    """A pull request as a proposal.

    `sourceRepo` is `repo`, as the caller spelled it, when the source branch
    is in the same repository (project keys compare without case); the other
    repository's `KEY/slug` when it is a fork; "" when the instance does not
    say.
    """
    source = node.get("fromRef") if isinstance(node.get("fromRef"), dict) else {}
    target = node.get("toRef") if isinstance(node.get("toRef"), dict) else {}
    holder = repository(source)
    state = STATES.get(str(node.get("state") or ""), "closed")
    return {
        "number": node.get("id"),
        "title": node.get("title") or "",
        "state": state,
        "draft": bool(node.get("draft")),
        "author": actor(node.get("author")),
        "labels": [],
        "source": str(source.get("displayId") or ""),
        "sourceRepo": repo if _same(holder, repo) else holder,
        "sourceRevision": str(source.get("latestCommit") or ""),
        "target": str(target.get("displayId") or ""),
        "url": _self_link(node),
        "created": instant(node.get("createdDate")),
        "updated": instant(node.get("updatedDate")),
        "closed": "" if state == "open" else instant(node.get("closedDate") or node.get("updatedDate")),
        "body": node.get("description") or "",
    }


def targets(node: dict[str, Any], repo: str) -> bool:
    """Whether a pull request is onto `repo` itself, not onto another repository."""
    target = node.get("toRef") if isinstance(node.get("toRef"), dict) else {}
    holder = repository(target)
    return not holder or _same(holder, repo)


def _path(anchor: dict[str, Any]) -> str:
    """A comment anchor's file path, which is a string or a path object."""
    path = anchor.get("path")
    if isinstance(path, dict):
        return str(path.get("toString") or "/".join(path.get("components") or []))
    return str(path or "")


def comment(node: dict[str, Any], anchor: dict[str, Any] | None = None) -> dict[str, Any]:
    """A comment. `anchor` is its thread's, for a reply that carries none."""
    ident = node.get("id")
    anchor = node.get("anchor") if isinstance(node.get("anchor"), dict) else (anchor or {})
    kind = DIFF_NOTE if anchor else CONVERSATION
    return {
        "id": ident,
        "ref": f"{kind}-{ident}",
        "kind": kind,
        "author": actor(node.get("author")),
        "bot": is_automation(node.get("author")),
        "created": instant(node.get("createdDate")),
        "body": node.get("text") or "",
        "url": "",
        "path": _path(anchor) if anchor else "",
        "line": anchor.get("line") if anchor else None,
    }


def thread(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """A comment and every reply under it, as comments.

    Replies carry no anchor of their own; a reply to a comment on a line is
    about that line too, so it takes its thread's.
    """
    anchor = node.get("anchor") if isinstance(node.get("anchor"), dict) else {}
    stack = [node]
    while stack:
        current = stack.pop(0)
        if not isinstance(current, dict):
            continue
        yield comment(current, anchor)
        stack[0:0] = list(current.get("comments") or [])


def activity_comments(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The comments a row of the activity stream carries: none unless it is one."""
    if str(row.get("action") or "") != COMMENTED or not isinstance(row.get("comment"), dict):
        return []
    root = dict(row["comment"])
    if "anchor" not in root and isinstance(row.get("commentAnchor"), dict):
        root["anchor"] = row["commentAnchor"]
    return list(thread(root))


def commit(node: dict[str, Any], url: str = "") -> dict[str, Any]:
    """One commit. The committer time, for the reason GitHub's translation gives.

    `email` is the git author email and `bot` whether the linked account is a
    service account; False when no account is linked, as for the install's own
    commits.
    """
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    return {
        "sha": node.get("id") or "",
        # `name` is the username of a linked account, which is the login, and
        # the git author name when no account is linked.
        "author": str(author.get("name") or author.get("displayName") or ""),
        "email": str(author.get("emailAddress") or ""),
        "bot": is_automation(author),
        "committed": instant(node.get("committerTimestamp") or node.get("authorTimestamp")),
        "message": node.get("message") or "",
        "url": url,
    }


#: The activity action of a decline.
DECLINED = "DECLINED"

#: The activity actions that are reviews, as the neutral review state.
#: `REVIEWED` is a reviewer setting "needs work".
REVIEW_ACTIONS = {"APPROVED": "approved", "REVIEWED": "changes_requested"}


def authored(node: dict[str, Any]) -> dict[str, Any]:
    """A pull request as one of an author's proposals."""
    item = proposal(node)
    return {
        "number": item["number"],
        "state": item["state"],
        "author": item["author"],
        "authorBot": is_automation(node.get("author") if isinstance(node.get("author"), dict) else None),
        "created": item["created"],
        "updated": item["updated"],
        "closed": item["closed"],
    }


def review(row: dict[str, Any]) -> dict[str, Any] | None:
    """An approval or a "needs work" in the activity stream; None otherwise."""
    state = REVIEW_ACTIONS.get(str(row.get("action") or ""))
    if state is None:
        return None
    who = row.get("user") if isinstance(row.get("user"), dict) else None
    return {
        "id": row.get("id"),
        "state": state,
        "author": actor(who),
        "bot": is_automation(who),
        "created": instant(row.get("createdDate")),
    }


def closer(row: dict[str, Any] | None) -> dict[str, Any]:
    """Who made an activity; "" when nobody."""
    who = (row or {}).get("user") if isinstance((row or {}).get("user"), dict) else None
    return {"actor": actor(who), "bot": is_automation(who)}
