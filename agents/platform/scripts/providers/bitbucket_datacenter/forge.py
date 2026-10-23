#!/usr/bin/env python3
"""Bitbucket Data Center: which calls to make, and nothing about how they are made.

One class serves every Data Center instance an install declares: the registry
builds one instance per configured host. The transport is the broker's
in-process HTTP client, rooted at the host itself, so that the REST API
(`rest/api/latest/...`) and the `whoami` servlet are both relative paths on
the one host the credential belongs to. The credential is an HTTP access
token that an administrator stored in a Secret: a project token (recommended),
a repository token for an install with one repository, or a service account's
personal token for repositories in more than one project. All of them are sent
as `Authorization: Bearer` to the API, and to git with the username
`x-token-auth`. `allowed_paths` narrows the token to project keys before it is
spent.

The instance is served at its root path on port 443 only. A context path or a
port needs a new field on the forge entry and a change to the shared loader,
which is not in this release: the operator and the loader refuse both by name.

What Data Center does differently from the shapes every caller expects, and
where it is absorbed:

- no issue tracker and no labels: the issue verbs and `label-ensure` are not
  in `verbs`, `labels` is False;
- reactions exist through a bundled plugin that an administrator can turn
  off, and the names it takes are not published: `acknowledges` is False
  until a recorded response proves the call;
- a repository is `KEY/slug`, and the clone URL has `/scm/` in front;
- listings page by `start` and `limit` and say `isLastPage`;
- an update and a decline carry the pull request's current `version`; a stale
  one answers 409, and the package reads again and retries once;
- an update that leaves `reviewers` out removes every reviewer, so it sends
  the reviewers it read;
- general comments come from the activity stream, newest first, so the
  conversation is read whole, up to its bound, and put oldest first here;
- the account a token acts as is the instance's `whoami` servlet, outside the
  REST API; a configured identity answers first.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

import repo_ref

from ..base import Forge, WorkspaceError, listing
from ..credentials import Credential, NoCredential, StaticFileCredential
from ..validate import (
    MAX_CONVERSATION_SIZE,
    MAX_PAGE_SIZE,
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

#: The REST API, relative to the host.
REST = "rest/api/latest"

#: The servlet that answers the username of the authenticated caller as plain
#: text. Outside the REST API, so the transport is rooted at the host.
WHOAMI = "plugins/servlet/applinks/whoami"

GIT_USERNAME = "x-token-auth"
TOKEN_HEADER = "Authorization"
TOKEN_FORMAT = "Bearer {token}"

#: Bitbucket Cloud's hosts. A Data Center forge declared at one would be
#: handed Cloud's traffic.
CLOUD_DOMAINS = ("bitbucket.org", "bitbucket.io")

#: A project key: a letter, then letters, digits and underscores.
_PROJECT_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

#: A username as the `whoami` servlet answers one: one line, no markup.
_USERNAME_RE = re.compile(r"[^\s<>]{1,255}")

#: The URL paths a Data Center web or clone URL starts with, which `parse`
#: reads down to `KEY/slug`.
_SCM = "scm"
_PROJECTS = "projects"
_USERS = "users"
_REPOS = "repos"


def _allowed_prefix(host: str, path: str) -> str:
    """One `allowedPaths` entry: a project key, as `parse` would read it."""
    if any(character.isspace() for character in path):
        raise ValueError(f"allowedPaths entry {path!r} for {host} contains whitespace")
    trimmed = path.strip("/")
    if not trimmed:
        raise ValueError(
            f"an allowedPaths entry for {host} names no project ({path!r}); "
            "write the project key, or [] on its own for every project the token reaches"
        )
    if not _PROJECT_KEY_RE.fullmatch(trimmed):
        raise ValueError(
            f"allowedPaths entry {trimmed!r} for {host} is not a project key "
            "(a letter, then letters, digits and underscores)"
        )
    return trimmed.casefold()


class BitbucketDataCenterForge(Forge):
    name = "bitbucket-datacenter"
    proposal_noun = "pull request"
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
    error_overrides = ERROR_OVERRIDES
    git_refused = GIT_REFUSED
    acknowledges = False
    labels = False
    identity_required = True

    #: How many pages of repositories `reach` reads.
    REACH_PAGES = 10

    #: How many of a pull request's commits `proposal_commits` reads.
    COMMIT_CAP = 250

    #: How many rows of the user search `can_write` reads before it says it
    #: does not know.
    WRITERS_CAP = 1000

    def __init__(
        self,
        host: str,
        token_path: str,
        allowed_paths: Iterable[str] = (),
        ca_file: str = "",
        ca_source: str = "",
        read_token_path: str = "",
        identity: str = "",
    ) -> None:
        super().__init__()
        if any(host == domain or host.endswith("." + domain) for domain in CLOUD_DOMAINS):
            raise ValueError(
                f"{host} is Bitbucket Cloud, not a Bitbucket Data Center instance; declare a bitbucket forge"
            )
        self.hosts = (host,)
        self.api_url = f"https://{host}"
        self.ca_file = ca_file
        self.ca_source = ca_source
        self.credential = self._stored(token_path)
        self.read_token_path = read_token_path
        if read_token_path:
            self._stored(read_token_path)
        self.allowed_paths = tuple(_allowed_prefix(host, path) for path in allowed_paths)
        self.identity = identity.strip()

    def _stored(self, token_path: str) -> StaticFileCredential:
        return StaticFileCredential(
            token_path,
            self.hosts[0],
            header=TOKEN_HEADER,
            header_format=TOKEN_FORMAT,
            username=GIT_USERNAME,
        )

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """One instance per configured Data Center host; none when none is."""
        built = []
        for entry in config.get("forges") or ():
            if entry.get("provider") != cls.name:
                continue
            host = str(entry.get("host") or "")
            token_path = str(entry.get("token_path") or "")
            if not token_path:
                raise ValueError(f"the {cls.name} forge at {host} names no tokenPath")
            allowed = entry.get("allowed_paths")
            if allowed is None:
                raise ValueError(
                    f"the {cls.name} forge at {host} names no allowedPaths: list the "
                    "project keys it may reach, or [] for every project"
                )
            built.append(
                cls(
                    host,
                    token_path,
                    allowed,
                    str(entry.get("ca_file") or ""),
                    str(entry.get("ca_source") or ""),
                    str(entry.get("read_token_path") or ""),
                    str(entry.get("identity") or ""),
                )
            )
        return tuple(built)

    def read_credential(self, repo: str) -> Credential:
        """The read-only token stored beside the write one, for one clone.

        The optional key `read-token`, as on GitLab and Bitbucket Cloud: a
        project token with read permission only. A missing, empty or
        malformed file is no credential. Never the write token.
        """
        if not self.read_token_path:
            return NoCredential()
        credential = self._stored(self.read_token_path)
        if not credential.holds_token():
            return NoCredential()
        return credential

    # -- identity -----------------------------------------------------------

    def whoami(self, api: Callable) -> str | None:
        """The username this token acts as.

        A configured identity answers first, with no call. Without one, the
        instance's `whoami` servlet answers the authenticated caller's
        username as plain text (verify live for project and repository
        tokens). A refusal, or an answer that is not one username, is "", so
        the broker refuses every ownership check by name.
        """
        if self.identity:
            return self.identity
        try:
            text = api("GET", WHOAMI, raw="text/plain")
        except WorkspaceError as exc:
            if exc.status in (401, 403, 404):
                return ""
            raise
        login = str(text or "").strip()
        return login if _USERNAME_RE.fullmatch(login) else ""

    def reach(self, api: Callable) -> tuple[list[str], bool]:
        """Every repository the token may write to.

        A project token reaches its project, and a service account's personal
        token reaches every project the account is in, which is the case an
        install wants to see this list for.
        """
        nodes, more = self._pages(
            api, f"{REST}/repos", {"permission": "REPO_WRITE"}, self.REACH_PAGES * MAX_PAGE_SIZE
        )
        paths = []
        for node in nodes:
            key = str((node.get("project") or {}).get("key") or "")
            slug = str(node.get("slug") or "")
            if key and slug:
                paths.append(f"{key}/{slug}")
        return paths, more

    def parse(self, url: str) -> str:
        """`KEY/slug`. A clone URL and a web URL of the repository are read too.

        `https://host/scm/KEY/slug.git` is what `clone_url` gives and what git
        reports as the remote, and `https://host/projects/KEY/repos/slug` is
        the page a person copies; both name `KEY/slug`. A personal repository
        (`~user/slug`) is not served in this release. Refused when the project
        is outside `allowed_paths`.
        """
        if isinstance(url, str) and ("/~" in url or url.strip().startswith("~")):
            raise WorkspaceError(
                f"{url!r} is a personal repository; this release serves Bitbucket Data "
                "Center project repositories only (KEY/repository)"
            )
        try:
            parts = repo_segments(url, self.hosts)
        except repo_ref.RepoRefError as error:
            raise WorkspaceError(
                f"{url!r} is not a repository on {self.hosts[0]}; expected KEY/repository"
            ) from error
        lowered = [part.lower() for part in parts]
        if lowered and lowered[0] == _USERS:
            # The web page of a personal repository: /users/<user>/repos/<slug>.
            raise WorkspaceError(
                f"{url!r} is a personal repository; this release serves Bitbucket Data "
                "Center project repositories only (KEY/repository)"
            )
        if len(parts) == 3 and lowered[0] == _SCM:
            parts = parts[1:]
        elif len(parts) >= 4 and lowered[0] == _PROJECTS and lowered[2] == _REPOS:
            parts = [parts[1], parts[3]]
        elif lowered and lowered[0] in (_SCM, _PROJECTS):
            # Part of the instance's URL layout, not a project: `scm/ACME`
            # is a clone URL with no repository, not project SCM.
            raise WorkspaceError(
                f"{url!r} is not a repository on {self.hosts[0]}; expected KEY/repository, "
                "or the repository's clone or web URL in full"
            )
        if len(parts) != 2 or not _PROJECT_KEY_RE.fullmatch(parts[0]):
            raise WorkspaceError(
                f"{url!r} is not a repository on {self.hosts[0]}; expected KEY/repository, "
                "where KEY is the project key"
            )
        if self.allowed_paths and parts[0].casefold() not in self.allowed_paths:
            raise WorkspaceError(
                f"{'/'.join(parts)} is outside the projects this install's {self.name} "
                f"forge at {self.hosts[0]} is allowed to act on",
                status=403,
                code="REPOSITORY_NOT_ALLOWED",
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"https://{self.hosts[0]}/{_SCM}/{repo}.git"

    def file_url(self, repo: str) -> str:
        key, slug = repo.split("/", 1)
        return f"https://{self.hosts[0]}/projects/{key}/repos/{slug}/browse/{{path}}?at={{ref}}"

    @staticmethod
    def _repo(repo: str) -> str:
        key, slug = repo.split("/", 1)
        return f"{REST}/projects/{key}/repos/{slug}"

    def _commit_url(self, repo: str, sha: str) -> str:
        key, slug = repo.split("/", 1)
        return f"https://{self.hosts[0]}/projects/{key}/repos/{slug}/commits/{sha}" if sha else ""

    def can_write(
        self, api: Callable, repo: str, login: str, bot: bool = False
    ) -> bool | None:
        """Whether `login` may write to `repo`: the user search, filtered on REPO_WRITE.

        The search's `filter` matches a part of the username, the display name
        or the e-mail address, so the user is found by an exact `name` in the
        pages it returns, read up to `WRITERS_CAP` rows. A login that the whole
        search does not return is a definitive no; one not found in the rows
        read, with more rows left, is not known (None), because a "no" here is
        a refusal that the conversation keeps. A service account is a no
        whatever its permission; a search that failed is not an answer (verify
        live that a project token may call it).
        """
        if not login or bot:
            return False
        key, slug = repo.split("/", 1)
        try:
            rows, more = self._pages(
                api,
                f"{REST}/users",
                # The numbered filter form, as Atlassian documents it: an
                # unnumbered `permission` does not take a repository.
                {
                    "filter": login,
                    "permission.1": "REPO_WRITE",
                    "permission.1.projectKey": key,
                    "permission.1.repositorySlug": slug,
                },
                self.WRITERS_CAP,
            )
        except WorkspaceError:
            return None
        for node in rows:
            if str(node.get("name") or "").casefold() == login.casefold():
                return not translate.is_automation(node)
        return None if more else False

    # -- shared by several verbs ------------------------------------------

    @staticmethod
    def _pages(
        api: Callable, path: str, params: dict[str, Any], cap: int, start: int = 0
    ) -> tuple[list[dict], bool]:
        """Rows from `path`, page by page from `start`, up to `cap` rows.

        True when rows were left unread. A page that says it is not the last
        and gives no next start is read as the last, so a broken answer cannot
        loop.
        """
        rows: list[dict] = []
        while True:
            request = dict(params, start=start, limit=min(MAX_PAGE_SIZE, max(cap - len(rows), 1)))
            answer = api("GET", path, params=request) or {}
            rows += [row for row in (answer.get("values") or []) if isinstance(row, dict)]
            following = answer.get("nextPageStart")
            last = bool(answer.get("isLastPage", True)) or not isinstance(following, int) or following <= start
            if last:
                return rows[:cap], len(rows) > cap
            if len(rows) >= cap:
                return rows[:cap], True
            start = following

    @staticmethod
    def _ref(branch: str, repo: str) -> dict[str, Any]:
        key, slug = repo.split("/", 1)
        return {"id": f"refs/heads/{branch}", "repository": {"slug": slug, "project": {"key": key}}}

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
            "fromRef": self._ref(validate_branch(payload.get("source"), "source"), repo),
            "toRef": self._ref(validate_branch(payload.get("target"), "target"), repo),
        }
        if payload.get("draft"):
            # A field from 8.18 on. Sent only when asked for, so an older
            # instance is not handed a field it does not know for every create.
            body["draft"] = True
        node = api("POST", f"{self._repo(repo)}/pull-requests", body=body)
        return {"proposal": translate.proposal(node or {}, repo)}

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        self.refuse_labels("proposal-list", payload)
        limit = validate_limit(payload.get("limit"))
        neutral = validate_state(payload.get("state"))
        page = validate_page(payload.get("page"))
        source = payload.get("source")
        source = validate_branch(source, "source") if source is not None else None
        target = payload.get("target")
        target = validate_branch(target, "target") if target is not None else None
        params: dict[str, Any] = {
            "state": "OPEN" if neutral == "open" else "ALL",
            "order": "NEWEST",
        }
        # One branch can be asked of the instance: the source, as the branch
        # pull requests go out of, or else the target, as the one they come
        # into. Everything else is filtered here.
        if source is not None:
            params.update({"at": f"refs/heads/{source}", "direction": "OUTGOING"})
        elif target is not None:
            params.update({"at": f"refs/heads/{target}", "direction": "INCOMING"})
        path = f"{self._repo(repo)}/pull-requests"
        filtered = neutral == "closed" or source is not None
        if not filtered:
            nodes, more = self._pages(api, path, params, limit, start=(page - 1) * limit)
            items = [translate.proposal(node, repo) for node in nodes]
            return listing(items, limit, "proposals", returned=limit if more else 0)

        def keep(node: dict[str, Any], item: dict[str, Any]) -> bool:
            # OUTGOING from this repository includes a pull request onto
            # another repository; it is not this repository's.
            if source is not None and not translate.targets(node, repo):
                return False
            if target is not None and item["target"] != target:
                return False
            if neutral == "closed" and item["state"] == "open":
                return False
            return item["sourceRepo"] == repo or source is None

        want = page * limit
        nodes, more = self._pages(api, path, params, MAX_CONVERSATION_SIZE)
        own = [
            item
            for node in nodes
            for item in [translate.proposal(node, repo)]
            if keep(node, item)
        ]
        chunk = own[(page - 1) * limit : want]
        more = more or len(own) > want
        return listing(chunk, limit, "proposals", returned=limit if more else 0)

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        base = f"{self._repo(repo)}/pull-requests/{number}"
        node = api("GET", base) or {}
        result: dict[str, Any] = {"proposal": translate.proposal(node, repo)}
        if payload.get("comments"):
            limit = validate_comment_limit(payload.get("limit"))
            comments, truncated = self._comments(api, f"{base}/activities", limit)
            result["comments"] = comments
            result["commentCount"] = len(comments)
            result["commentsTruncated"] = truncated
        if payload.get("diff"):
            result["diff"] = api("GET", f"{base}.diff", raw="text/plain")
        return result

    def _comments(self, api: Callable, path: str, limit: int) -> tuple[list, bool]:
        """The first `limit` comments, oldest first, and whether there were more.

        The activity stream is newest first, so the conversation is read whole,
        up to `MAX_CONVERSATION_SIZE` rows, and ordered here. Truncated when
        rows were left unread or more than `limit` comments were found.
        """
        rows, more = self._pages(api, path, {}, MAX_CONVERSATION_SIZE)
        found = [item for row in rows for item in translate.activity_comments(row)]
        found.sort(key=lambda item: (item["created"], str(item["id"])))
        return found[:limit], more or len(found) > limit

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"{self._repo(repo)}/pull-requests/{number}/comments",
            body={"text": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node or {})}

    def _versioned(
        self, api: Callable, path: str, write: Callable[[dict[str, Any]], Any]
    ) -> Any:
        """Read the pull request, then `write` with what was read; once more on 409.

        A stale `version` is a 409, and so is a change somebody made between
        the read and the write. One retry on a fresh read covers the first;
        a second 409 is reported, because the second is a real conflict.
        """
        for attempt in (1, 2):
            current = api("GET", path) or {}
            try:
                return write(current)
            except WorkspaceError as exc:
                if exc.status != 409 or attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def proposal_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        self.refuse_labels("proposal-update", payload)
        number = validate_number(payload.get("number"))
        fields = self._text_fields(payload)
        path = f"{self._repo(repo)}/pull-requests/{number}"
        if not fields:
            return {"proposal": translate.proposal(api("GET", path) or {}, repo)}

        def write(current: dict[str, Any]) -> Any:
            body: dict[str, Any] = {"version": current.get("version"), **fields}
            # An update without `reviewers` removes them all, so the ones that
            # were read go back as they were.
            body["reviewers"] = [
                {"user": {"name": translate.actor(reviewer)}}
                for reviewer in current.get("reviewers") or []
                if translate.actor(reviewer)
            ]
            return api("PUT", path, body=body)

        node = self._versioned(api, path, write)
        return {"proposal": translate.proposal(node or {}, repo)}

    def proposal_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        path = f"{self._repo(repo)}/pull-requests/{number}"

        def write(current: dict[str, Any]) -> Any:
            return api("POST", f"{path}/decline", params={"version": current.get("version")}, body={})

        node = self._versioned(api, path, write)
        return {"proposal": translate.proposal(node or {}, repo)}

    def proposal_commits(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        limit = validate_limit(payload.get("limit"))
        page = validate_page(payload.get("page"))
        # Newest first, with no parameter to turn it round; the verb promises
        # oldest first. Read whole up to the ceiling and paged here.
        nodes, _more = self._pages(
            api, f"{self._repo(repo)}/pull-requests/{number}/commits", {}, self.COMMIT_CAP
        )
        commits = [
            translate.commit(node, self._commit_url(repo, str(node.get("id") or "")))
            for node in reversed(nodes)
        ]
        return listing(commits[(page - 1) * limit : page * limit], limit, "commits")
