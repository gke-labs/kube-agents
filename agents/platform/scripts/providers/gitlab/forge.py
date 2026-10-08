#!/usr/bin/env python3
"""GitLab: which calls to make, and nothing about how they are made.

One class serves gitlab.com and every self-managed instance: the registry
builds one instance per configured host, and the host is the only thing that
differs between them. The transport is the broker's in-process HTTP client;
the credential is a token an administrator stored in a Secret -- a group or
project access token, or, on gitlab.com's Free tier where those do not exist,
a personal access token of an account that exists for this install. The
mechanism is the same for all three; what differs is how far the token
reaches, which is why `allowed_paths` is enforced here before a credential is
spent and the broker's managed list is enforced before that.

What GitLab's API does differently from the shapes every caller expects, and
where it is absorbed:

- a project is addressed as one URL-encoded segment, `quote(path, safe="")`.
  The default `quote` leaves `/` alone, which GitLab reads as a different
  route, and the 404 that answers it reads like a permissions problem;
- every number is an `iid`, never an `id` (see `translate.py`);
- states are `opened`/`closed`/`merged` on the way in as well as out;
- a draft is a title prefix, because the `draft` field is ignored when a merge
  request is created;
- notes carry GitLab's own bookkeeping, which is not a conversation;
- `raw_diffs` answers 5xx until GitLab has computed the diff, so a diff falls
  back to the JSON `diffs` endpoint and is assembled from it.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

import repo_ref

from ..base import COLLABORATION_VERBS, Forge, WorkspaceError, listing
from ..credentials import StaticFileCredential
from ..validate import (
    MAX_CONVERSATION_SIZE,
    MAX_PAGE_SIZE,
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

#: GitLab's access level for Developer, the lowest that may push and open a
#: merge request. Maintainer (40) and Owner (50) are above it.
DEVELOPER_ACCESS = 30

#: The username git is given beside the token. GitLab reads the token and
#: accepts any non-empty username for an access token over HTTPS.
GIT_USERNAME = "oauth2"

#: The colour a label is created with when the caller names none. GitLab
#: requires one; GitHub picks one itself.
DEFAULT_LABEL_COLOR = "#6699cc"

#: The emoji `proposal-acknowledge` awards: the same "seen" GitHub's reaction
#: says.
ACKNOWLEDGE_EMOJI = "eyes"

#: The guidance code for a forge's own 5xx, as opposed to a refusal the
#: transport made for it.
FORGE_UNAVAILABLE = "FORGE_UNAVAILABLE"

#: The label filter values GitLab reads as keywords, case-blind: `None` lists
#: what has no label, `Any` what has one. There is no way to escape them.
RESERVED_LABEL_FILTERS = frozenset({"none", "any"})

#: The validation message GitLab sends, as a 404, for an emoji already awarded.
ALREADY_AWARDED = "has already been taken"


def _states(neutral: str) -> str:
    """The neutral state as GitLab's `state` parameter."""
    return {"open": "opened", "closed": "closed", "all": "all"}[neutral]



def _filter_labels(raw: Any) -> list[str]:
    """`validate_labels`, refusing the names GitLab's filter reads as keywords.

    A label titled `None` or `Any` exists happily on GitLab, but filtering on
    it lists items with no label or with any label -- the wrong set, with no
    error -- so the filter refuses it rather than answering for another one.
    """
    labels = validate_labels(raw)
    reserved = [label for label in labels if label.casefold() in RESERVED_LABEL_FILTERS]
    if reserved:
        raise WorkspaceError(
            f"GitLab reads {reserved[0]!r} in a label filter as a keyword, not a "
            "label name, and offers no way to escape it; filter on another label"
        )
    return labels

