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


def _merge_commit(node: dict[str, Any]) -> str | None:
    merged = node.get("merge_commit")
    return (str(merged.get("hash") or "") or None) if isinstance(merged, dict) else None


def proposal(node: dict[str, Any], repo: str = "") -> dict[str, Any]:
    """A pull request as a proposal.

    `closed` is when it stopped being open. Bitbucket Cloud records no close
    time, so it is the last update of a pull request that is no longer open,
    and "" while it is open.

    `mergeCommit` is `merge_commit.hash`, `None` unless merged: the merge or
    squash commit, or after a fast-forward merge the target's tip, which may
    not hold the whole change. Bitbucket abbreviates it to 12 characters, as
    it does the source revision, and the translation keeps what it was given.
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
        "mergeCommit": _merge_commit(node) if state == "merged" else None,
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


def _author_email(node: dict[str, Any]) -> str:
    """The git author email, from the `Name <email>` line."""
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    raw = str(author.get("raw") or "")
    if "<" not in raw or not raw.rstrip().endswith(">"):
        return ""
    return raw.rsplit("<", 1)[1].rstrip().rstrip(">").strip()


def commit(node: dict[str, Any]) -> dict[str, Any]:
    """One commit. Bitbucket Cloud gives one date, the author's.

    `email` is the git author email and `bot` whether the linked account is an
    access token's bot user; False when no account is linked, as for the
    install's own commits, whose address Bitbucket links to nobody.
    """
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    linked = author.get("user") if isinstance(author.get("user"), dict) else None
    return {
        "sha": node.get("hash") or "",
        # The login of the linked account, as every forge's commit author is,
        # so that the readings can tell the install's own commits; the git
        # author name when no account is linked.
        "author": actor(linked) or _author_name(node),
        "email": _author_email(node),
        "bot": is_automation(linked),
        "committed": node.get("date") or "",
        "message": node.get("message") or "",
        "url": _href(node),
    }


def authored(node: dict[str, Any]) -> dict[str, Any]:
    """A pull request as one of an author's proposals."""
    item = proposal(node)
    return {
        "number": item["number"],
        "state": item["state"],
        "author": item["author"],
        "authorBot": is_automation(node.get("author")),
        "created": item["created"],
        "updated": item["updated"],
        "closed": item["closed"],
    }


#: The activity rows that are reviews, as the neutral review state.
REVIEW_ROWS = {"approval": "approved", "changes_requested": "changes_requested"}


def review(row: dict[str, Any]) -> dict[str, Any] | None:
    """An approval or a request for changes in the activity stream; None otherwise.

    An activity row has no id of its own, so the review's id is the kind, the
    account and the time, which is unique for one pull request.
    """
    for key, state in REVIEW_ROWS.items():
        node = row.get(key)
        if isinstance(node, dict):
            who = node.get("user") if isinstance(node.get("user"), dict) else None
            when = str(node.get("date") or "")
            return {
                "id": f"{key}:{actor(who)}:{when}",
                "state": state,
                "author": actor(who),
                "bot": is_automation(who),
                "created": when,
            }
    return None


def closer(node: dict[str, Any]) -> dict[str, Any]:
    """Who closed a pull request: its `closed_by`; "" when Bitbucket does not say."""
    who = node.get("closed_by") if isinstance(node.get("closed_by"), dict) else None
    return {"actor": actor(who), "bot": is_automation(who)}
