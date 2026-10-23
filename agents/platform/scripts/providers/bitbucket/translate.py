#!/usr/bin/env python3
"""Bitbucket Cloud's JSON, turned into the concepts every forge has.

Where Bitbucket Cloud's vocabulary stops, as `../github/translate.py` is
where GitHub's does. The differences from the neutral shapes, absorbed here:

- **The login is the account's `uuid`.** A Cloud `nickname` is not unique, and
  the API has no `username` for most accounts, so the `uuid` (with its braces,
  as Bitbucket writes it) is what an author is compared by. `whoami` answers
  the same field.
- **Four states.** `OPEN`, `MERGED`, `DECLINED` and `SUPERSEDED`; the last two
  are the neutral `closed`.
- **No labels.** A pull request has no label field, so `labels` is always
  empty.
- **An abbreviated revision.** A pull request names its source commit by a
  12-character hash. The forge expands it where the revision decides
  something (`BitbucketCloudForge._full_revision`); the translation keeps what
  it was given.
- **Comments.** One endpoint carries the conversation and the comments on
  lines of the diff, told apart by `inline`. A comment that is `deleted`
  carries no text and is not a comment.
"""

from __future__ import annotations

from typing import Any

#: The neutral comment kinds a Bitbucket Cloud comment becomes.
CONVERSATION = "issue"
DIFF_NOTE = "review_comment"

#: The account type of the bot user behind a workspace, project or repository
#: access token. Its comments are automation's.
APP_USER = "app_user"

#: Bitbucket Cloud states, as the neutral state.
STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed", "SUPERSEDED": "closed"}

#: The Bitbucket states a neutral `state` filter asks for.
NEUTRAL_STATES = {
    "open": ("OPEN",),
    "closed": ("MERGED", "DECLINED", "SUPERSEDED"),
    "all": ("OPEN", "MERGED", "DECLINED", "SUPERSEDED"),
}


def actor(node: dict[str, Any] | None) -> str:
    """An account as its login: the `uuid`."""
    return str((node or {}).get("uuid") or "").strip()


def is_automation(node: dict[str, Any] | None) -> bool:
    """Whether an account is the bot user of an access token."""
    return str((node or {}).get("type") or "") == APP_USER


def _href(node: dict[str, Any], name: str = "html") -> str:
    link = ((node.get("links") or {}).get(name)) or {}
    return str(link.get("href") or "") if isinstance(link, dict) else ""


def _endpoint(node: dict[str, Any], side: str) -> dict[str, Any]:
    value = node.get(side)
    return value if isinstance(value, dict) else {}


def source_repo(node: dict[str, Any], repo: str) -> str:
    """The repository the source branch lives in.

    `repo` itself, spelled as the caller spelled it, when Bitbucket names the
    same repository in another case; a fork's `workspace/slug` otherwise; ""
    when Bitbucket does not say, which every caller reads as "not this
    repository".
    """
    full = str((_endpoint(node, "source").get("repository") or {}).get("full_name") or "")
    if full and repo and full.casefold() == repo.casefold():
        return repo
    return full


def proposal(node: dict[str, Any], repo: str = "") -> dict[str, Any]:
    """A pull request as a proposal.

    `closed` is when it stopped being open. Bitbucket Cloud records no close
    time, so it is the last update of a pull request that is no longer open,
    and "" while it is open.
    """
    state = STATES.get(str(node.get("state") or ""), "closed")
    source = _endpoint(node, "source")
    destination = _endpoint(node, "destination")
    body = node.get("description")
    if body is None:
        body = ((node.get("summary") or {}).get("raw")) if isinstance(node.get("summary"), dict) else ""
    return {
        "number": node.get("id"),
        "title": node.get("title") or "",
        "state": state,
        "draft": bool(node.get("draft")),
        "author": actor(node.get("author")),
        "labels": [],
        "source": str((source.get("branch") or {}).get("name") or ""),
        "sourceRepo": source_repo(node, repo),
        "sourceRevision": str((source.get("commit") or {}).get("hash") or ""),
        "target": str((destination.get("branch") or {}).get("name") or ""),
        "url": _href(node),
        "created": node.get("created_on") or "",
        "updated": node.get("updated_on") or "",
        "closed": "" if state == "open" else (node.get("updated_on") or ""),
        "body": body or "",
    }


def destination_revision(node: dict[str, Any]) -> str:
    """The destination commit a pull request is compared against."""
    return str((_endpoint(node, "destination").get("commit") or {}).get("hash") or "")


def comment(node: dict[str, Any]) -> dict[str, Any]:
    """A pull-request comment.

    A comment with `inline` is on a line of the diff: a `review_comment`
    whose file and line are `path` and `line`. Every other comment, a reply
    included, is a conversation (`issue`) comment. Comment ids are unique in
    the repository, so `ref` is unique however the kinds mix.
    """
    ident = node.get("id")
    inline = node.get("inline") if isinstance(node.get("inline"), dict) else {}
    kind = DIFF_NOTE if inline else CONVERSATION
    content = node.get("content") if isinstance(node.get("content"), dict) else {}
    return {
        "id": ident,
        "ref": f"{kind}-{ident}",
        "kind": kind,
        "author": actor(node.get("user")),
        "bot": is_automation(node.get("user")),
        "created": node.get("created_on") or "",
        "body": content.get("raw") or "",
        "url": _href(node),
        "path": inline.get("path") or "",
        "line": inline.get("to") or inline.get("from"),
    }


def is_deleted(node: dict[str, Any]) -> bool:
    return bool(node.get("deleted"))


def _author_name(node: dict[str, Any]) -> str:
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    user = author.get("user") if isinstance(author.get("user"), dict) else {}
    if user.get("display_name"):
        return str(user["display_name"])
    # `raw` is the git author line, `Name <email>`; the name is what a reader
    # of the other forges' answers sees.
    raw = str(author.get("raw") or "")
    return raw.split("<", 1)[0].strip() or raw


def commit(node: dict[str, Any]) -> dict[str, Any]:
    """One commit. Bitbucket Cloud gives one date, the author's."""
    return {
        "sha": node.get("hash") or "",
        "author": _author_name(node),
        "committed": node.get("date") or "",
        "message": node.get("message") or "",
        "url": _href(node),
    }