class GitLabForge(Forge):
    name = "gitlab"
    proposal_noun = "merge request"
    verbs = COLLABORATION_VERBS
    transport = "http"
    error_overrides = ERROR_OVERRIDES
    acknowledges = True
    whoami_route = ("user", "username")
    # What an install that has not configured GitLab answers for gitlab.com: a
    # named gap rather than "not a forge this install serves".
    default_hosts = ("gitlab.com",)
    unconfigured = (
        "no credential is configured for gitlab.com: declare a gitlab forge with "
        "a credentialsRef on the PlatformAgent",
    )

    def __init__(
        self, host: str, token_path: str, allowed_paths: Iterable[str] = ()
    ) -> None:
        super().__init__()
        # gitlab.com answers on `www.` too, and the operator folds that
        # spelling onto gitlab.com, so a URL written with it resolves here.
        # The canonical host stays first: clone URLs and managed-list keys
        # are built from it.
        self.hosts = (host, "www.gitlab.com") if host == "gitlab.com" else (host,)
        self.api_url = f"https://{host}/api/v4"
        self.credential = StaticFileCredential(
            token_path, host, header="PRIVATE-TOKEN", username=GIT_USERNAME
        )
        prefixes = []
        for path in allowed_paths:
            trimmed = path.strip("/")
            # An entry that names no namespace -- `""`, `"/"`, a template value
            # that rendered empty -- is refused rather than dropped: dropped,
            # it could leave the list empty, and empty is the whole host.
            if not trimmed:
                raise ValueError(
                    f"an allowedPaths entry for {host} names no namespace ({path!r}); "
                    "write the namespace, or [] on its own for the whole host"
                )
            prefixes.append(self._prefix(host, path, trimmed))
        self.allowed_paths = tuple(prefixes)

    @staticmethod
    def _prefix(host: str, path: str, trimmed: str) -> tuple[str, ...]:
        """An `allowedPaths` entry as the segments `parse` would compare it to.

        Read by the parser `parse` uses, so the two cannot disagree: a trailing
        `.git` comes off and a leading host is lifted, exactly as they do for a
        repository, and a segment the parser refuses -- empty from a doubled
        slash, `.`, `..`, `.git`, led by a dash -- refuses the entry. An entry
        `parse` could never match would refuse every repository on the host
        one request at a time. Whitespace anywhere is refused rather than
        stripped: it is a typo, and stripping it would be a second, quieter
        rule beside the parser's.
        """
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
        if not segments:
            raise ValueError(
                f"an allowedPaths entry for {host} names no namespace ({path!r})"
            )
        # Checked on the normalised prefix, not the input: a parsed repository
        # never starts with its own host, so a prefix that does matches
        # nothing. The bare host is the spelling someone meaning "the whole
        # host" writes; `[]` is that.
        if segments[0].casefold() == host.casefold():
            raise ValueError(
                f"allowedPaths entry {path!r} names the host {host}, not a namespace "
                "on it; list namespaces, or [] for the whole host"
            )
        return tuple(segment.casefold() for segment in segments)

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """One instance per configured GitLab host; none when none is configured.

        gitlab.com and a self-managed instance are the same class with
        different hosts. Without a configuration nothing is built, and a
        gitlab.com URL then resolves to the named gap in the registry rather
        than to a forge with no credential.
        """
        built = []
        for entry in config.get("forges") or ():
            if entry.get("provider") != cls.name:
                continue
            token_path = str(entry.get("token_path") or "")
            if not token_path:
                raise ValueError(f"the {cls.name} forge at {entry.get('host')} names no tokenPath")
            allowed = entry.get("allowed_paths")
            if allowed is None:
                # A GitLab token reaches whatever its account or group does,
                # and these prefixes are what narrows it before it is spent.
                # The whole host is allowed only when asked for.
                raise ValueError(
                    f"the {cls.name} forge at {entry.get('host')} names no allowedPaths: "
                    "list the namespaces it may reach, or [] for the whole host"
                )
            built.append(cls(entry["host"], token_path, allowed))
        return tuple(built)

    #: How many pages of the token account's projects `reach` reads: enough
    #: for an account that belongs to a thousand projects, which is already the
    #: finding.
    REACH_PAGES = 10

    #: How many of a merge request's commits `proposal_commits` reads -- the
    #: same ceiling GitHub serves for a pull request, so the verb answers the
    #: same depth on both.
    COMMIT_CAP = 250

    #: How many pages of per-file diffs the `diffs` fallback reads before it
    #: says the diff is cut short.
    DIFF_PAGES = 10

    #: Roughly how much of the assembled fallback diff is kept before it says
    #: it stopped: each page is under the transport's ceiling, but ten of them
    #: together need not be. The broker's default response ceiling.
    DIFF_FALLBACK_CHARS = 4 * 1024 * 1024

    def reach(self, api: Callable) -> tuple[list[str], bool]:
        """Every project the token's account is a member of.

        A group or project access token reaches its group or project; a
        personal access token reaches everything its account belongs to, which
        is why an install on gitlab.com's Free tier wants to see this list.
        """
        paths: list[str] = []
        for page in range(1, self.REACH_PAGES + 1):
            batch = api(
                "GET",
                "projects",
                params={"membership": "true", "simple": "true", "per_page": MAX_PAGE_SIZE, "page": page},
            ) or []
            paths += [str(item.get("path_with_namespace") or "") for item in batch]
            if len(batch) < MAX_PAGE_SIZE:
                return [path for path in paths if path], False
        return [path for path in paths if path], True

    # -- identity -----------------------------------------------------------

    def parse(self, url: str) -> str:
        """`group/subgroup/project`, nested as deep as GitLab nests.

        Refused when it falls outside `allowed_paths`, compared segment by
        segment: `acme/infra-secret` starts with the string `acme/infra` and is
        a different project.
        """
        try:
            parts = repo_segments(url, self.hosts)
        except repo_ref.RepoRefError as error:
            raise WorkspaceError(
                f"{url!r} is not a repository on {self.hosts[0]}; expected group/project"
            ) from error
        if len(parts) < 2:
            raise WorkspaceError(
                f"{url!r} is not a repository on {self.hosts[0]}; expected group/project"
            )
        folded = tuple(part.casefold() for part in parts)
        if self.allowed_paths and not any(
            folded[: len(prefix)] == prefix for prefix in self.allowed_paths
        ):
            raise WorkspaceError(
                f"{'/'.join(parts)} is outside the paths this install's {self.name} "
                f"forge at {self.hosts[0]} is allowed to act on",
                status=403,
                code="REPOSITORY_NOT_ALLOWED",
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"https://{self.hosts[0]}/{repo}.git"

    @staticmethod
    def _project(repo: str) -> str:
        return f"projects/{quote(repo, safe='')}"

    # -- shared by several verbs ------------------------------------------

    @staticmethod
    def _notes(api: Callable, path: str, limit: int) -> tuple[list, bool]:
        """Up to `limit` notes from a notes endpoint, oldest first, as comments,
        and whether it held more.

        `limit` counts comments, as it does on GitHub, where a row of the
        conversation endpoint is one. Here most rows of a long merge request
        are GitLab's own bookkeeping -- "added 1 commit", a label change, an
        approval -- so this reads on past them until it has `limit` comments or
        GitLab runs out, within `MAX_CONVERSATION_SIZE` rows, the shape
        `GitHubForge._issue_pages` uses for GitHub's own leak of proposals into
        issues. Counting rows instead would report a twelve-comment
        conversation truncated, and the sweep refuses a truncated one.

        A page is still judged full on the rows GitLab sent -- that is what
        says GitLab may hold more -- but a full page whose slots went to
        bookkeeping is a reason to read the next one, not to stop. Truncated
        when the last page read was full or more than `limit` comments
        survived.
        """
        per_page = min(limit, MAX_PAGE_SIZE)
        kept: list = []
        full = False
        for page in range(1, -(-MAX_CONVERSATION_SIZE // per_page) + 1):
            params: dict[str, Any] = {"sort": "asc", "order_by": "created_at", "per_page": per_page}
            if page > 1:
                params["page"] = page
            batch = api("GET", path, params=params) or []
            kept += [translate.comment(n) for n in batch if not translate.is_system_note(n)]
            full = len(batch) >= per_page
            if len(kept) >= limit or not full:
                break
        return kept[:limit], full or len(kept) > limit

    @staticmethod
    def _label_params(payload: dict) -> dict[str, str]:
        # Validated before any call, so a bad label leaves nothing half-applied.
        # One update call carries both halves on GitLab, so there is no ordering
        # between a label write and the text write to get wrong.
        add = validate_labels(payload.get("labelsAdd"))
        remove = validate_labels(payload.get("labelsRemove"))
        params: dict[str, str] = {}
        if add:
            params["add_labels"] = ",".join(add)
        if remove:
            params["remove_labels"] = ",".join(remove)
        return params

    def _text_fields(self, payload: dict) -> dict[str, str]:
        body: dict[str, str] = {}
        if payload.get("title") is not None:
            body["title"] = validate_text(payload.get("title"), "title").strip()
        if payload.get("body") is not None:
            body["description"] = validate_text(payload.get("body"), "body", required=False)
        return body

    def can_write(
        self, api: Callable, repo: str, login: str, bot: bool = False
    ) -> bool | None:
        """Whether `login` may push to `repo`: a Developer or above.

        GitLab keys membership on a user id, so the login is looked up first.
        A login nobody holds, or a member below Developer, is a definitive no;
        a lookup that failed is not an answer and says so.

        An automation is a no whatever its role. A comment's author carries no
        `bot` field, so a service account with a name of its own reads as a
        person until here, where the user object is read: the username search
        may leave `bot` out for a token that is not an administrator's, so the
        user itself is asked when it does. Answering yes for one would make
        another automation's comment a request the agent acts on.
        """
        if not login:
            return False
        if bot:
            # The caller already knows: the comment's author was read as an
            # automation (this forge's own `is_automation`). No lookup can
            # turn that into a writer, so none is spent.
            return False
        try:
            users = api("GET", "users", params={"username": login}) or []
        except WorkspaceError:
            return None
        if not users:
            return False
        user_id = users[0].get("id")
        bot = users[0].get("bot")
        if bot is None:
            try:
                bot = (api("GET", f"users/{user_id}") or {}).get("bot")
            except WorkspaceError:
                return None
        if bot:
            return False
        try:
            member = api("GET", f"{self._project(repo)}/members/all/{user_id}")
        except WorkspaceError as exc:
            return False if exc.status == 404 else None
        return int((member or {}).get("access_level") or 0) >= DEVELOPER_ACCESS

    # -- proposals ----------------------------------------------------------

    def proposal_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        title = validate_text(payload.get("title"), "title").strip()
        if payload.get("draft") and not translate.is_draft_title(title):
            # The `draft` field is accepted and ignored on create; the prefix is
            # what GitLab reads.
            title = f"Draft: {title}"
        body = {
            "title": title,
            "description": validate_text(payload.get("body"), "body", required=False),
            "source_branch": validate_branch(payload.get("source"), "source"),
            "target_branch": validate_branch(payload.get("target"), "target"),
        }
        node = api("POST", f"{self._project(repo)}/merge_requests", body=body)
        return {"proposal": translate.proposal(node, repo)}

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        neutral = validate_state(payload.get("state"))
        # The neutral `closed` is every proposal that is no longer open, merged
        # ones included, as GitHub's is. GitLab's `closed` excludes merged, so
        # that one is asked as `all` and filtered here -- which makes a closed
        # page a page of `all` with the open ones taken out: it can be short,
        # or empty, while closed ones exist further on, and `truncated` is
        # judged on what GitLab sent so it says so.
        params: dict[str, Any] = {
            "state": "all" if neutral == "closed" else _states(neutral),
            "per_page": limit,
            "order_by": "created_at",
            "sort": "desc",
        }
        page = validate_page(payload.get("page"))
        if page > 1:
            params["page"] = page
        source = payload.get("source")
        if source is not None:
            params["source_branch"] = validate_branch(source, "source")
        target = payload.get("target")
        if target is not None:
            params["target_branch"] = validate_branch(target, "target")
        labels = _filter_labels(payload.get("labels"))
        if labels:
            # GitLab's `labels` filter matches proposals carrying every one.
            params["labels"] = ",".join(labels)
        if source is not None:
            return self._own_branch_proposals(api, repo, params, neutral, limit, page)
        nodes = api("GET", f"{self._project(repo)}/merge_requests", params=params) or []
        proposals = [translate.proposal(node, repo) for node in nodes]
        if neutral == "closed":
            proposals = [item for item in proposals if item["state"] != "open"]
        # Judged on what GitLab sent: a full page filtered down is still a page.
        return listing(proposals, limit, "proposals", returned=len(nodes))

    def _own_branch_proposals(
        self, api: Callable, repo: str, params: dict, neutral: str, limit: int, page: int
    ) -> dict[str, Any]:
        """A source-filtered listing, paged over this repository's own proposals.

        A branch of the same name on a fork is not this repository's branch,
        and must not answer "is there an open proposal for the branch I just
        published" -- but forks are only told apart after a page arrives, so
        GitLab's own pages of `limit` could be filled by forks and hide this
        repository's. The rows are read in pages of the most GitLab serves,
        filtered, and paged here, so page 2 continues page 1 rather than
        coming from a second, differently sized pagination. Bounded by the
        conversation row bound; a source branch rarely has more than a few.
        """
        want = page * limit
        own: list = []
        full = False
        for fetch in range(1, -(-MAX_CONVERSATION_SIZE // MAX_PAGE_SIZE) + 1):
            batch_params = dict(params, per_page=MAX_PAGE_SIZE, page=fetch)
            nodes = api("GET", f"{self._project(repo)}/merge_requests", params=batch_params) or []
            for node in nodes:
                item = translate.proposal(node, repo)
                if item["sourceRepo"] != repo:
                    continue
                if neutral == "closed" and item["state"] == "open":
                    continue
                own.append(item)
            full = len(nodes) >= MAX_PAGE_SIZE
            if len(own) > want or not full:
                break
        chunk = own[(page - 1) * limit : want]
        more = len(own) > want or full
        return listing(chunk, limit, "proposals", returned=limit if more else 0)

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        base = f"{self._project(repo)}/merge_requests/{number}"
        node = api("GET", base)
        result: dict[str, Any] = {"proposal": translate.proposal(node, repo)}
        if payload.get("comments"):
            limit = validate_comment_limit(payload.get("limit"))
            comments, truncated = self._notes(api, f"{base}/notes", limit)
            result["comments"] = comments
            result["commentCount"] = len(comments)
            result["commentsTruncated"] = truncated
        if payload.get("diff"):
            result["diff"] = self._diff(api, base)
        return result

    def _diff(self, api: Callable, base: str) -> str:
        """A unified diff of the merge request.

        `raw_diffs` is the unified diff itself, and answers 5xx until GitLab
        has computed it -- seconds after a merge request is opened -- and 404
        on a self-managed instance older than the route. The JSON `diffs`
        endpoint carries the same hunks per file, so that is the fallback
        rather than a retry loop with a sleep in it. A merge request that does
        not exist answers 404 there too, so the fallback cannot hide one.

        The fallback is paged, and it says so in the diff when it stops early
        or when GitLab left a file's hunks out: an omission the caller cannot
        see reads as a file the change did not touch.
        """
        try:
            return api("GET", f"{base}/raw_diffs", raw="text/plain")
        except WorkspaceError as exc:
            # GitLab's own 5xx or 404 only. The transport's own refusals --
            # an answer over the broker's ceiling, a deadline that passed --
            # come back as 502 too, and falling back on those would fetch the
            # very diff the ceiling refused, ten pages at a time.
            if not (exc.status == 404 or exc.fields.get("code") == FORGE_UNAVAILABLE):
                raise
        out: list[str] = []
        size = 0
        for page in range(1, self.DIFF_PAGES + 1):
            files = api(
                "GET", f"{base}/diffs", params={"per_page": MAX_PAGE_SIZE, "page": page}
            ) or []
            for item in files:
                old, new = item.get("old_path") or "", item.get("new_path") or ""
                out.append(f"diff --git a/{old} b/{new}\n")
                if not item.get("diff") and (item.get("too_large") or item.get("collapsed")):
                    out.append(f"# GitLab did not include the changes to {new}: too large to show\n")
                    continue
                out.append("--- " + ("/dev/null" if item.get("new_file") else f"a/{old}") + "\n")
                out.append("+++ " + ("/dev/null" if item.get("deleted_file") else f"b/{new}") + "\n")
                out.append(item.get("diff") or "")
                size += len(item.get("diff") or "")
            if size > self.DIFF_FALLBACK_CHARS:
                out.append(
                    f"# diff cut short: stopped after {size} characters of changes\n"
                )
                return "".join(out)
            if len(files) < MAX_PAGE_SIZE:
                return "".join(out)
        out.append(
            f"# diff cut short: only the first {self.DIFF_PAGES * MAX_PAGE_SIZE} files are shown\n"
        )
        return "".join(out)

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"{self._project(repo)}/merge_requests/{number}/notes",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node)}

    def proposal_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        body: dict[str, Any] = {**self._label_params(payload), **self._text_fields(payload)}
        path = f"{self._project(repo)}/merge_requests/{number}"
        if "title" in body and not translate.is_draft_title(body["title"]):
            # On GitLab the draft marker is the title, and this forge may have
            # written it itself on create, where the caller never typed it. A
            # new title without it would mark the merge request ready -- on
            # GitHub the same call leaves `draft` alone -- so a draft keeps its
            # marker unless the caller asks otherwise by writing its own.
            current = api("GET", path) or {}
            if translate.proposal(current, repo)["draft"]:
                body["title"] = f"Draft: {body['title']}"
        # GitLab refuses an update that changes nothing (400, "at least one
        # parameter"), where GitHub answers the unchanged proposal; the answer
        # this verb promises is the proposal as it stands, so read it instead.
        node = api("PUT", path, body=body) if body else api("GET", path)
        return {"proposal": translate.proposal(node, repo)}

    def proposal_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "PUT",
            f"{self._project(repo)}/merge_requests/{number}",
            body={"state_event": "close"},
        )
        return {"proposal": translate.proposal(node, repo)}

    def proposal_commits(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        limit = validate_limit(payload.get("limit"))
        page = validate_page(payload.get("page"))
        # GitLab lists a merge request's commits newest first and has no
        # parameter to turn that round; the verb promises oldest first, with
        # page 1 holding the oldest. Reversing each page would not do it -- on
        # a merge request longer than one page, page 1 would be the newest
        # commits -- so the list is read whole, up to the ceiling, and paged
        # here. Past the ceiling it is the newest COMMIT_CAP commits, where
        # GitHub's is the oldest; a caller after the tip reads `sourceRevision`
        # off the proposal on both.
        path = f"{self._project(repo)}/merge_requests/{number}/commits"
        nodes: list[dict] = []
        for fetch in range(1, -(-self.COMMIT_CAP // MAX_PAGE_SIZE) + 1):
            batch = api("GET", path, params={"per_page": MAX_PAGE_SIZE, "page": fetch}) or []
            nodes.extend(batch)
            if len(batch) < MAX_PAGE_SIZE:
                break
        commits = [translate.commit(node) for node in reversed(nodes[: self.COMMIT_CAP])]
        return listing(commits[(page - 1) * limit : page * limit], limit, "commits")

    def proposal_acknowledge(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Best-effort by contract. Any note -- conversation or diff -- takes an
        # award emoji; GitLab keys it on the merge request and the note
        # together, which is why `number` is in the request shape.
        number = validate_number(payload.get("number"), "number")
        comment = payload.get("comment") or {}
        if not isinstance(comment, dict):
            raise WorkspaceError("comment must be the {id, kind} of a comment")
        ident = validate_number(comment.get("id"), "comment.id")
        if str(comment.get("kind") or "") not in translate.ACKNOWLEDGEABLE:
            return {"acknowledged": False}
        try:
            api(
                "POST",
                f"{self._project(repo)}/merge_requests/{number}/notes/{ident}/award_emoji",
                body={"name": ACKNOWLEDGE_EMOJI},
            )
        except WorkspaceError as exc:
            # Already awarded is the state the caller asked for. GitLab says so
            # with a 404 whose message is the validation error, so the wording
            # is what tells it from a note that is not there.
            detail = str(exc.fields.get("detail") or "").lower()
            if not (exc.status == 404 and ALREADY_AWARDED in detail):
                raise
        return {"acknowledged": True}

    # -- issues -------------------------------------------------------------

    def issue_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "description": validate_text(payload.get("body"), "body", required=False),
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            body["labels"] = ",".join(labels)
        node = api("POST", f"{self._project(repo)}/issues", body=body)
        return {"issue": translate.issue(node)}

    def issue_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        params: dict[str, Any] = {
            "state": _states(validate_state(payload.get("state"))),
            "per_page": limit,
            "order_by": "created_at",
            "sort": "desc",
        }
        labels = _filter_labels(payload.get("labels"))
        if labels:
            params["labels"] = ",".join(labels)
        # Both halves of the filter are GitLab's own parameters on the listing
        # endpoint -- `not[labels]` and `search` -- so unlike GitHub there is
        # no second route with its own grammar and ordering.
        excluded = _filter_labels(payload.get("excludeLabels"))
        if excluded:
            params["not[labels]"] = ",".join(excluded)
        query = validate_text(payload.get("query"), "query", required=False).strip()
        if query:
            params["search"] = query
        # GitLab's issues endpoint returns issues only; GitHub's mixes in pull
        # requests, which is why this side needs no filtering and no paging past
        # them.
        nodes = api("GET", f"{self._project(repo)}/issues", params=params) or []
        return listing([translate.issue(node) for node in nodes], limit, "issues")

    def issue_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        base = f"{self._project(repo)}/issues/{number}"
        node = api("GET", base)
        result: dict[str, Any] = {"issue": translate.issue(node)}
        if payload.get("comments"):
            limit = validate_comment_limit(payload.get("limit"))
            comments, truncated = self._notes(api, f"{base}/notes", limit)
            result["comments"] = comments
            result["commentCount"] = len(comments)
            result["commentsTruncated"] = truncated
        return result

    def issue_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"{self._project(repo)}/issues/{number}/notes",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node)}

    def issue_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        body: dict[str, Any] = {**self._label_params(payload), **self._text_fields(payload)}
        path = f"{self._project(repo)}/issues/{number}"
        node = api("PUT", path, body=body) if body else api("GET", path)
        return {"issue": translate.issue(node)}

    def issue_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        reason = validate_text(payload.get("reason"), "reason", required=False).strip()
        # Validated for parity with every forge; GitLab records no close reason.
        if reason and reason not in ("completed", "not-planned"):
            raise WorkspaceError("reason must be one of completed, not-planned")
        node = api(
            "PUT", f"{self._project(repo)}/issues/{number}", body={"state_event": "close"}
        )
        return {"issue": translate.issue(node)}

    # -- labels -------------------------------------------------------------

    def label_ensure(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Read, then create or update, as on every forge.
        name = validate_labels([payload.get("name")])[0]
        color = validate_text(payload.get("color"), "color", required=False).strip().lstrip("#")
        description = validate_text(payload.get("description"), "description", required=False)
        quoted = quote(name, safe="")
        labels = f"{self._project(repo)}/labels"
        try:
            existing = api("GET", f"{labels}/{quoted}")
        except WorkspaceError as exc:
            if exc.status != 404:
                raise
            body: dict[str, Any] = {"name": name, "color": f"#{color}" if color else DEFAULT_LABEL_COLOR}
            if description:
                body["description"] = description
            node = api("POST", labels, body=body)
        else:
            changes: dict[str, Any] = {}
            if color:
                changes["color"] = f"#{color}"
            if description:
                changes["description"] = description
            node = api("PUT", f"{labels}/{quoted}", body=changes) if changes else existing
        return {"label": translate.label(node)}
