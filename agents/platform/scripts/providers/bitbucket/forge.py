#!/usr/bin/env python3
"""Bitbucket Cloud: which calls to make, and nothing about how they are made.

One host, bitbucket.org, and its API at api.bitbucket.org/2.0. The transport
is the broker's in-process HTTP client. The credential is a token that an
administrator stored in a Secret: a workspace access token (Premium), the API
token of an account that exists only for this install (any plan), or a
repository access token for an install with one repository. All of them are
sent as `Authorization: Bearer` to the API, and to git with the username
`x-token-auth`. `allowed_paths` narrows the token to the workspaces this
install acts on before it is spent. A second, read-only token in the same
Secret, when there is one, is what a context repository clones with
(`read_credential`).

What Bitbucket Cloud does differently from the shapes every caller expects,
and where it is absorbed:

- no issue tracker and no labels: the issue verbs and `label-ensure` are not
  in `verbs`, `labels` is False, and the broker refuses a label field before
  it reaches this package;
- no reaction on a comment: `acknowledges` is False;
- listings are pages with an opaque `next` link, followed inside the package
  as a path relative to the API root (`_pages`);
- the pull-request diff route answers 302, which the transport refuses, so the
  diff is asked of the repository diff route with the pull request's two
  commits, which is where the redirect points (`_diff`);
- a pull request names its source commit by a 12-character hash, which is
  expanded where the revision decides something (`_full_revision`);
- whether another account may write needs repository admin on the token, so
  `can_write` answers None (unknown);
- the account a token acts as: an API token can read it at `/user`; an access
  token cannot, so the forge names the login from its configured identity
  (`whoami`), and an unknown one is refused, never "do not compare".
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

import repo_ref

from ..base import Forge, WorkspaceError, listing
from ..credentials import Credential, NoCredential, StaticFileCredential
from ..validate import (
    MAX_CONVERSATION_SIZE,
    repo_segments,
    validate_branch,
    validate_comment_limit,
    validate_limit,
    validate_number,
    validate_page,
    validate_state,
    validate_text,
)
from . import translate
from .errors import ERROR_OVERRIDES, GIT_REFUSED

#: The one host Bitbucket Cloud serves repositories at, and its other spelling.
HOST = repo_ref.BITBUCKET_CANONICAL_HOST
HOSTS = (HOST, "www.bitbucket.org")

#: The REST API root. Not on `HOST`: every API path is relative to this.
API_URL = "https://api.bitbucket.org/2.0"

#: The username git is given beside the token. Bitbucket reads the token and
#: accepts this username for every token type it recommends.
GIT_USERNAME = "x-token-auth"

#: How the API is handed the token.
TOKEN_HEADER = "Authorization"
TOKEN_FORMAT = "Bearer {token}"

#: The most rows one page of pull requests holds. The API's documented ceiling
#: is 100, and it says that "individual APIs may enforce different values";
#: the pull-request list enforces 50 (verify live).
PROPOSAL_PAGE = 50

#: The most rows one page of comments or commits holds.
PAGE = 100

#: A commit hash as Bitbucket writes one: 7 to 40 hexadecimal characters.
_HASH_RE = re.compile(r"[0-9a-f]{7,40}")

#: A full revision id.
_FULL_HASH_RE = re.compile(r"[0-9a-f]{40}")

#: The characters a BBQL string value may hold here. Branch names have already
#: passed `validate_branch`, which allows none of `"` or `\`.
_BBQL_SAFE = re.compile(r"[A-Za-z0-9._/-]+")


def _allowed_prefix(path: str) -> str:
    """One `allowedPaths` entry: a workspace, as `parse` would read it.

    A workspace is the first segment of every repository path, and the only
    one a token can be narrowed by, so an entry is exactly one segment. It is
    read by the parser `parse` uses, so the two cannot disagree.
    """
    if any(character.isspace() for character in path):
        raise ValueError(f"allowedPaths entry {path!r} for {HOST} contains whitespace")
    trimmed = path.strip("/")
    if not trimmed:
        raise ValueError(
            f"an allowedPaths entry for {HOST} names no workspace ({path!r}); "
            "write the workspace, or [] on its own for every workspace the token reaches"
        )
    try:
        segments = repo_segments(trimmed, HOSTS)
    except repo_ref.RepoRefError as error:
        raise ValueError(
            f"allowedPaths entry {trimmed!r} for {HOST} is not a workspace a repository can be in"
        ) from error
    if len(segments) != 1:
        raise ValueError(
            f"allowedPaths entry {trimmed!r} for {HOST} is not one workspace; "
            "a Bitbucket Cloud token is narrowed by workspace"
        )
    if segments[0].casefold() in HOSTS:
        raise ValueError(
            f"allowedPaths entry {path!r} names the host {HOST}, not a workspace on it"
        )
    return segments[0].casefold()


class BitbucketCloudForge(Forge):
    name = "bitbucket"
    proposal_noun = "pull request"
    # No issue verbs (Atlassian removed the issue tracker on 2026-08-20), no
    # `label-ensure` (a pull request has no labels), and no
    # `proposal-acknowledge` (no reaction on a comment).
    verbs = (
        "proposal-create",
        "proposal-list",
        "proposal-view",
        "proposal-comment",
        "proposal-update",
        "proposal-close",
        "proposal-commits",
    )
    transport = "http"
    api_url = API_URL
    error_overrides = ERROR_OVERRIDES
    git_refused = GIT_REFUSED
    acknowledges = False
    labels = False
    # A Bitbucket token always acts as an account: a person's, a dedicated
    # account's, or the bot user of an access token. An unknown login is a
    # fault, so the ownership checks refuse rather than skip.
    identity_required = True
    default_hosts = (HOST,)
    unconfigured = (
        "no credential is configured for bitbucket.org: declare a bitbucket forge in "
        "the PlatformAgent's spec.integration.forges, with a credentialsRef naming a "
        "Secret that holds the access token under the key `token`",
    )

    #: How many pages of repositories `reach` reads.
    REACH_PAGES = 10

    #: How many of a pull request's commits `proposal_commits` reads -- the
    #: ceiling GitHub serves, so the verb answers the same depth everywhere.
    COMMIT_CAP = 250

    def __init__(
        self,
        token_path: str,
        allowed_paths: Iterable[str] = (),
        read_token_path: str = "",
        identity: str = "",
    ) -> None:
        super().__init__()
        self.hosts = HOSTS
        self.credential = self._stored(token_path)
        self.read_token_path = read_token_path
        if read_token_path:
            # Built once here, so a path git may not be handed stops the build
            # as a bad tokenPath does, and not the first context clone.
            self._stored(read_token_path)
        self.allowed_paths = tuple(_allowed_prefix(path) for path in allowed_paths)
        self.identity = _account_uuid(identity)

    @staticmethod
    def _stored(token_path: str) -> StaticFileCredential:
        """A token file, as git and the API are handed it."""
        return StaticFileCredential(
            token_path, HOST, header=TOKEN_HEADER, header_format=TOKEN_FORMAT, username=GIT_USERNAME
        )

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """The one Bitbucket Cloud instance, when configured; none otherwise."""
        built = []
        for entry in config.get("forges") or ():
            if entry.get("provider") != cls.name:
                continue
            host = str(entry.get("host") or "")
            if host not in HOSTS:
                raise ValueError(
                    f"the {cls.name} forge is Bitbucket Cloud and serves {HOST} only, not {host}; "
                    "a Bitbucket Data Center host is a bitbucket-datacenter forge"
                )
            if entry.get("ca_file"):
                raise ValueError(
                    f"the {cls.name} forge at {HOST} names a caFile; {HOST} uses the public CAs"
                )
            token_path = str(entry.get("token_path") or "")
            if not token_path:
                raise ValueError(f"the {cls.name} forge at {HOST} names no tokenPath")
            allowed = entry.get("allowed_paths")
            if allowed is None:
                # A workspace token or an account's API token reaches every
                # repository it can see, and these workspaces are what
                # narrows it before it is spent. Every workspace is allowed
                # only when asked for.
                raise ValueError(
                    f"the {cls.name} forge at {HOST} names no allowedPaths: list the "
                    "workspaces it may reach, or [] for every workspace"
                )
            built.append(
                cls(
                    token_path,
                    allowed,
                    str(entry.get("read_token_path") or ""),
                    str(entry.get("identity") or ""),
                )
            )
        return tuple(built)

    def read_credential(self, repo: str) -> Credential:
        """The read-only token stored beside the write one, for one clone.

        The optional key `read-token` in the forge's credentials Secret, as on
        GitLab: checked on each call, so a key added later is read with no
        restart, and a missing, empty or malformed file is no credential.
        Never the write token.
        """
        if not self.read_token_path:
            return NoCredential()
        credential = self._stored(self.read_token_path)
        if not credential.holds_token():
            return NoCredential()
        return credential

    # -- identity -----------------------------------------------------------

    def whoami(self, api: Callable) -> str | None:
        """The `uuid` of the account this token acts as.

        A configured identity answers first, with no call. Without one, the
        current-user route answers an API token that has the account scope.
        An access token has no account, and the route refuses it (verify
        live): that refusal is "" here, so the broker refuses every ownership
        check by name (FORGE_IDENTITY_UNKNOWN) and the administrator
        configures the identity. Any other failure is raised as it is.
        """
        if self.identity:
            return self.identity
        try:
            answer = api("GET", "user")
        except WorkspaceError as exc:
            if exc.status in (401, 403):
                return ""
            raise
        return translate.actor(answer if isinstance(answer, dict) else {})

    def reach(self, api: Callable) -> tuple[list[str], bool]:
        """Every repository the token's account is a member of.

        An access token reaches a fixed workspace, project or repository, and
        an account's API token reaches everything the account can see, which
        is the case an install wants to see this list for.
        """
        nodes, more = self._pages(
            api, "repositories", {"role": "member", "pagelen": PAGE}, self.REACH_PAGES * PAGE
        )
        paths = [str(node.get("full_name") or "") for node in nodes]
        return [path for path in paths if path], more

    def parse(self, url: str) -> str:
        """`workspace/repository`, and nothing deeper.

        A project is not in a Bitbucket Cloud path. Refused when the workspace
        is outside `allowed_paths`.
        """
        try:
            parts = repo_segments(url, self.hosts)
        except repo_ref.RepoRefError as error:
            raise WorkspaceError(
                f"{url!r} is not a repository on {HOST}; expected workspace/repository"
            ) from error
        if len(parts) != 2:
            raise WorkspaceError(
                f"{url!r} is not a repository on {HOST}; expected workspace/repository"
            )
        if self.allowed_paths and parts[0].casefold() not in self.allowed_paths:
            raise WorkspaceError(
                f"{'/'.join(parts)} is outside the workspaces this install's {self.name} "
                f"forge is allowed to act on",
                status=403,
                code="REPOSITORY_NOT_ALLOWED",
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"https://{HOST}/{repo}.git"

    def file_url(self, repo: str) -> str:
        return f"https://{HOST}/{repo}/src/{{ref}}/{{path}}"

    @staticmethod
    def _repo(repo: str) -> str:
        return f"repositories/{repo}"

    def can_write(
        self, api: Callable, repo: str, login: str, bot: bool = False
    ) -> bool | None:
        """Unknown, except for an automation, which is never a writer.

        Bitbucket Cloud reads another account's repository permission only
        with repository admin on the token, which is a wider grant than this
        install asks for. So a person's comment is from an author whose write
        access is not known, and the conversation treats it as such.
        """
        if not login or bot:
            return False
        return None

    # -- shared by several verbs ------------------------------------------

    def _pages(
        self, api: Callable, path: str, params: dict[str, Any], cap: int
    ) -> tuple[list[dict], bool]:
        """Rows from `path` and the `next` links after it, up to `cap` rows.

        The `next` link is absolute and opaque: Bitbucket asks clients not to
        compose their own. It is followed as the path after the API root, so
        the transport still decides the host, and a link to anywhere else is
        refused, not followed. True when rows were left unread.
        """
        rows: list[dict] = []
        answer = api("GET", path, params=params) or {}
        while True:
            rows += [row for row in (answer.get("values") or []) if isinstance(row, dict)]
            following = str(answer.get("next") or "")
            if not following:
                return rows[:cap], len(rows) > cap
            if len(rows) >= cap:
                return rows[:cap], True
            answer = api("GET", self._relative(following)) or {}

    @staticmethod
    def _relative(link: str) -> str:
        prefix = API_URL + "/"
        if not link.startswith(prefix):
            raise WorkspaceError(
                "Bitbucket Cloud sent a next-page link outside its API, which this broker does not follow",
                status=502,
                code="FORGE_CALL_FAILED",
            )
        return link[len(prefix):]

    def _full_revision(self, api: Callable, repo: str, item: dict[str, Any]) -> dict[str, Any]:
        """`item` with its abbreviated `sourceRevision` expanded to the full id.

        The revision is compared with what git reads off the remote, which is
        a full id. A commit that cannot be read, and a commit on a fork, keep
        the hash Bitbucket gave.
        """
        short = str(item.get("sourceRevision") or "")
        if not _HASH_RE.fullmatch(short) or _FULL_HASH_RE.fullmatch(short):
            return item
        # Only for a source in this repository. A fork can be in any
        # workspace, outside `allowed_paths`, and the token is not spent
        # there; a fork's revision also decides nothing, because a proposal
        # from another repository is never this install's.
        if item.get("sourceRepo") != repo:
            return item
        try:
            node = api("GET", f"{self._repo(repo)}/commit/{short}") or {}
        except WorkspaceError:
            return item
        full = str(node.get("hash") or "")
        if _FULL_HASH_RE.fullmatch(full) and full.startswith(short):
            item["sourceRevision"] = full
        return item

    def _proposal(self, api: Callable, repo: str, node: dict[str, Any]) -> dict[str, Any]:
        return self._full_revision(api, repo, translate.proposal(node, repo))

    def _text_fields(self, payload: dict) -> dict[str, str]:
        body: dict[str, str] = {}
        if payload.get("title") is not None:
            body["title"] = validate_text(payload.get("title"), "title").strip()
        if payload.get("body") is not None:
            body["description"] = validate_text(payload.get("body"), "body", required=False)
        return body

    # -- proposals ----------------------------------------------------------

    def proposal_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        self.refuse_labels("proposal-create", payload)
        body: dict[str, Any] = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "description": validate_text(payload.get("body"), "body", required=False),
            "source": {"branch": {"name": validate_branch(payload.get("source"), "source")}},
            "destination": {"branch": {"name": validate_branch(payload.get("target"), "target")}},
        }
        if payload.get("draft"):
            # A real field on Cloud, read on create.
            body["draft"] = True
        node = api("POST", f"{self._repo(repo)}/pullrequests", body=body)
        return {"proposal": self._proposal(api, repo, node)}

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        self.refuse_labels("proposal-list", payload)
        limit = validate_limit(payload.get("limit"))
        neutral = validate_state(payload.get("state"))
        page = validate_page(payload.get("page"))
        clauses = []
        source = payload.get("source")
        if source is not None:
            clauses.append(f'source.branch.name="{_bbql(validate_branch(source, "source"))}"')
        target = payload.get("target")
        if target is not None:
            clauses.append(f'destination.branch.name="{_bbql(validate_branch(target, "target"))}"')
        params: dict[str, Any] = {
            "state": list(translate.NEUTRAL_STATES[neutral]),
            "sort": "-created_on",
            "pagelen": min(limit, PROPOSAL_PAGE),
        }
        if clauses:
            params["q"] = " AND ".join(clauses)
        path = f"{self._repo(repo)}/pullrequests"
        if source is None:
            items, more = self._window(api, repo, path, params, page, limit)
            return listing(items, limit, "proposals", returned=limit if more else 0)
        # A branch of the same name on a fork is not this repository's branch,
        # and must not answer "is there an open pull request for the branch I
        # just published". Forks are told apart only after a page arrives, so
        # the rows are read in full pages from the first, filtered, and paged
        # here, as GitLab's source-filtered listing is.
        params["pagelen"] = PROPOSAL_PAGE
        want = page * limit
        nodes, more = self._pages(api, path, params, MAX_CONVERSATION_SIZE)
        own = [
            item
            for item in (translate.proposal(node, repo) for node in nodes)
            if item["sourceRepo"] == repo
        ]
        chunk = [self._full_revision(api, repo, item) for item in own[(page - 1) * limit : want]]
        more = more or len(own) > want
        return listing(chunk, limit, "proposals", returned=limit if more else 0)

    def _window(
        self, api: Callable, repo: str, path: str, params: dict[str, Any], page: int, limit: int
    ) -> tuple[list[dict], bool]:
        """Page `page` of `limit` proposals, read from Bitbucket's own pages.

        Bitbucket's page holds at most `PROPOSAL_PAGE` rows, which can be
        fewer than `limit`, so a window starts on the page that holds its
        first row and follows `next` until it is full. True when more rows
        follow the window.
        """
        size = params["pagelen"]
        start = (page - 1) * limit
        first = start // size + 1
        offset = start % size
        request = dict(params)
        if first > 1:
            request["page"] = first
        rows, more = self._pages(api, path, request, offset + limit)
        return [translate.proposal(node, repo) for node in rows[offset:]], more

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        base = f"{self._repo(repo)}/pullrequests/{number}"
        node = api("GET", base) or {}
        proposal = self._proposal(api, repo, node)
        result: dict[str, Any] = {"proposal": proposal}
        if payload.get("comments"):
            limit = validate_comment_limit(payload.get("limit"))
            comments, truncated = self._comments(api, f"{base}/comments", limit)
            result["comments"] = comments
            result["commentCount"] = len(comments)
            result["commentsTruncated"] = truncated
        if payload.get("diff"):
            result["diff"] = self._diff(api, repo, node, proposal)
        return result

    def _comments(self, api: Callable, path: str, limit: int) -> tuple[list, bool]:
        """Up to `limit` comments, oldest first, and whether there were more.

        `limit` counts comments, not rows: a deleted comment is a row with no
        text, so the reading goes on past it until `limit` comments, the last
        page, or `MAX_CONVERSATION_SIZE` rows. Truncated when rows were left
        unread or more than `limit` comments survived.
        """
        kept: list = []
        answer = api("GET", path, params={"pagelen": PAGE, "sort": "created_on"}) or {}
        rows = 0
        while True:
            batch = [row for row in (answer.get("values") or []) if isinstance(row, dict)]
            rows += len(batch)
            kept += [translate.comment(row) for row in batch if not translate.is_deleted(row)]
            following = str(answer.get("next") or "")
            if len(kept) > limit:
                return kept[:limit], True
            if not following:
                return kept, False
            if len(kept) == limit or rows >= MAX_CONVERSATION_SIZE:
                return kept, True
            answer = api("GET", self._relative(following)) or {}

    def _diff(
        self, api: Callable, repo: str, node: dict[str, Any], proposal: dict[str, Any]
    ) -> str:
        """The pull request's diff, from the repository diff route.

        The pull-request diff route answers 302 to this route, and the
        transport follows no redirect, because the credential would go with
        it. So the package asks for what the redirect names: the source commit
        and the destination commit, in that order (the opposite of `git
        diff`), with `topic=true` for the diff from their merge base. The
        source commit is the expanded one, and a source on a fork is named
        with its repository (`workspace/slug:commit`), because the fork's
        commit is not in this repository (verify live).
        """
        source = str(proposal.get("sourceRevision") or "")
        destination = translate.destination_revision(node)
        if not (_HASH_RE.fullmatch(source) and _HASH_RE.fullmatch(destination)):
            return "# Bitbucket did not name both commits of this pull request, so no diff is shown\n"
        holder = str(proposal.get("sourceRepo") or "")
        if holder and holder != repo:
            source = f"{holder}:{source}"
        return api(
            "GET",
            f"{self._repo(repo)}/diff/{source}..{destination}",
            params={"topic": "true"},
            raw="text/plain",
        )

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"{self._repo(repo)}/pullrequests/{number}/comments",
            body={"content": {"raw": validate_text(payload.get("body"), "body")}},
        )
        return {"comment": translate.comment(node or {})}

    def proposal_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        self.refuse_labels("proposal-update", payload)
        number = validate_number(payload.get("number"))
        body: dict[str, Any] = self._text_fields(payload)
        path = f"{self._repo(repo)}/pullrequests/{number}"
        # The answer is the pull request as it stands; with nothing to change,
        # read it rather than send an empty update.
        current = api("GET", path) or {}
        if not body:
            return {"proposal": self._proposal(api, repo, current)}
        # An update that leaves `reviewers` out can remove them (verify live),
        # so the reviewers that were read go back as they were.
        body["reviewers"] = [
            {"uuid": translate.actor(reviewer)}
            for reviewer in current.get("reviewers") or []
            if isinstance(reviewer, dict) and translate.actor(reviewer)
        ]
        node = api("PUT", path, body=body)
        return {"proposal": self._proposal(api, repo, node or {})}

    def proposal_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("POST", f"{self._repo(repo)}/pullrequests/{number}/decline", body={})
        return {"proposal": self._proposal(api, repo, node or {})}

    def proposal_commits(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        limit = validate_limit(payload.get("limit"))
        page = validate_page(payload.get("page"))
        # Bitbucket lists a pull request's commits newest first (verify live),
        # with cursor pages; the verb promises oldest first, with page 1
        # holding the oldest. So the list is read whole, up to the ceiling,
        # and paged here, as GitLab's is.
        nodes, _more = self._pages(
            api,
            f"{self._repo(repo)}/pullrequests/{number}/commits",
            {"pagelen": PAGE},
            self.COMMIT_CAP,
        )
        commits = [translate.commit(node) for node in reversed(nodes)]
        return listing(commits[(page - 1) * limit : page * limit], limit, "commits")


#: An account UUID, with or without the braces Bitbucket Cloud writes it in.
_UUID_RE = re.compile(r"\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}?")


def _account_uuid(identity: str) -> str:
    """A configured identity as Bitbucket Cloud writes an author: `{uuid}`.

    Every author and reviewer is compared by its `uuid`, which Bitbucket
    writes with braces. A UUID copied without them would match no author,
    and the install would read its own proposals as another account's, so it
    is put in braces here. A value that is not a UUID stops the build, as a
    bad tokenPath does, because no author can ever match it.
    """
    trimmed = identity.strip()
    if not trimmed:
        return ""
    match = _UUID_RE.fullmatch(trimmed)
    if not match or (trimmed.startswith("{") != trimmed.endswith("}")):
        raise ValueError(
            f"the bitbucket forge's identity {trimmed!r} is not an account UUID; write the "
            "UUID of the account that the token acts as, such as {0f3b2c1d-aaaa-bbbb-cccc-0123456789ab}"
        )
    return "{" + match.group(1).lower() + "}"


def _bbql(value: str) -> str:
    """A branch name as a BBQL string value.

    `validate_branch` already refuses every character BBQL would read as
    syntax; this is the check that keeps it so if that rule ever widens.
    """
    if not _BBQL_SAFE.fullmatch(value):
        raise WorkspaceError("a branch name holds a character a Bitbucket query cannot carry")
    return value
