#!/usr/bin/env python3
"""Gitea: which calls to make, and nothing about how they are made.

A self-managed forge. There is no gitea.com an install is assumed to use, so
`for_config` builds one instance per declaration the administrator wrote --
zero, one or several, each at its own host -- and an install with none builds
nothing. Each instance holds its own origin (`scheme://host[:port]`), and every
URL it hands out is composed from that origin and validated path segments,
never from the caller's URL.

`transport = "http"`: Gitea's REST API takes a token in one header, so the
broker calls it in-process and no binary is added to the credentialed
container. The token is a long-lived one an administrator minted in Gitea and
stored in a Secret; `StaticFileCredential` reads it from the mounted file at
each use, presents it to the API as `Authorization: token <t>`, and to git
through the token-file credential helper scoped to this instance's origin.

Plain `http` is accepted only when a declaration says so. It exists for an
in-cluster Gitea reached over a Service, where TLS terminates nowhere; the
token then crosses the pod network in the clear, which is the administrator's
call to make and is why it is never the default.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

import repo_ref

from ..base import COLLABORATION_VERBS, Forge, WorkspaceError, listing
from ..credentials import Credential, StaticFileCredential
from ..validate import (
    repo_segments,
    validate_branch,
    validate_comment_limit,
    validate_labels,
    validate_limit,
    validate_number,
    validate_page,
    validate_state,
    validate_text,
)
from . import translate
from .errors import ERROR_OVERRIDES

# The `provider` value a declaration names this forge by.
PROVIDER = "gitea"
# The schemes a declaration may name. https is the default and needs no
# mention; http has to be written down.
DEFAULT_SCHEME = "https"
ALLOWED_SCHEMES = frozenset({"https", "http"})
# Where Gitea serves its REST API under the instance root.
API_ROOT = "/api/v1"
# The git Basic-auth username Gitea accepts alongside an access token password.
GIT_USERNAME = "x-access-token"
# A declaration's name, host and port, validated here because the operator's
# schema is a second check and not the only one.
FORGE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
MIN_PORT = 1
MAX_PORT = 65535
# A Gitea repository is exactly `owner/name`.
PATH_DEPTH = 2
# The most items one Gitea list call returns with the default
# `[api] MAX_RESPONSE_ITEMS`. Every list is read in pages of this size and the
# caller's page and limit are counted over what survives any filter.
FORGE_PAGE_SIZE = 50
# How many forge pages one listing reads before it stops and reports itself
# truncated. Bounded so a filter almost nothing matches cannot make one call
# walk a whole tracker.
MAX_LIST_PAGES = 20
# How many of a pull request's commits `proposal_commits` reads -- the same
# ceiling GitHub and GitLab serve, so the verb answers the same depth.
COMMIT_CAP = 250
# How many pages of the token account's repositories `reach` reads.
REACH_PAGES = 10
# The media type that has no meaning to Gitea's `.diff` route but says to the
# transport "return the body as text".
DIFF_MEDIA_TYPE = "text/plain"
# The colour a label is created with when the caller names none. Gitea refuses
# a label without one.
DEFAULT_LABEL_COLOR = "ededed"
# The reaction `proposal-acknowledge` leaves.
ACKNOWLEDGE_REACTION = "eyes"
# `collaborators/{login}/permission` values that mean "may write".
WRITE_PERMISSIONS = frozenset({"owner", "admin", "write"})
# The reasons `issue-close` accepts. Gitea records no reason, so a valid one is
# accepted and not sent; an invalid one is refused, as on every forge.
CLOSE_REASONS = ("completed", "not-planned")


class GiteaForge(Forge):
    name = PROVIDER
    proposal_noun = "pull request"
    verbs = COLLABORATION_VERBS
    transport = "http"
    cli = ""
    schemes = ("https", "http")
    accepts_port = True
    error_overrides = ERROR_OVERRIDES
    acknowledges = True
    # Issues and pull requests carry labels; `label-ensure` creates them and
    # the list verbs match them by name.
    labels = True
    whoami_route = ("user", "login")
    COMMIT_CAP = COMMIT_CAP
    REACH_PAGES = REACH_PAGES

    def __init__(
        self,
        host: str,
        token_path: str,
        *,
        label: str = PROVIDER,
        scheme: str = DEFAULT_SCHEME,
        port: int | None = None,
        allowed_paths: Iterable[str] | None = None,
        ca_file: str = "",
        ca_source: str = "",
    ) -> None:
        super().__init__()
        if not FORGE_NAME_RE.match(label):
            raise ValueError(f"name {label!r} is not a DNS label")
        host = (host or "").strip().lower()
        if not host or not HOST_RE.match(host):
            raise ValueError(f"forge {label}: host {host!r} is not a hostname")
        scheme = (scheme or DEFAULT_SCHEME).strip().lower()
        if scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"forge {label}: scheme {scheme!r} is not one of https, http")
        if scheme == "http" and not host.endswith((".svc", ".svc.cluster.local")):
            raise ValueError(
                f"forge {label}: scheme 'http' is only supported for in-cluster "
                f"Kubernetes Services (*.svc or *.svc.cluster.local), got {host!r}"
            )
        if ca_file and scheme == "http":
            raise ValueError(f"forge {label}: ca_file cannot be set when scheme is http")
        if port is not None:
            if isinstance(port, bool) or not isinstance(port, int):
                raise ValueError(f"forge {label}: port must be a number")
            if not MIN_PORT <= port <= MAX_PORT:
                raise ValueError(f"forge {label}: port {port} is out of range")
        prefixes = []
        for path in allowed_paths or ():
            if not isinstance(path, str):
                raise ValueError(f"allowedPaths entry {path!r} for {host} must be a string")
            trimmed = path.strip("/")
            if not trimmed:
                raise ValueError(
                    f"an allowedPaths entry for {host} names no namespace ({path!r}); "
                    "write the namespace, or [] on its own for the whole host"
                )
            prefixes.append(self._prefix(host, path, trimmed))
        self.label = label
        self.host = host
        self.scheme = scheme
        self.port = port
        self.ca_file = ca_file
        self.ca_source = ca_source
        self.hosts = (host,)
        self.schemes = (scheme,)
        self.allowed_paths = tuple(prefixes)
        authority = f"{host}:{port}" if port else host
        self.origin = f"{scheme}://{authority}"
        self.api_url = f"{self.origin}{API_ROOT}"
        self.credential: Credential = StaticFileCredential(
            token_path,
            authority,
            header="Authorization",
            header_format="token {token}",
            username=GIT_USERNAME,
            scheme=scheme,
        )

    @staticmethod
    def _prefix(host: str, path: str, trimmed: str) -> tuple[str, ...]:
        if any(character.isspace() for character in path):
            raise ValueError(
                f"allowedPaths entry {path!r} for {host} contains whitespace"
            )
        try:
            segments = repo_segments(trimmed, (host,))
        except repo_ref.RepoRefError as error:
            raise ValueError(
                f"allowedPaths entry {trimmed!r} for {host} is not a namespace "
                "path a repository can have"
            ) from error
        if not segments or len(segments) > PATH_DEPTH:
            raise ValueError(
                f"an allowedPaths entry for {host} must be owner or owner/name ({path!r})"
            )
        if segments[0].casefold() == host.casefold():
            raise ValueError(
                f"allowedPaths entry {path!r} names the host {host}, not a namespace "
                "on it; list namespaces, or [] for the whole host"
            )
        return tuple(segment.casefold() for segment in segments)

    # -- registration -------------------------------------------------------

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """One instance per configured Gitea host; none when none is configured."""
        built: list[Forge] = []
        for entry in config.get("forges") or ():
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("provider") or "").strip().lower() != PROVIDER:
                continue
            token_path = str(entry.get("token_path") or "").strip()
            if not token_path:
                raise ValueError(f"the {cls.name} forge at {entry.get('host')} names no tokenPath")
            allowed = entry.get("allowed_paths")
            if allowed is None and "allowedPaths" in entry:
                allowed = entry.get("allowedPaths")
            if allowed is None:
                raise ValueError(
                    f"the {cls.name} forge at {entry.get('host')} names no allowedPaths: "
                    "list the namespaces it may reach, or [] for the whole host"
                )
            label = str(entry.get("name") or PROVIDER).strip()
            host = str(entry.get("host") or "").strip().lower()
            scheme = str(entry.get("scheme") or DEFAULT_SCHEME).strip().lower()
            raw_port = entry.get("port")
            port = None if raw_port in (None, "", 0) else raw_port
            built.append(
                cls(
                    host,
                    token_path,
                    label=label,
                    scheme=scheme,
                    port=port,
                    allowed_paths=allowed,
                    ca_file=str(entry.get("ca_file") or ""),
                    ca_source=str(entry.get("ca_source") or ""),
                )
            )
        return tuple(built)

    def reach(self, api: Callable) -> tuple[list[str], bool]:
        """Every repository the token's account can access (`GET /api/v1/user/repos`)."""
        paths: list[str] = []
        for page in range(1, self.REACH_PAGES + 1):
            batch = (
                api(
                    "GET",
                    "user/repos",
                    params={"limit": FORGE_PAGE_SIZE, "page": page},
                )
                or []
            )
            if not batch:
                return [path for path in paths if path], False
            paths += [
                str(item.get("full_name") or "")
                for item in batch
                if isinstance(item, dict)
            ]
        return [path for path in paths if path], True

    # -- identity -----------------------------------------------------------

    def parse(self, url: str) -> str:
        """`owner/name`, from a URL on this instance's host or `host/owner/name`.

        The port in a URL is not compared. The clone URL and every API call are
        composed from the declared origin, so a URL naming another port on the
        same host still reaches the declared one and no other. Git's scp syntax
        (`[user@]host:owner/name`) has no port field, so a schemeless
        `host:port/...` is refused rather than guessed at.
        """
        error = WorkspaceError(f"{url!r} is not a repository on {self.host}; expected owner/name")
        stripped = url.strip() if isinstance(url, str) else ""
        scp = repo_ref.SCP_REMOTE_RE.match(stripped) if "://" not in stripped else None
        try:
            parts = repo_segments(url, self.hosts)
        except repo_ref.RepoRefError as exc:
            raise error from exc
        if scp is not None and parts and parts[0].isdigit() and (
            "@" not in stripped[: scp.start("host")] or len(parts) != PATH_DEPTH
        ):
            raise WorkspaceError(
                f"{url!r} is not a repository on {self.host}; write a repository "
                f"with a port as a full URL ({self.origin}/owner/name)"
            )
        if len(parts) != PATH_DEPTH:
            raise error
        folded = tuple(part.casefold() for part in parts)
        if self.allowed_paths and not any(
            folded[: len(prefix)] == prefix for prefix in self.allowed_paths
        ):
            raise WorkspaceError(
                f"{'/'.join(parts)} is outside the paths this install's {self.name} "
                f"forge at {self.host} is allowed to act on",
                status=403,
                code="REPOSITORY_NOT_ALLOWED",
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"{self.origin}/{repo}.git"

    def file_url(self, repo: str) -> str:
        return f"{self.origin}/{repo}/src/commit/{{ref}}/{{path}}"

    def can_write(
        self, api: Callable, repo: str, login: str, bot: bool = False
    ) -> bool | None:
        # Gitea has no separate spelling for an automation's login, so `bot`
        # changes nothing here. 404 is a definitive no (no such user); any
        # other failure is not an answer and says so.
        if not login:
            return False
        try:
            data = api("GET", f"repos/{repo}/collaborators/{quote(login, safe='')}/permission")
        except WorkspaceError as exc:
            return False if exc.status == 404 else None
        permission = str((data or {}).get("permission") or "").strip().lower()
        return permission in WRITE_PERMISSIONS

    # -- paging -------------------------------------------------------------

    @staticmethod
    def _walk(
        api: Callable,
        path: str,
        params: dict[str, Any],
        *,
        page: int,
        limit: int,
        keep: Callable[[dict], bool] | None = None,
    ) -> tuple[list[dict], bool]:
        """The caller's page of a Gitea listing, and whether the forge holds more.

        Gitea clamps `limit` to the server's `[api] MAX_RESPONSE_ITEMS`, which
        an administrator may lower below `FORGE_PAGE_SIZE`, so a page shorter
        than `FORGE_PAGE_SIZE` is not proof that the listing has ended. Pages
        are read from the top until one surviving item past the caller's page
        has been seen (truncated), or an empty batch confirms the end, or
        `MAX_LIST_PAGES` fetches have been spent (truncated).
        """
        skip = (page - 1) * limit
        out: list[dict] = []
        for forge_page in range(1, MAX_LIST_PAGES + 1):
            batch = api(
                "GET", path, params={**params, "page": forge_page, "limit": FORGE_PAGE_SIZE}
            ) or []
            if not batch:
                return out[:limit], len(out) > limit
            kept = [node for node in batch if keep is None or keep(node)]
            dropped = min(skip, len(kept))
            skip -= dropped
            out += kept[dropped:]
            if len(out) > limit:
                return out[:limit], True
        return out[:limit], True

    @staticmethod
    def _conversation_pages(api: Callable, path: str, limit: int) -> tuple[list, bool]:
        """Up to `limit` nodes from a paged comment endpoint, and whether it held more.

        Because `[api] MAX_RESPONSE_ITEMS` on the server may be smaller than
        `per_page`, only an empty batch proves the endpoint has no more rows.
        Reaching `limit` itself marks the conversation truncated, as in `_whole`.
        """
        per_page = min(limit, FORGE_PAGE_SIZE)
        nodes: list = []
        for page in range(1, MAX_LIST_PAGES + 1):
            batch = api("GET", path, params={"page": page, "limit": per_page}) or []
            nodes += batch
            if not batch or len(nodes) >= limit:
                return nodes[:limit], len(nodes) >= limit
        return nodes[:limit], True

    @staticmethod
    def _whole(nodes: list, limit: int) -> tuple[list, bool]:
        """An unpaged endpoint's answer, cut to `limit`.

        Gitea serves an issue's comments and a review's comments in one answer
        with no paging. Judged as a page of `limit` would be -- full is
        truncated -- so a conversation that exactly fills the caller's limit
        reads as one that may hold more, which is the safe side for a caller
        deciding what it has already answered.
        """
        nodes = list(nodes or [])
        return nodes[:limit], len(nodes) >= limit

    # -- labels -------------------------------------------------------------

    def _repo_labels(self, api: Callable, repo: str) -> list[dict]:
        labels, _ = self._walk(
            api, f"repos/{repo}/labels", {}, page=1, limit=FORGE_PAGE_SIZE * MAX_LIST_PAGES
        )
        return labels

    def _label_ids(self, api: Callable, repo: str, names: list[str]) -> list[int]:
        """The ids of `names`, creating any the repository does not have yet.

        Gitea attaches labels by id, and a label that does not exist is not
        one it will attach. GitHub creates a missing label when an issue names
        it, and the shipped callers rely on that -- they put a status label on
        an issue without ensuring it first -- so the same request has to work
        here. A label made this way gets `DEFAULT_LABEL_COLOR`, as on GitHub.
        """
        if not names:
            return []
        existing = {
            str(node.get("name") or "").casefold(): node.get("id")
            for node in self._repo_labels(api, repo)
        }
        ids: list[int] = []
        for name in names:
            ident = existing.get(name.casefold())
            if ident is None:
                node = api(
                    "POST",
                    f"repos/{repo}/labels",
                    body={"name": name, "color": DEFAULT_LABEL_COLOR},
                ) or {}
                ident = node.get("id")
                existing[name.casefold()] = ident
            ids.append(ident)
        return ids

    @staticmethod
    def _label_changes(payload: dict) -> tuple[list[str], list[str]]:
        return validate_labels(payload.get("labelsAdd")), validate_labels(payload.get("labelsRemove"))

    def _labels(self, api: Callable, repo: str, number: int, payload: dict) -> None:
        # Adds are one call by id. A removal needs the id too, read off the
        # item's own labels; a label not on the item is the state the caller
        # asked for and is skipped, as GitHub's 404 is tolerated.
        add, remove = self._label_changes(payload)
        if add:
            api(
                "POST",
                f"repos/{repo}/issues/{number}/labels",
                body={"labels": self._label_ids(api, repo, add)},
            )
        if remove:
            current = {
                str(node.get("name") or "").casefold(): node.get("id")
                for node in api("GET", f"repos/{repo}/issues/{number}/labels") or []
            }
            for name in remove:
                ident = current.get(name.casefold())
                if ident is None:
                    continue
                try:
                    api("DELETE", f"repos/{repo}/issues/{number}/labels/{ident}")
                except WorkspaceError as exc:
                    if exc.status != 404:
                        raise

    # -- proposals ----------------------------------------------------------

    def proposal_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        title = validate_text(payload.get("title"), "title").strip()
        body = {
            "title": f"{translate.DRAFT_PREFIX}{title}" if payload.get("draft") else title,
            "body": validate_text(payload.get("body"), "body", required=False),
            "head": validate_branch(payload.get("source"), "source"),
            "base": validate_branch(payload.get("target"), "target"),
        }
        node = api("POST", f"repos/{repo}/pulls", body=body)
        return {"proposal": translate.proposal(node)}

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        page = validate_page(payload.get("page"))
        params: dict[str, Any] = {"state": validate_state(payload.get("state"))}
        target = payload.get("target")
        if target is not None:
            params["base_branch"] = validate_branch(target, "target")
        source = payload.get("source")
        branch = validate_branch(source, "source") if source is not None else None
        wanted = {label.casefold() for label in validate_labels(payload.get("labels"))}
        keep = None
        if branch is not None or wanted:
            # Gitea's list takes no head filter, and its label filter takes
            # ids. Both are matched here, on the head's repository as well as
            # its name: a fork's branch of the same name is not ours.
            def keep(node: dict) -> bool:
                item = translate.proposal(node)
                if branch is not None and (
                    item["source"] != branch or item["sourceRepo"].lower() != repo.lower()
                ):
                    return False
                return wanted <= {label.casefold() for label in item["labels"]}

        nodes, truncated = self._walk(
            api, f"repos/{repo}/pulls", params, page=page, limit=limit, keep=keep
        )
        proposals = [translate.proposal(node) for node in nodes]
        return {"proposals": proposals, "count": len(proposals), "truncated": truncated}

    def _proposal_comments(
        self, api: Callable, repo: str, number: int, payload: dict
    ) -> tuple[list, bool]:
        # Three places, as on GitHub: the conversation, review summaries, and
        # inline comments on the diff. Gitea serves the inline ones per review,
        # so they are read for each review that says it has any.
        limit = validate_comment_limit(payload.get("limit"))
        nodes, truncated = self._whole(api("GET", f"repos/{repo}/issues/{number}/comments"), limit)
        out = [translate.comment(node, "issue") for node in nodes]
        reviews, reviews_full = self._conversation_pages(
            api, f"repos/{repo}/pulls/{number}/reviews", limit
        )
        truncated = truncated or reviews_full
        for review in reviews:
            if (review.get("body") or "").strip():
                out.append(translate.comment(review, "review"))
            if int(review.get("comments_count") or 0) > 0:
                inline, inline_full = self._whole(
                    api("GET", f"repos/{repo}/pulls/{number}/reviews/{review.get('id')}/comments"),
                    limit,
                )
                out += [translate.comment(node, "review_comment") for node in inline]
                truncated = truncated or inline_full
        out.sort(key=lambda c: (c["created"], c["ref"]))
        return out, truncated

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
            result["diff"] = api("GET", f"repos/{repo}/pulls/{number}.diff", raw=DIFF_MEDIA_TYPE)
        return result

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node, "issue")}

    def _require_pull(self, api: Callable, repo: str, number: int) -> dict:
        return api("GET", f"repos/{repo}/pulls/{number}") or {}

    def proposal_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        self._label_changes(payload)
        body: dict[str, Any] = {}
        title = (
            validate_text(payload.get("title"), "title").strip()
            if payload.get("title") is not None
            else None
        )
        if payload.get("body") is not None:
            body["body"] = validate_text(payload.get("body"), "body", required=False)
        current = self._require_pull(api, repo, number)
        if title is not None:
            # Draft lives in the title here. A new title on a draft keeps the
            # prefix, or the edit would also mark the proposal ready.
            if not translate.split_draft(title)[1] and translate.proposal(current)["draft"]:
                title = f"{translate.DRAFT_PREFIX}{title}"
            body["title"] = title
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
        page = validate_page(payload.get("page"))
        # Gitea lists a pull request's commits newest first and has no
        # parameter to turn that round; the verb promises oldest first, with
        # page 1 holding the oldest. Reversing each page would not do it -- on
        # a pull request longer than one page, page 1 would be the newest
        # commits -- so the list is read whole, up to the ceiling, and paged
        # here. `verification` and `files` are off: each costs the server a
        # pass over every commit and neither is in the answer.
        path = f"repos/{repo}/pulls/{number}/commits"
        nodes: list[dict] = []
        for fetch in range(1, MAX_LIST_PAGES + 1):
            batch = api(
                "GET",
                path,
                params={
                    "verification": "false",
                    "files": "false",
                    "page": fetch,
                    "limit": FORGE_PAGE_SIZE,
                },
            ) or []
            if not batch:
                break
            nodes.extend(batch)
            if len(nodes) >= self.COMMIT_CAP:
                break
        commits = [translate.commit(node) for node in reversed(nodes[: self.COMMIT_CAP])]
        return listing(commits[(page - 1) * limit : page * limit], limit, "commits")

    def proposal_acknowledge(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # A conversation comment and an inline review comment are both
        # comments to Gitea's reaction route; a review summary has none, which
        # is `False` rather than an error. `number` is validated although the
        # route does not take it, for parity with every forge.
        validate_number(payload.get("number"), "number")
        comment = payload.get("comment") or {}
        if not isinstance(comment, dict):
            raise WorkspaceError("comment must be the {id, kind} of a comment")
        kind = str(comment.get("kind") or "")
        ident = validate_number(comment.get("id"), "comment.id")
        if kind not in ("issue", "review_comment"):
            return {"acknowledged": False}
        api(
            "POST",
            f"repos/{repo}/issues/comments/{ident}/reactions",
            body={"content": ACKNOWLEDGE_REACTION},
        )
        return {"acknowledged": True}

    # -- issues -------------------------------------------------------------

    def issue_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "body": validate_text(payload.get("body"), "body", required=False),
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            body["labels"] = self._label_ids(api, repo, labels)
        node = api("POST", f"repos/{repo}/issues", body=body)
        return {"issue": translate.issue(node)}

    def _require_issue(self, api: Callable, repo: str, number: int) -> dict:
        node = api("GET", f"repos/{repo}/issues/{number}") or {}
        if node.get("pull_request"):
            raise WorkspaceError(
                f"#{number} is a {self.proposal_noun}, not an issue; "
                "read it with `proposal view`"
            )
        return node

    def issue_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        page = validate_page(payload.get("page"))
        params: dict[str, Any] = {
            "state": validate_state(payload.get("state")),
            # Gitea's issue list carries pull requests unless told not to.
            "type": "issues",
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            params["labels"] = ",".join(labels)
        query = validate_text(payload.get("query"), "query", required=False).strip()
        if query:
            params["q"] = query
        excluded = {label.casefold() for label in validate_labels(payload.get("excludeLabels"))}
        wanted = {label.casefold() for label in labels}

        # Gitea has no negative label filter, so the exclusion is applied
        # here -- across pages, counted over what survives, so a page of
        # claimed issues does not read as a quiet queue. Any pull-request row
        # is dropped before counting too.
        def keep(node: dict) -> bool:
            if node.get("pull_request"):
                return False
            if not excluded and not wanted:
                return True
            names = {
                str(item.get("name") or "").casefold()
                for item in (node.get("labels") or [])
                if isinstance(item, dict)
            }
            return wanted <= names and not (excluded & names)

        nodes, truncated = self._walk(
            api, f"repos/{repo}/issues", params, page=page, limit=limit, keep=keep
        )
        issues = [translate.issue(node) for node in nodes]
        return {"issues": issues, "count": len(issues), "truncated": truncated}

    def issue_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = self._require_issue(api, repo, number)
        result: dict[str, Any] = {"issue": translate.issue(node)}
        if payload.get("comments"):
            limit = validate_comment_limit(payload.get("limit"))
            nodes, truncated = self._whole(
                api("GET", f"repos/{repo}/issues/{number}/comments"), limit
            )
            comments = [translate.comment(item, "issue") for item in nodes]
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
        self._require_issue(api, repo, number)
        self._labels(api, repo, number, payload)
        node = api("PATCH", f"repos/{repo}/issues/{number}", body=body)
        return {"issue": translate.issue(node)}

    def issue_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        reason = validate_text(payload.get("reason"), "reason", required=False).strip()
        if reason and reason not in CLOSE_REASONS:
            raise WorkspaceError(f"reason must be one of {', '.join(CLOSE_REASONS)}")
        self._require_issue(api, repo, number)
        node = api("PATCH", f"repos/{repo}/issues/{number}", body={"state": "closed"})
        return {"issue": translate.issue(node)}

    # -- labels -------------------------------------------------------------

    def label_ensure(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Read, then create or update. Gitea has no read-by-name route, so the
        # read is the repository's label list, matched without case as Gitea
        # matches names.
        name = validate_labels([payload.get("name")])[0]
        color = validate_text(payload.get("color"), "color", required=False).strip().lstrip("#")
        description = validate_text(payload.get("description"), "description", required=False)
        body: dict[str, Any] = {"name": name}
        if color:
            body["color"] = color
        if description:
            body["description"] = description
        found = next(
            (
                node
                for node in self._repo_labels(api, repo)
                if str(node.get("name") or "").casefold() == name.casefold()
            ),
            None,
        )
        if found is None:
            body.setdefault("color", DEFAULT_LABEL_COLOR)
            node = api("POST", f"repos/{repo}/labels", body=body)
        else:
            node = api("PATCH", f"repos/{repo}/labels/{found.get('id')}", body=body)
        return {"label": translate.label(node)}
