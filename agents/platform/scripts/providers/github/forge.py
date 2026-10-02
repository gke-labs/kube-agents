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
from urllib.parse import quote

import repo_ref

from ..base import COLLABORATION_VERBS, Forge, WorkspaceError, listing
from ..credentials import BrokeredCredential, Credential, MintedReadCredential, NoCredential
from ..validate import (
    repo_segments,
    validate_branch,
    validate_labels,
    validate_limit,
    validate_number,
    validate_page,
    validate_state,
    validate_text,
)
from . import translate
from .errors import ERROR_OVERRIDES

# The media type that makes the pull-request endpoint answer with a unified
# diff instead of JSON.
DIFF_MEDIA_TYPE = "application/vnd.github.v3.diff"


# `repos/{r}/collaborators/{login}/permission` values that mean "may write".
WRITE_PERMISSIONS = frozenset({"admin", "write", "maintain"})


class GitHubForge(Forge):
    name = "github"
    hosts = ("github.com", "www.github.com")
    proposal_noun = "pull request"
    verbs = COLLABORATION_VERBS
    transport = "cli"
    cli = "gh"
    error_overrides = ERROR_OVERRIDES
    acknowledges = True

    def __init__(
        self,
        refresh: Callable[[str, str], None] | None = None,
        mint: Callable[[str, str], str] | None = None,
    ) -> None:
        super().__init__()
        self.credential = BrokeredCredential(self.name, refresh)
        self._mint = mint

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """Exactly one, always.

        An install has one GitHub or it has none, and "none" is not a state
        this repository has ever been in -- github.com is where it lives. The
        argument is read only for the two privileged operations to hand the
        credentials: `refresh` for the write token, `mint` for a read-only one.
        """
        return (cls(refresh=config.get("refresh"), mint=config.get("mint")),)

    def read_credential(self, repo: str) -> Credential:
        """A `contents: read` App installation token for `repo`, per clone.

        Minted from the repository's own policy in the minter (the operator
        renders one per `context_repos` entry) by the executor, which refuses
        the mint for a repository not registered as context. Presented to git as
        an `extraheader` on this host, never installed. Without a mint operation
        -- an install with no minter -- there is nothing to present.
        """
        if self._mint is None:
            return NoCredential()
        return MintedReadCredential(self.name, self._mint, self.hosts[0])

    # -- identity -----------------------------------------------------------

    def parse(self, url: str) -> str:
        try:
            parts = repo_segments(url, self.hosts)
        except repo_ref.RepoRefError as error:
            raise WorkspaceError(
                f"{url!r} is not a GitHub repository; expected owner/name"
            ) from error
        if len(parts) != 2:
            raise WorkspaceError(
                f"{url!r} is not a GitHub repository; expected owner/name"
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"

    # -- shared by two verbs ------------------------------------------------

    def _comments(
        self, api: Callable, repo: str, number: int, payload: dict
    ) -> tuple[list, bool]:
        # The conversation tab. For an issue that is the whole discussion; for
        # a proposal it is one of three places -- see `_proposal_comments`.
        #
        # The second half of the answer is whether the page filled, and it is
        # not optional. A truncated conversation looks exactly like a complete
        # one, and the caller that reads a conversation is deciding which
        # requests it has already answered: a marker past the ceiling is a
        # marker it cannot see, so it answers the same request again on every
        # tick, forever. Saying so is what lets that caller refuse instead.
        limit = validate_limit(payload.get("limit"))
        nodes = api(
            "GET",
            f"repos/{repo}/issues/{number}/comments",
            params={"per_page": limit},
        )
        return [translate.comment(node, "issue") for node in nodes], len(nodes) >= limit

    def _proposal_comments(
        self, api: Callable, repo: str, number: int, payload: dict
    ) -> tuple[list, bool]:
        # GitHub splits one human-visible conversation across three endpoints:
        # the conversation tab, inline review comments on the diff, and the
        # summary body of a review. A reviewer typing "please fix this" has no
        # idea which one they used, so reading fewer than three means a caller
        # ignores requests at random. Each comment carries which one it came
        # from as `kind`, because that decides whether it can be acknowledged
        # (a review summary has no reaction endpoint) and whether `path` and
        # `line` mean anything. Oldest first, across all three.
        limit = validate_limit(payload.get("limit"))
        params = {"per_page": limit}
        out, truncated = self._comments(api, repo, number, payload)
        inline = api("GET", f"repos/{repo}/pulls/{number}/comments", params=params)
        out += [translate.comment(node, "review_comment") for node in inline]
        reviews = api("GET", f"repos/{repo}/pulls/{number}/reviews", params=params)
        out += [
            translate.comment(node, "review")
            for node in reviews
            # A review with no summary body is an approval or a state change,
            # not an utterance.
            if (node.get("body") or "").strip()
        ]
        # Any one of the three filling its page truncates the conversation, and
        # the reviews page is judged on what the forge sent rather than on what
        # survived the body test -- a page of bodiless approvals is still a
        # page, and there may be an utterance behind it.
        truncated = truncated or len(inline) >= limit or len(reviews) >= limit
        # `ref` and not `id` as the tie-break: two of these three endpoints
        # number independently, so a conversation comment and a review comment
        # can share an id and the order between them would depend on which of
        # two equal keys the sort happened to see first. `ref` carries the kind
        # as well, so it is unique across the merge and the order is stable.
        out.sort(key=lambda c: (c["created"], c["ref"]))
        return out, truncated

    @staticmethod
    def _label_changes(payload: dict) -> tuple[list[str], list[str]]:
        # Validated before the first call an update makes, so a bad label
        # cannot leave a half-applied edit behind.
        return validate_labels(payload.get("labelsAdd")), validate_labels(payload.get("labelsRemove"))

    def _labels(self, api: Callable, repo: str, number: int, payload: dict) -> None:
        # Labels live on the issue side of GitHub's model for proposals too.
        # Adds are one call; each removal is its own, because that is the API.
        add, remove = self._label_changes(payload)
        if add:
            api("POST", f"repos/{repo}/issues/{number}/labels", body={"labels": add})
        for name in remove:
            try:
                api("DELETE", f"repos/{repo}/issues/{number}/labels/{quote(name, safe='')}")
            except WorkspaceError as exc:
                # A label that is not on the issue is the state the caller asked
                # for, and GitHub answers 404 for it. Letting that through would
                # abort the whole update before the PATCH runs, so a `labelsAdd`
                # travelling with the removal would be dropped too: the resolver
                # sends `{labelsAdd: [status:<terminal>], labelsRemove:
                # [status:in-progress]}` in one call, and the stale sweep may
                # already have taken the claim label off. The `gh issue edit
                # --remove-label` calls this replaces were tolerant of it.
                if exc.status != 404:
                    raise

    def can_write(
        self, api: Callable, repo: str, login: str, bot: bool = False
    ) -> bool | None:
        # The collaborator-permission endpoint rather than `author_association`
        # off a comment: an App installation token sees every association as
        # NONE, which is the blindness forge.py's history records. A 404 is a
        # definitive no; any other failure is not an answer and says so.
        #
        # An automation's login gets its App spelling back before it is asked
        # about. `translate.actor` took `[bot]` off every author this forge
        # emitted, so a caller relaying a comment author has `renovate` in hand
        # for the App `renovate[bot]`, and the endpoint answers for whichever
        # principal it is given: bare, it is the permission of the *user*
        # `renovate` -- a stranger, or nobody (404, "is not a user") -- and
        # never the App's. The caller cannot re-add a suffix it was never
        # allowed to know about, which is why it says `bot` and this side
        # spells it.
        if not login:
            return False
        subject = login if not bot or login.endswith("[bot]") else f"{login}[bot]"
        quoted = quote(subject, safe="")
        try:
            data = api("GET", f"repos/{repo}/collaborators/{quoted}/permission")
        except WorkspaceError as exc:
            return False if exc.status == 404 else None
        permission = str((data or {}).get("permission") or "").strip().lower()
        return permission in WRITE_PERMISSIONS

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
        params: dict[str, Any] = {
            "state": validate_state(payload.get("state")),
            "per_page": limit,
        }
        # Sent only when it says something. The first page is what an
        # unqualified `GET /pulls` answers, and the recorded requests every
        # forge is tested against were taken without it.
        page = validate_page(payload.get("page"))
        if page > 1:
            params["page"] = page
        source = payload.get("source")
        if source is not None:
            # Asked as a filter rather than by listing everything and matching
            # on `source` here, because "is there an open proposal for the
            # branch I just published" is the question every submitting caller
            # asks, and a page of the newest twenty proposals answers it wrong
            # on a busy repository.
            #
            # The owner qualifier is this repository's own. The bare branch
            # name is also accepted here and matches the same branch on every
            # fork, which would let a fork's proposal answer for ours; a
            # published branch always lives on the repository itself.
            owner = repo.split("/")[0]
            params["head"] = f"{owner}:{validate_branch(source, 'source')}"
        target = payload.get("target")
        if target is not None:
            params["base"] = validate_branch(target, "target")
        nodes = api("GET", f"repos/{repo}/pulls", params=params)
        return listing([translate.proposal(node) for node in nodes], limit, "proposals")

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/pulls/{number}")
        result: dict[str, Any] = {"proposal": translate.proposal(node)}
        if payload.get("comments"):
            comments, truncated = self._proposal_comments(api, repo, number, payload)
            result["comments"] = comments
            result["commentCount"] = len(comments)
            result["commentsTruncated"] = truncated
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
        return {"comment": translate.comment(node, "issue")}

    def proposal_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        self._label_changes(payload)
        body: dict[str, Any] = {}
        if payload.get("title") is not None:
            body["title"] = validate_text(payload.get("title"), "title").strip()
        if payload.get("body") is not None:
            body["body"] = validate_text(payload.get("body"), "body", required=False)
        # One PATCH whatever was given, so the answer is always the proposal
        # as it now stands; GitHub returns it unchanged for an empty patch.
        # Labels first, then the PATCH: the answer is the proposal as it now
        # stands, and a read taken before the labels landed would report them
        # missing -- seen live on the first run of this verb.
        self._labels(api, repo, number, payload)
        node = api("PATCH", f"repos/{repo}/pulls/{number}", body=body)
        return {"proposal": translate.proposal(node)}

    def proposal_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("PATCH", f"repos/{repo}/pulls/{number}", body={"state": "closed"})
        return {"proposal": translate.proposal(node)}

    def proposal_commits(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        limit = validate_limit(payload.get("limit"))
        params: dict[str, Any] = {"per_page": limit}
        # Oldest first, which is GitHub's order for this endpoint and the one
        # the verb promises. The commit a caller most often wants is the newest,
        # so the caller that needs it walks to the last page -- and on a
        # proposal past 250 commits it never arrives at one. GitHub stops
        # serving there, so the page that reaches the cap is short, `listing`
        # reads a short page as the end and answers `truncated: false`, and the
        # last entry of it is the 250th commit rather than the branch tip. A
        # caller that needs the tip reads `sourceRevision` off the proposal
        # instead; `submit_suggestion.stale_tip` says why, and is the one that
        # was bitten.
        page = validate_page(payload.get("page"))
        if page > 1:
            params["page"] = page
        nodes = api("GET", f"repos/{repo}/pulls/{number}/commits", params=params)
        return listing([translate.commit(node) for node in nodes], limit, "commits")

    def proposal_acknowledge(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Best-effort by contract: a courtesy so the reviewer sees something
        # inside the tick. A review summary has no reaction endpoint, which is
        # `False` rather than an error.
        #
        # Validated although nothing here reads it: the reaction endpoint is
        # keyed on the comment alone, but `number` is in the verb's request
        # shape for the forges whose award-emoji route needs the proposal too.
        # A request every forge accepts and one forge refuses is the parity the
        # shared validators exist to hold, so it is checked where it is not
        # used.
        validate_number(payload.get("number"), "number")
        comment = payload.get("comment") or {}
        if not isinstance(comment, dict):
            raise WorkspaceError("comment must be the {id, kind} of a comment")
        kind = str(comment.get("kind") or "")
        ident = validate_number(comment.get("id"), "comment.id")
        if kind == "issue":
            path = f"repos/{repo}/issues/comments/{ident}/reactions"
        elif kind == "review_comment":
            path = f"repos/{repo}/pulls/comments/{ident}/reactions"
        else:
            return {"acknowledged": False}
        api("POST", path, body={"content": "eyes"})
        return {"acknowledged": True}

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
        # The negative half of the same filter. Asked of the forge rather than
        # applied to the answer here, because a page is not the repository: a
        # caller asking for "open issues nobody has claimed" on a repository
        # with a hundred claimed ones gets a full page of exclusions and an
        # empty result, which reads as a quiet repository. Every poller that
        # watches a queue by label needs this shape of question.
        excluded = validate_labels(payload.get("excludeLabels"))
        query = validate_text(payload.get("query"), "query", required=False).strip()
        if query or excluded:
            # The search API, whose query grammar is GitHub's own: the neutral
            # request is text plus the same state and labels, and this is where
            # they become `repo:`, `is:issue`, `label:` and `-label:`
            # qualifiers. The result envelope is `{items}`, unlike `/issues`.
            terms = ([query] if query else []) + [f"repo:{repo}", "is:issue"]
            if params["state"] != "all":
                terms.append(f"is:{params['state']}")
            terms += [f'label:"{name}"' for name in labels]
            terms += [f'-label:"{name}"' for name in excluded]
            found = api(
                "GET",
                "search/issues",
                # Newest first, explicitly. Unsorted, this endpoint answers in
                # relevance order, which is not an order the caller can predict
                # and not the one `/repos/{repo}/issues` uses -- so the same
                # verb with and without a filter returned differently ordered
                # pages, and `truncated` meant a different thing in each. A
                # caller that pages, or that ranks the page it got, needs the
                # window to be a window rather than a sample.
                params={
                    "q": " ".join(terms),
                    "per_page": limit,
                    "sort": "created",
                    "order": "desc",
                },
            )
            nodes = (found or {}).get("items") or []
        else:
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
            comments, truncated = self._comments(api, repo, number, payload)
            result["comments"] = comments
            result["commentCount"] = len(comments)
            result["commentsTruncated"] = truncated
        return result

    def issue_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node, "issue")}

    def issue_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        self._label_changes(payload)
        body: dict[str, Any] = {}
        if payload.get("title") is not None:
            body["title"] = validate_text(payload.get("title"), "title").strip()
        if payload.get("body") is not None:
            body["body"] = validate_text(payload.get("body"), "body", required=False)
        # Labels first, then the PATCH: the answer is the issue as it now
        # stands, and a read taken before the labels landed would report them
        # missing -- seen live on the first run of this verb.
        self._labels(api, repo, number, payload)
        node = api("PATCH", f"repos/{repo}/issues/{number}", body=body)
        return {"issue": translate.issue(node)}

    def issue_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        reason = validate_text(payload.get("reason"), "reason", required=False).strip()
        body: dict[str, Any] = {"state": "closed"}
        if reason:
            if reason not in ("completed", "not-planned"):
                raise WorkspaceError("reason must be one of completed, not-planned")
            body["state_reason"] = reason.replace("-", "_")
        node = api("PATCH", f"repos/{repo}/issues/{number}", body=body)
        return {"issue": translate.issue(node)}

    # -- labels -------------------------------------------------------------

    def label_ensure(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Read, then create or update. Creating first and reading the 422 back
        # would work on GitHub and nowhere else; a read that 404s is the
        # portable spelling of "does not exist yet".
        name = validate_labels([payload.get("name")])[0]
        body: dict[str, Any] = {"name": name}
        color = validate_text(payload.get("color"), "color", required=False).strip().lstrip("#")
        if color:
            body["color"] = color
        description = validate_text(payload.get("description"), "description", required=False)
        if description:
            body["description"] = description
        quoted = quote(name, safe="")
        try:
            api("GET", f"repos/{repo}/labels/{quoted}")
        except WorkspaceError as exc:
            if exc.status != 404:
                raise
            node = api("POST", f"repos/{repo}/labels", body=body)
        else:
            node = api("PATCH", f"repos/{repo}/labels/{quoted}", body=body)
        return {"label": translate.label(node)}
