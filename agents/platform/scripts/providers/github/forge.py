#!/usr/bin/env python3
"""GitHub: which calls to make, and nothing about how they are made.

Only the API is used, never `gh pr` or `gh issue`. Those subcommands infer the
repository from a nearby `.git/config` -- the one file this whole design exists
to keep out of the credentialed process -- and they format for a human, which
is not something a translation can be written against. So this class is a REST
client's *description* of a REST client: it names paths, parameters and bodies,
and the transport the broker built for it does the calling.

`transport = "cli"` is the only reason `gh` is in the broker image at all: it
was already there for the App installation flow, so borrowing it costs nothing.
That is a fact about this install's history rather than about GitHub, which is
why it is one word here and not a shape the interface has to have.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from ..base import COLLABORATION_VERBS, Forge, WorkspaceError, listing
from ..credentials import BrokeredCredential
from ..identity import SEGMENT_RE, path_segments
from ..validate import (
    validate_branch,
    validate_labels,
    validate_limit,
    validate_number,
    validate_state,
    validate_text,
)
from . import translate
from .errors import ERROR_OVERRIDES

# The media type that makes the pull-request endpoint answer with a unified
# diff instead of JSON.
DIFF_MEDIA_TYPE = "application/vnd.github.v3.diff"


class GitHubForge(Forge):
    name = "github"
    hosts = ("github.com", "www.github.com")
    proposal_noun = "pull request"
    verbs = COLLABORATION_VERBS
    transport = "cli"
    cli = "gh"
    error_overrides = ERROR_OVERRIDES

    def __init__(self, refresh: Callable[[str, str], None] | None = None) -> None:
        super().__init__()
        self.credential = BrokeredCredential(self.name, refresh)

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """Exactly one, always.

        An install has one GitHub or it has none, and "none" is not a state
        this repository has ever been in -- github.com is where it lives. The
        argument is read only for the refresh operation to hand the credential.
        """
        return (cls(refresh=config.get("refresh")),)

    # -- identity -----------------------------------------------------------

    def parse(self, url: str) -> str:
        parts = path_segments(url, self.hosts)
        if len(parts) != 2 or not all(SEGMENT_RE.match(part) for part in parts):
            raise WorkspaceError(
                f"{url!r} is not a GitHub repository; expected owner/name"
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"

    # -- shared by two verbs ------------------------------------------------

    def _comments(self, api: Callable, repo: str, number: int, payload: dict) -> list:
        # The issue-comments endpoint, and for a proposal too: on GitHub that is
        # the conversation, while `pulls/{n}/comments` is line notes on the
        # diff. A caller asking to read the discussion means the former.
        limit = validate_limit(payload.get("limit"))
        nodes = api(
            "GET",
            f"repos/{repo}/issues/{number}/comments",
            params={"per_page": limit},
        )
        return [translate.comment(node) for node in nodes]

    # -- proposals ----------------------------------------------------------

    def proposal_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        body = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "body": validate_text(payload.get("body"), "body", required=False),
            "head": validate_branch(payload.get("source"), "source"),
            "base": validate_branch(payload.get("target"), "target"),
        }
        if payload.get("draft"):
            body["draft"] = True
        node = api("POST", f"repos/{repo}/pulls", body=body)
        return {"proposal": translate.proposal(node)}

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        nodes = api(
            "GET",
            f"repos/{repo}/pulls",
            params={"state": validate_state(payload.get("state")), "per_page": limit},
        )
        return listing([translate.proposal(node) for node in nodes], limit, "proposals")

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/pulls/{number}")
        result: dict[str, Any] = {"proposal": translate.proposal(node)}
        if payload.get("comments"):
            result["comments"] = self._comments(api, repo, number, payload)
        if payload.get("diff"):
            result["diff"] = api(
                "GET", f"repos/{repo}/pulls/{number}", raw=DIFF_MEDIA_TYPE
            )
        return result

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node)}

    # -- issues -------------------------------------------------------------

    def issue_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "body": validate_text(payload.get("body"), "body", required=False),
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            body["labels"] = labels
        node = api("POST", f"repos/{repo}/issues", body=body)
        return {"issue": translate.issue(node)}

    def issue_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        params: dict[str, Any] = {
            "state": validate_state(payload.get("state")),
            "per_page": limit,
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            params["labels"] = ",".join(labels)
        nodes = api("GET", f"repos/{repo}/issues", params=params)
        # GitHub's issues endpoint returns pull requests too -- a PR *is* an
        # issue there. Nowhere else models it that way, and a caller that asked
        # for issues and got proposals mixed in would have to know that. The
        # `pull_request` key is how they are told apart.
        issues = [node for node in nodes if "pull_request" not in node]
        # `per_page` bounded what GitHub sent, not what survived the filter, so
        # `truncated` is judged on the page rather than on the remainder.
        return listing(
            [translate.issue(node) for node in issues],
            limit,
            "issues",
            returned=len(nodes),
        )

    def issue_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/issues/{number}")
        if "pull_request" in node:
            raise WorkspaceError(
                f"#{number} is a {self.proposal_noun}, not an issue; "
                "read it with `proposal view`"
            )
        result: dict[str, Any] = {"issue": translate.issue(node)}
        if payload.get("comments"):
            result["comments"] = self._comments(api, repo, number, payload)
        return result

    def issue_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node)}
