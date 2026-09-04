#!/usr/bin/env python3
"""forge.py — the forge operations this harness needs, behind one seam.

Staged into `$HERMES_HOME/scripts` by the entrypoint's step 2b force-sync, so
every skill script on the Platform Agent's `sys.path` can import it.

What this is for
----------------
Reading and answering a pull-request conversation needs five things from a
code-hosting service: who am I, which pull requests are open, what has been said
on one, say something back, and acknowledge that a request was seen. Keeping an
open pull request mergeable needs two more: does this branch conflict with its
base, and what is red on its head commit. Those are the whole
forge-shaped surface of both features; everything above them — what counts as
addressing the agent, who is allowed to, when a request has already been
answered, how many times the agent may try to fix one branch — is harness policy
that does not change between forges.

Splitting the two here is what makes a second forge a new class rather than a
second copy of the sweep. It is *not* a claim that a second forge is cheap:
`docs/designs/multi-forge-support.md` §5 covers the layers under this module —
token brokering, the credential sidecar's executable allowlist, git credential
shape, the egress policy — and §6 the CRD, each of which would need work first.
The seam is here so that when that work happens it lands in one place.

Why `_call` exists
------------------
Every provider method reaches `gh` through one method. The agent container holds
no GitHub token: `gh` is proxied to the credential sidecar, which is also why
`ALLOWED_EXECUTABLES` is a closed list. Bitbucket has no comparable CLI, so a
Bitbucket provider cannot shell anything at all — it needs a `/v1/<forge>/…`
route on that sidecar. Funnelling every call through one override point means
that provider replaces one method instead of reimplementing every read.

Three normalisations, and the forge that forced each
----------------------------------------------------
* **`Comment.can_write` is a boolean, not GitHub's `authorAssociation`.** The
  provider answers "may this account direct the agent?" and the caller never
  sees a forge's vocabulary. GitHub appears to hand the association over free on
  every comment, but it is not usable here: `author_association` is reported
  relative to what the *authenticated viewer* can see, and an App installation
  token cannot see organisation membership. A repository admin's comment comes
  back `CONTRIBUTOR` under this credential — observed live — so trusting the
  field refuses the very people entitled to direct the agent. The provider asks
  `repos/{repo}/collaborators/{user}/permission` instead and caches the answer
  per account for the tick, which is the same members lookup GitLab and
  Bitbucket would need.
* **`supports_acknowledge` is a capability, not an assumption.** Bitbucket Cloud
  has no reactions on pull-request comments. A caller that assumed the 👀 would
  either crash there or silently skip it; a flag makes the absence legible.
* **`normalise_login` strips a trailing `[bot]`.** GitHub's REST and GraphQL
  APIs disagree about whether an App's login carries the suffix — `AGENTS.md`
  records the same discrepancy for `kube-agents-bot`. Comparing an unnormalised
  login against a comment author is how an agent ends up answering itself
  forever. Every login crossing this seam goes through it, including the one
  `viewer_login` reads out of the credential store.

On the repository parser
------------------------
`_parse_repo` is a deliberate copy of `github-issue-resolver`'s
`get_target_repo`, not a reference to it: a shared module must not import from a
skill. Two copies can drift, so `test_forge.py` runs both parsers over one corpus
and fails when they disagree. That test is the thing to delete — along with this
copy — when `resolver.py` migrates onto this module, which
`docs/designs/pr-comment-conversation.md` §7 keeps out of scope for now.

The looser parser in `gitops_workspace.repo_from_settings` is deliberately not
reused. It strips a `github.com/` prefix and otherwise takes the last two path
segments, so `https://evil.com/github.com/attacker/repo` resolves to
`attacker/repo`. That is out of scope here and noted rather than fixed.

A third parser, `github_token_refresh.github_repo_from_remote`, reads the git
remote rather than a configured value and rejects a non-GitHub host outright.
Its host set carries `ssh.github.com` — GitHub's SSH-over-443 endpoint, which
appears in a clone URL but not in a `SETTINGS.md` repository line — so a remote
of that form resolves there and not here.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, Sequence

from github_token_refresh import (
    GH_MISSING_RC,
    GH_TIMEOUT_RC,
    is_refresh_failed,
    looks_like_auth_failure,
    refresh_credentials_once,
)

LOGGER = logging.getLogger(__name__)

SETTINGS_PATH = "/opt/data/SETTINGS.md"

#: How long any single `gh` call may take. A hung proxy must not hold the cron
#: tick's per-job lock open indefinitely.
GH_TIMEOUT_S = 60

#: Page size for the pull-request list. `gh api --paginate` merges the pages
#: into one JSON array (verified against gh 2.92 on the live install), so this
#: bounds the number of round trips rather than the number of results — unlike
#: `gh pr list --limit`, which truncates at its ceiling and says nothing.
PR_PAGE_SIZE = 100

#: The branch prefix the agent's own pull requests carry. Only one place writes
#: it in code — `audit_report.group_branch_for` — and `submit-suggestion`'s
#: SKILL.md instructs the model to use it, which `submit_suggestion.check_branch`
#: does not enforce: that function rejects an empty or protected branch name and
#: nothing else. So this is a convention held up by a prompt, which is exactly
#: why a prefix alone is not enough to call a pull request the agent's — see
#: `is_agent_pull_request` for the two checks that actually carry the weight.
AGENT_BRANCH_PREFIX = "platform-agent/"

#: A label that opts a pull request out of every sweep, matching the convention
#: `github-issue-resolver` already honours on issues.
IGNORE_LABEL = "agent:ignore"

#: The operator writes this literal when no GitOps repo is configured
#: (`buildSettingsConfigMap` in `platformagent_manifests.go`). It means absent,
#: not malformed — a distinction the two callers branch on differently.
SETTINGS_REPO_UNSET = "none"

# Host must sit at the *start* of the value, after an optional scheme and
# optional userinfo. Copied from resolver.py, whose comment explains why the
# obvious spellings admit `https://evil.com/github.com/attacker/repo`.
REPO_URL_RE = re.compile(
    r"^(?:(?:https?|git|ssh)://)?(?:[^/@]+@)?(?:www\.)?github\.com[/:]"
    r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"
)
BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: `permission` values from `repos/{repo}/collaborators/{user}/permission` that
#: carry the standing to direct the agent. GitHub collapses the `maintain` and
#: `triage` roles into this legacy field, so `maintain` arrives as `write` and
#: `triage` as `read` — which is the intended reading either way.
WRITE_PERMISSIONS = frozenset({"admin", "write", "maintain"})

#: `mergeable_state` while GitHub is still computing the merge. The merge is
#: computed lazily, on the first read after a push to either branch, so the
#: first poll after any push routinely lands here. It is an unknown, not a
#: clean bill: `_has_write` has the same three-valued shape for the same reason.
MERGEABLE_STATE_UNKNOWN = "unknown"

#: `mergeable_state` for a branch that conflicts with its base. The other values
#: — `blocked`, `behind`, `unstable`, `draft` — are all things a human or a
#: check decides and none of them is a conflict, so this is the only one of
#: *this field's* values that means one. `conflict_state` still ORs it with
#: `mergeable is False`, which is not a second reading of the same field: those
#: other states come back with `mergeable: true`, so the OR widens the answer
#: only where GitHub itself says the merge cannot be produced.
MERGEABLE_STATE_CONFLICTED = "dirty"

#: Check-run conclusions that mean CI is asking the author to change something.
#: `cancelled` and `stale` are excluded deliberately: a run somebody stopped, or
#: one a newer push superseded, says nothing about the code and re-running the
#: agent at it would spend a model turn on a verdict nobody reached.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure"}
)

#: Commit-status states that mean the same thing. Read as well as check runs
#: because GitHub keeps two registers and CI that predates the Checks API only
#: ever appears in this one — Prow, which gates this repository, is in it.
#: Reading one register is how a red pull request looks green.
FAILING_STATUS_STATES = frozenset({"failure", "error"})

#: Page size for the CI reads. Both endpoints answer with a JSON *object*
#: wrapping the array, and `gh api --paginate` concatenates objects into
#: something `json.loads` rejects, so these are one request each and this is a
#: real ceiling rather than a round-trip bound. A pull request with more than
#: this many checks reports the first page; the sweep's job is to notice that CI
#: is red, and a hundred checks in it will not be uniformly green.
CHECK_PAGE_SIZE = 100

#: How much of a check's name and details URL any caller may pass on. Both come
#: from whatever posted the check — anything holding `checks:write` or
#: `statuses:write` on the repository — so both are third-party text on its way
#: to a board entry or a prompt, and neither has a length GitHub bounds. They
#: live here, beside `CheckRun`, because a truncation width two callers each
#: keep their own copy of is two widths: a card and a `poll` row would then show
#: the model different amounts of the same name.
MAX_CHECK_NAME_CHARS = 120
MAX_CHECK_URL_CHARS = 300

#: What a check name may still carry once it leaves this module. Word
#: characters — Unicode, so a job named in a non-Latin script survives — spaces,
#: and the punctuation real names use: `build (ubuntu-latest, 3.12)`,
#: `e2e/kind`, `pull-kube-agents-smoke-test`.
#:
#: An allowlist rather than a denylist, because the characters that matter in
#: third-party text on its way to a prompt are the ones that end one context and
#: begin another, and the set of those grows every time a caller changes its
#: markup. A newline forges a second entry in the bullet list `_update_card`
#: builds; a backtick closes the code span the name sits inside; `<`, `[` and
#: `]` are the delimiters `pr_triggers` refuses outright in a slash request
#: (`HIDING_CHARS`) and `resolver.py` neutralises in an issue title. Truncation
#: is no substitute and stays where it is: cutting a name at 120 characters does
#: nothing about a newline in its first ten.
CHECK_NAME_DISALLOWED_RE = re.compile(r"[^\w .:/#+,()@'-]", re.UNICODE)

#: The only schemes a check's log URL may carry. The URL comes from whoever
#: posted the check — `checks:write` alone, no access to the code — and it
#: reaches a card body a model reads and may fetch. `javascript:` and `data:`
#: are the ones that are obviously not addresses; `file:` is the one that is
#: not obvious, because it looks like a path and names one inside the agent's
#: own container. Anything else is dropped to "" rather than rewritten: the
#: check's name and conclusion still reach the card, which is what a worker
#: needs to go and find the log itself.
CHECK_URL_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: `gh api` puts the HTTP status in its stderr line: `gh: Not Found (HTTP 404)`.
#: A 404 from the collaborator endpoint is an answer; every other failure is not.
HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")

#: The account line in `gh auth status`. Only the success spelling matches: a
#: broken credential reports "Failed to log in to … account <login>", and that
#: line names an account whose token no longer works.
VIEWER_RE = re.compile(r"Logged in to \S+ account (\S+)")

#: Definitive HTTP status codes in `gh` stderr that indicate permanent errors
#: (401 Bad Credentials, 403 Forbidden, 404 Not Found), which should not be
#: retried even for read-only queries.
DEFINITIVE_HTTP_STATUS_RE = re.compile(r"HTTP (?:401|403|404)\b", re.IGNORECASE)


class ForgeError(Exception):
    """A fault with a machine-readable reason code.

    The gate turns `reason` into the `⚠️` line an operator reads, so the codes
    are part of the contract rather than debug text. They match the vocabulary
    `resolver.py handle_poll` already emits, so one operator-facing glossary
    covers every sweep.
    """

    def __init__(self, reason: str, value: str = ""):
        super().__init__(f"{reason}: {value}" if value else reason)
        self.reason = reason
        self.value = value


class RepoUnparseable(ForgeError):
    """SETTINGS.md names a repository that could not be understood.

    Distinct from absent on purpose. Configuring nothing is a supported install
    with no work to do; configuring something unreadable is a fault, and
    silence there means the watcher stops working and nobody finds out.
    """

    def __init__(self, value: str):
        super().__init__("GIT_REPO_UNPARSEABLE", value)


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_ref: str
    author: str
    labels: tuple[str, ...] = ()
    url: str = ""
    #: The branch this pull request merges into. Carried because the `update-pr`
    #: skill merges it into the head branch to clear a conflict, and the default
    #: branch is not a safe guess: a stacked pull request bases on another
    #: feature branch, and merging `main` into it would be the wrong resolution
    #: rather than a failed one.
    base_ref: str = ""
    #: `owner/name` of the repository the head branch lives in. Empty when the
    #: fork it came from has been deleted, which `is_agent_pull_request` reads
    #: as "not ours" rather than as "unknown".
    head_repo: str = ""
    #: Tip of the head branch as the forge reported it on this read. Used to
    #: check a reply's claim to have amended the branch, so it is deliberately
    #: re-read at post time rather than carried from the sweep.
    head_sha: str = ""

    @property
    def is_ignored(self) -> bool:
        return IGNORE_LABEL in self.labels


@dataclass(frozen=True)
class Comment:
    """One utterance on a pull request, from whichever endpoint produced it.

    `node_id` rather than `id` is the identity used in answered-markers. It is
    globally unique and stable, where the numeric id is only unique within its
    own endpoint — a conversation comment and a review comment can share one,
    which would let an answer to either suppress the other.
    """

    node_id: str
    author: str
    body: str
    can_write: bool
    created_at: str
    #: False when the permission lookup did not answer — a proxy fault, a
    #: timeout, a 5xx. `can_write` is then False because every caller must fail
    #: closed, but the two are not the same fact: a refusal is a public comment
    #: carrying a marker that stops the request ever being retried, and posting
    #: one because the network hiccuped permanently refuses a maintainer.
    can_write_known: bool = True
    #: Which endpoint this came from: "issue", "review_comment", or "review".
    #: Routes the reaction API, which has a different path per kind and none at
    #: all for a review.
    kind: str = "issue"
    numeric_id: int = 0
    path: str = ""
    line: Optional[int] = None

    @property
    def is_bot(self) -> bool:
        return self.author.endswith("[bot]")


@dataclass(frozen=True)
class Commit:
    """One commit on a pull request's head branch.

    The date rides along with the sha because a reply's claim to have amended
    the branch is only true if the commit came *after* the request it answers.
    A sha alone answers "is this on the branch", which every commit the agent
    ever pushed satisfies — including the one that opened the pull request. See
    `pr_conversation._check_claim`.
    """

    sha: str
    #: ISO-8601 committer date as the forge reported it, or "" when it did not.
    #: Empty is unverifiable, which the caller treats as a failure rather than
    #: as a pass.
    committed_at: str = ""


@dataclass(frozen=True)
class CheckRun:
    """One red CI signal on a pull request's head commit.

    Flattened from GitHub's two registers — a check run has a `conclusion` and a
    commit status has a `state` — because every caller asks the same question of
    both: what is called what, and where does a human go to read why. `register`
    survives the flattening only so a report can say where it came from; nothing
    branches on it.
    """

    #: Already reduced by `plain_check_name`, so every consumer — a card body,
    #: a `poll` row, a report — gets the same constrained string and none of
    #: them has to remember to constrain it. `_pr_card` leans on `find_trigger`
    #: the same way rather than re-checking at the card.
    name: str
    #: The forge's own word for the failure: a check-run conclusion
    #: (`failure`, `timed_out`, …) or a status state (`failure`, `error`).
    conclusion: str
    #: Where the log lives. A `details_url` for a check run, a `target_url` for
    #: a status. Already reduced by `safe_check_url`, so it is either an
    #: `http`/`https` address or empty — empty when the reporter set neither,
    #: and empty when what it set was not a web address at all.
    details_url: str = ""
    #: "check_run" | "status"
    register: str = "check_run"


def plain_check_name(name: str) -> str:
    """`name` with everything `CHECK_NAME_DISALLOWED_RE` rejects taken out.

    Applied at ingest, where the name enters the process, rather than at each
    of the places it leaves it. A check name reaches a board entry that wakes a
    model holding a workspace lease, and whoever posted the check needs only
    `checks:write` or `statuses:write` on the repository — which an integration
    with no write access to the code still has.

    Disallowed characters become spaces and runs of whitespace collapse, so a
    name broken across two lines reads as one line rather than as one word.
    """
    return " ".join(CHECK_NAME_DISALLOWED_RE.sub(" ", name or "").split())


def safe_check_url(url: str) -> str:
    """`url` if it is an ordinary web address, otherwise "".

    The mechanical half of the rule `update-pr/SKILL.md` states in prose: a
    `details_url` is an address somebody else chose. Constrained at ingest for
    the reason `plain_check_name` is — one place, so no consumer has to
    remember — and by scheme only. Which hosts are worth reading is a judgment
    a self-hosted runner or an on-prem CI makes differently in every install,
    and an allowlist of them would break those installs silently; a scheme that
    is not `http` or `https` is wrong everywhere.

    Whitespace goes first, because a URL split across two lines would otherwise
    parse as a scheme-less path and be dropped for the wrong reason.
    """
    text = "".join(str(url or "").split())
    if not text:
        return ""
    try:
        scheme = urllib.parse.urlsplit(text).scheme.lower()
    except ValueError:
        # `urlsplit` raises on a malformed IPv6 literal, which is a URL nothing
        # downstream can use either.
        return ""
    return text if scheme in CHECK_URL_ALLOWED_SCHEMES else ""


class ForgeProvider(Protocol):
    """The complete forge-shaped surface of the pull-request features."""

    #: False on a forge with no reaction API (Bitbucket Cloud), so a caller can
    #: skip the acknowledgement rather than discover it fails.
    supports_acknowledge: bool

    def preflight(self) -> None: ...

    def viewer_login(self) -> str: ...

    def list_open_prs(self, repo: str) -> list[PullRequest]: ...

    def list_comments(self, repo: str, pr: PullRequest) -> list[Comment]: ...

    def post_comment(self, repo: str, pr: PullRequest, body_file: str) -> None: ...

    def acknowledge(self, repo: str, comment: Comment) -> bool: ...

    def list_commits(self, repo: str, pr: PullRequest) -> list[Commit]: ...

    def conflict_state(self, repo: str, pr: PullRequest) -> Optional[bool]: ...

    def failing_checks(self, repo: str, pr: PullRequest) -> list[CheckRun]: ...


def normalise_login(login: str) -> str:
    """Reduce every spelling of one account to a single key.

    GitHub gives an App three different logins for the same identity, and this
    sweep sees all three in one tick:

    * `gh pr list --json author` → `app/kube-agents`
    * REST comment authors      → `kube-agents[bot]`
    * an @-mention a human types → `kube-agents`

    Both affixes are stripped, because the comparison this feeds is what stops
    the agent answering itself. Matching `app/x` against `x[bot]` fails, no
    marker the agent wrote is ever recognised as its own, and every tick
    re-answers the same comment — observed live before this was normalised.
    """
    # Case is folded first, so the affix tests do not depend on the spelling the
    # forge happened to use for them.
    text = str(login or "").strip().lower()
    if text.startswith("app/"):
        text = text[len("app/") :]
    if text.endswith("[bot]"):
        text = text[: -len("[bot]")]
    return text


def is_agent_pull_request(pr: PullRequest, repo: str, viewer: str) -> bool:
    """Did the agent open this pull request, from a branch it wrote, here?

    All three conditions, because each one alone is something a stranger can
    arrange:

    * **The branch prefix alone is not ownership.** A pull request from a fork
      carries the bare branch name in `head.ref`, so anybody who can fork this
      repository can open one whose head ref reads `platform-agent/anything`.
      The sweep would then treat a stranger's pull request as the agent's own,
      and — worse — `submit-suggestion` amends by pushing `head_ref` to *this*
      repository, creating a branch under a name the stranger chose.
    * **The author alone is not ownership either.** The agent opens pull
      requests for GitOps changes on `platform-agent/*`; a pull request it
      opened for some other purpose is not a review conversation this feature
      should be driving.
    * **`viewer` is the account this credential authenticates as**, not the
      author of the pull request being examined. Deriving the agent's identity
      from the thing it is deciding about is circular: on a pull request that
      is not the agent's, `pr.author` is a human, the marker scan then looks for
      the agent's bookkeeping in that human's comments, finds none, and the same
      request is answered again on every tick forever.

    An empty `viewer` means the credential could not name itself, and everything
    is refused. See `GitHubProvider.viewer_login`.
    """
    if not viewer or not pr.head_repo:
        return False
    return (
        normalise_login(pr.author) == normalise_login(viewer)
        and pr.head_ref.startswith(AGENT_BRANCH_PREFIX)
        and pr.head_repo.lower() == repo.lower()
    )


def _valid_repo_component(part: str) -> bool:
    """Reject path components unsafe to hand to `gh -R`.

    The slug pattern permits "." and "-", so it happily produces "../..", and a
    leading dash is parsed by `gh` as a flag. Neither is a shape the regex can
    express.
    """
    return bool(part) and part not in (".", "..") and not part.startswith("-")


def _parse_repo(configured: str) -> str:
    """`owner/name` from a configured value, or raise `RepoUnparseable`."""
    match = REPO_URL_RE.search(configured)
    if match:
        repo = match.group(1)
    elif BARE_REPO_RE.match(configured):
        repo = configured
    else:
        raise RepoUnparseable(configured)

    repo = re.sub(r"\.git$", "", repo)
    owner, _, name = repo.partition("/")
    # After the shorthand branch, not instead of it: "../.." satisfies
    # BARE_REPO_RE, so this is what rejects it.
    if not _valid_repo_component(owner) or not _valid_repo_component(name):
        raise RepoUnparseable(configured)
    return repo


def _should_retry_transient(result: subprocess.CompletedProcess) -> bool:
    """Return True if a non-zero `gh` result is worth a single transient retry.

    Definitive HTTP failures (401, 403, 404), missing binary (GH_MISSING_RC),
    and sidecar timeouts (GH_TIMEOUT_RC) are skipped.
    """
    if result.returncode in (GH_MISSING_RC, GH_TIMEOUT_RC):
        return False
    if DEFINITIVE_HTTP_STATUS_RE.search(result.stderr or ""):
        return False
    return True


def run_gh_once(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """Run one gh command without retry, mapping a missing binary onto a return code."""
    try:
        return subprocess.run(
            ["gh", *argv],
            check=False,
            text=True,
            capture_output=True,
            timeout=GH_TIMEOUT_S,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            ["gh", *argv], GH_MISSING_RC, stdout="", stderr="'gh' not found in PATH."
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["gh", *argv],
            GH_TIMEOUT_RC,
            stdout="",
            stderr=f"'gh' timed out after {GH_TIMEOUT_S}s.",
        )


def run_gh(
    argv: Sequence[str], repo: Optional[str] = None
) -> subprocess.CompletedProcess:
    """One `gh` invocation, never raising for a non-zero exit.

    A failed call gets one retry behind a freshly minted token on auth failures.
    Callers here always need the reason code more than the exception: a token
    without scope for this repository and a repository that 404s both exit
    non-zero with usable stderr, and turning that into a traceback loses it.
    A missing binary is reported as `GH_MISSING_RC` so it stays distinguishable
    from a command that ran and failed.
    """
    result = run_gh_once(argv)
    if looks_like_auth_failure(argv, result) and refresh_credentials_once(
        argv, repo=repo
    ):
        result = run_gh_once(argv)
    return result


def gh_preflight(run: Callable[[Sequence[str]], subprocess.CompletedProcess] = run_gh):
    """Raise `ForgeError` when `gh` cannot authenticate at all.

    Passing means only that *some* host is authenticated — a token without scope
    for the target repository still fails later, which is why every list call
    below reports `REPO_UNREACHABLE` on its own rather than trusting this.
    """
    result = run(["auth", "status"])
    if result.returncode == 0:
        return
    if is_refresh_failed():
        raise ForgeError("GITHUB_TOKEN_REFRESH_FAILED")
    raise ForgeError(
        "GH_CLI_NOT_FOUND"
        if result.returncode == GH_MISSING_RC
        else "GITHUB_AUTH_NOT_CONFIGURED"
    )


class GitHubProvider:
    """`ForgeProvider` over the proxied `gh` CLI."""

    supports_acknowledge = True
    name = "github"

    def __init__(self, run: Optional[Callable] = None):
        """Initialize the GitHub provider.

        Args:
            run: Runner callable matching `run(argv, repo=None)`. Defaults to
                `run_gh`, which passes `repo` to credential refresh on auth failure.
        """
        self._run = run or run_gh
        # One entry per distinct commenter per provider instance, which the
        # gate builds fresh each tick. A busy thread is usually three or four
        # people, so this keeps the permission lookups to three or four calls
        # rather than one per comment. None is a cached "did not answer".
        self._permission_cache: dict[str, Optional[bool]] = {}
        # Resolved at most once per tick; "" is a real answer meaning the
        # credential could not name itself, and is cached as such.
        self._viewer: Optional[str] = None

    # -- the seam ---------------------------------------------------------
    def _call(
        self,
        argv: Sequence[str],
        *,
        repo: Optional[str] = None,
        expect_json: bool = True,
        retry_transient: bool = False,
    ):
        """Every forge round trip goes through here. See the module docstring.

        Returns parsed JSON, or None for a call made only for its effect. A
        non-zero exit raises `REPO_UNREACHABLE`, which is the honest reading of
        a `gh` failure that survived the preflight: the credential works
        somewhere, just not here.

        When `repo` is provided, it is passed to the runner for credential refresh
        context on authentication failure.

        When `retry_transient=True` (for read-only queries), a non-zero exit gets
        a single bounded retry before raising `REPO_UNREACHABLE`. Mutating calls
        (e.g., post_comment, acknowledge) must leave `retry_transient=False` to
        avoid double-posting on a sidecar timeout.
        """
        result = self._run(list(argv), repo=repo)
        if (
            result.returncode != 0
            and retry_transient
            and _should_retry_transient(result)
        ):
            result = self._run(list(argv), repo=repo)
        if result.returncode != 0:
            raise ForgeError("REPO_UNREACHABLE", (result.stderr or "").strip()[:200])
        if not expect_json:
            return None
        text = (result.stdout or "").strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ForgeError("FORGE_RESPONSE_UNREADABLE", str(exc)) from exc

    # -- the forge operations ----------------------------------------------
    def preflight(self) -> None:
        """`gh_preflight` against *this* provider's runner.

        A method rather than a bare call so a caller holding a provider never
        has to reach past it to the module-level default — which under test
        would be the real `gh`.
        """
        gh_preflight(self._run)

    def viewer_login(self) -> str:
        """The account this credential authenticates as.

        `gh auth status` rather than `GET /user`, because the agent holds a
        GitHub App *installation* token and `/user` answers `401 Bad
        credentials` for one — verified on the live install. `auth status` reads
        the account out of the credential store the sidecar wrote when it minted
        the token, so it costs no API call and works for a token that cannot
        introspect itself.

        Empty when no working account can be read. Every caller treats that as
        "sweep nothing": this login is what separates the agent's own pull
        requests from a stranger's, and its own comments from a reviewer's, so
        proceeding without it is how the agent starts answering itself.
        """
        if self._viewer is not None:
            return self._viewer
        result = self._run(["auth", "status"])
        # `gh` has moved this between streams across versions, and the sidecar
        # merges neither, so read both rather than depend on which one it is.
        match = VIEWER_RE.search(f"{result.stdout or ''}\n{result.stderr or ''}")
        self._viewer = normalise_login(match.group(1)) if match else ""
        return self._viewer

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        """Every open pull request, paginated rather than truncated.

        REST rather than `gh pr list`, for two reasons that both bite on a busy
        repository. `--limit` truncates at its ceiling and human pull requests
        share that budget, so the agent's own would eventually fall out of the
        window — and the old code raised on a full page, which on a `deliver:
        "all"` job at `*/10` is 144 identical warnings a day with the feature
        switched off. `gh api --paginate` merges the pages instead.

        The other reason is `head.repo.full_name`, which `gh pr list` does not
        carry and which is the only field that tells a branch in this repository
        from a same-named branch on somebody's fork.
        """
        rows = self._call(
            [
                "api",
                f"repos/{repo}/pulls?state=open&per_page={PR_PAGE_SIZE}",
                "--paginate",
            ],
            repo=repo,
            retry_transient=True,
        )
        return [
            PullRequest(
                number=int(row.get("number", 0)),
                head_ref=str((row.get("head") or {}).get("ref", "")),
                # Null when the fork has been deleted, which reads as "" and is
                # rejected by `is_agent_pull_request` rather than assumed local.
                head_repo=str(
                    ((row.get("head") or {}).get("repo") or {}).get("full_name", "")
                ),
                head_sha=str((row.get("head") or {}).get("sha", "")),
                base_ref=str((row.get("base") or {}).get("ref", "")),
                author=str((row.get("user") or {}).get("login", "")),
                labels=tuple(
                    str(label.get("name", "")) for label in (row.get("labels") or [])
                ),
                url=str(row.get("html_url", "")),
            )
            for row in (rows or [])
        ]

    def list_comments(self, repo: str, pr: PullRequest) -> list[Comment]:
        """Every utterance on one pull request, from all three endpoints.

        GitHub splits a single human-visible conversation across three: the
        conversation tab (`issues/N/comments`), inline review comments
        (`pulls/N/comments`), and the summary body of a review
        (`pulls/N/reviews`). A reviewer typing "@agent please fix this" has no
        idea which one they used, so reading fewer than three means the agent
        ignores requests at random.

        `--paginate` is not optional. The default page is 30, and a truncated
        list looks exactly like a complete one — the same trap `AGENTS.md`
        flags for reading this bot's own review comments.
        """
        out: list[Comment] = []
        out.extend(
            self._collect(
                f"repos/{repo}/issues/{pr.number}/comments", kind="issue", repo=repo
            )
        )
        out.extend(
            self._collect(
                f"repos/{repo}/pulls/{pr.number}/comments",
                kind="review_comment",
                repo=repo,
            )
        )
        out.extend(
            self._collect(
                f"repos/{repo}/pulls/{pr.number}/reviews", kind="review", repo=repo
            )
        )
        # Oldest first: the cap takes the oldest unanswered triggers, so a
        # request must not be starved by newer ones arriving in the same tick.
        out.sort(key=lambda c: (c.created_at, c.node_id))
        return out

    def _has_write(self, repo: str, login: str) -> Optional[bool]:
        """May `login` direct the agent on `repo`? None when nothing answered.

        Asks the collaborator-permission endpoint rather than reading
        `author_association` off the comment — see the module docstring for the
        App-token blindness that makes the field useless here.

        A non-collaborator 404s, and that is a definitive no. Any other failure
        — a proxy fault, a timeout, a 5xx — is not an answer, and collapsing the
        two into one `False` is worse than it looks: the sweep answers `False`
        with a public refusal carrying `<!-- agent-refused:… -->`, and that
        marker is exactly what stops the request being retried. A five-second
        network blip would refuse a maintainer permanently, and they would have
        to notice and re-comment. So the unknown gets its own value and the
        caller waits for the next tick.

        The login is percent-encoded into the path. A GitHub App comments as
        `<name>[bot]`, and `[`/`]` are not path characters — unencoded that is a
        malformed URL rather than a 404, so the failure is not "no such
        collaborator", every allowlisted bot caches as `None`, and the sweep
        re-asks the same unanswerable question on every tick.
        """
        if not login:
            return False
        # Keyed on the login this actually asks about, not on `normalise_login`,
        # which exists to make a mention match a handle and collapses accounts
        # that are not the same account: it strips a trailing `[bot]` and a
        # leading `app/`, so the App `foo[bot]` and the user `foo` shared one
        # slot and whichever `_collect` reached first decided trust for both.
        # One direction hands a non-collaborator `can_write=True` and clears the
        # sweep's only trust gate; the other refuses a maintainer and writes a
        # public `agent-refused` marker that `refused_node_ids` treats as
        # permanent. Neither needs timing luck — permission is resolved eagerly
        # for every comment author on every swept pull request, so once both
        # accounts have commented the wrong answer is re-derived every tick.
        #
        # Case is folded because GitHub logins are case-insensitive, so `Foo`
        # and `foo` are one account and one lookup. Nothing else is folded. The
        # `app/` and bare-name spellings `normalise_login` handles come from
        # `gh pr list` and from mention text, neither of which reaches here.
        key = login.lower()
        if key in self._permission_cache:
            return self._permission_cache[key]
        quoted = urllib.parse.quote(login, safe="")
        try:
            data = self._call(
                ["api", f"repos/{repo}/collaborators/{quoted}/permission"],
                repo=repo,
            )
        except ForgeError as error:
            status = HTTP_STATUS_RE.search(error.value or "")
            if not status or status.group(1) != "404":
                self._permission_cache[key] = None
                return None
            allowed = False
        else:
            permission = str((data or {}).get("permission") or "").strip().lower()
            allowed = permission in WRITE_PERMISSIONS
        self._permission_cache[key] = allowed
        return allowed

    def _collect(self, path: str, *, kind: str, repo: str) -> Iterable[Comment]:
        rows = (
            self._call(
                ["api", path, "--paginate"], repo=repo, retry_transient=True
            )
            or []
        )
        for row in rows:
            body = str(row.get("body") or "")
            # A review with no summary body is an approval or a state change,
            # not an utterance. Keeping it would give the marker scan an empty
            # comment to match nothing against on every tick.
            if kind == "review" and not body.strip():
                continue
            author = str((row.get("user") or {}).get("login", ""))
            access = self._has_write(repo, author)
            yield Comment(
                node_id=str(row.get("node_id") or ""),
                numeric_id=int(row.get("id") or 0),
                author=author,
                body=body,
                can_write=bool(access),
                can_write_known=access is not None,
                created_at=str(row.get("submitted_at") or row.get("created_at") or ""),
                kind=kind,
                path=str(row.get("path") or ""),
                line=row.get("line"),
            )

    def post_comment(self, repo: str, pr: PullRequest, body_file: str) -> None:
        """Post from a file, never from an argv string.

        The body carries a reviewer's own words back to them and can run to
        thousands of characters; `--body` would put all of that on a command
        line, through a proxy, with the quoting rules of two shells in between.
        `--body-file` is also what `audit_report.py` and `resolver.py` use.
        """
        self._call(
            ["pr", "comment", str(pr.number), "-R", repo, "--body-file", body_file],
            repo=repo,
            expect_json=False,
        )

    def acknowledge(self, repo: str, comment: Comment) -> bool:
        """React 👀, returning whether the reaction landed.

        Best-effort by contract: the acknowledgement is a courtesy so the
        reviewer sees something inside the tick, and failing to leave it must
        never stop the request being answered. A review summary has no reaction
        endpoint at all, which is a False rather than an error.
        """
        if comment.kind == "issue":
            path = f"repos/{repo}/issues/comments/{comment.numeric_id}/reactions"
        elif comment.kind == "review_comment":
            path = f"repos/{repo}/pulls/comments/{comment.numeric_id}/reactions"
        else:
            return False
        try:
            self._call(
                ["api", "-X", "POST", path, "-f", "content=eyes"],
                repo=repo,
                expect_json=False,
            )
        except ForgeError as error:
            # Expected through the credential proxy, which refuses mutating
            # `gh api` calls as github.api-mutation -- the same rule that stops
            # the agent merging its own pull request. The reaction is a
            # courtesy and the request is answered without it, so this is a
            # accepted loss rather than a failure, but it is logged because a
            # silent one leaves nobody able to explain a missing eyes emoji.
            # Note the proxy also logs its refusal at WARNING, so a brokered
            # sweep emits a SECURITY_POLICY_BLOCKED line per acknowledgement.
            LOGGER.info("acknowledgement reaction not left on %s: %s", path, error)
            return False
        return True

    def list_commits(self, repo: str, pr: PullRequest) -> list[Commit]:
        """Every commit on the pull request, tip last, each with its date.

        Exists so a reply that claims to have amended the branch can be checked
        against the branch before it is posted. The head sha alone is not
        enough: an amend that made two commits leaves the model naming the one
        it wrote about, which is real and is not the tip.

        The committer date rather than the author date, because a rebase or a
        cherry-pick preserves the author date of a commit written weeks ago.
        The question being asked is "did this land on the branch after the
        request", and the committer date is the one that answers it.
        """
        rows = self._call(
            [
                "api",
                f"repos/{repo}/pulls/{pr.number}/commits?per_page={PR_PAGE_SIZE}",
                "--paginate",
            ],
            repo=repo,
            retry_transient=True,
        )
        commits = []
        for row in rows or []:
            sha = str(row.get("sha", ""))
            if not sha:
                continue
            committer = ((row.get("commit") or {}).get("committer")) or {}
            commits.append(Commit(sha=sha, committed_at=str(committer.get("date", ""))))
        return commits

    def conflict_state(self, repo: str, pr: PullRequest) -> Optional[bool]:
        """Does this branch conflict with its base? None when the forge cannot say.

        The single-pull-request endpoint, because the list endpoint carries no
        `mergeable` at all — GitHub computes the merge only when a pull request
        is asked about by itself. That makes this one extra round trip per
        agent-owned pull request per tick, which is the price of the answer.

        Three-valued for the reason `_has_write` is. The computation is lazy and
        asynchronous: the first read after a push to either branch gets
        `mergeable: null` and `mergeable_state: "unknown"` while a background job
        works it out. Collapsing that to "no conflict" would make the sweep blind
        for exactly the tick after every push — the tick where a conflict is most
        likely to have just appeared — and collapsing it to "conflict" would send
        a worker to merge a branch that merges fine. The caller waits instead;
        the next tick is ten minutes away and the answer is cached by then.

        `mergeable` and `mergeable_state` are both read because either alone has
        a gap: `mergeable_state` carries values (`blocked`, `behind`, `unstable`)
        that are not conflicts and must not read as one, and `mergeable` is the
        field GitHub documents as authoritative for the merge itself.
        """
        data = (
            self._call(
                ["api", f"repos/{repo}/pulls/{pr.number}"],
                repo=repo,
                retry_transient=True,
            )
            or {}
        )
        mergeable = data.get("mergeable")
        state = str(data.get("mergeable_state") or "").strip().lower()
        if mergeable is None or not state or state == MERGEABLE_STATE_UNKNOWN:
            return None
        return mergeable is False or state == MERGEABLE_STATE_CONFLICTED

    def failing_checks(self, repo: str, pr: PullRequest) -> list[CheckRun]:
        """Red CI on the head commit, from both of GitHub's two registers.

        A check run that has not finished is not a failure, so `status` is
        tested before `conclusion`: a queued or in-progress run has
        `conclusion: null`, which is neither red nor green, and treating the
        absence as red would wake the agent at CI that is still running.

        Empty is a real answer meaning "nothing red", and it is the same answer
        as "no CI configured". The two do not need separating here: neither is
        something for the agent to fix.
        """
        found: list[CheckRun] = []

        runs = self._call(
            [
                "api",
                f"repos/{repo}/commits/{pr.head_sha}/check-runs"
                f"?per_page={CHECK_PAGE_SIZE}",
            ],
            repo=repo,
            retry_transient=True,
        )
        for row in (runs or {}).get("check_runs") or []:
            if str(row.get("status") or "").strip().lower() != "completed":
                continue
            conclusion = str(row.get("conclusion") or "").strip().lower()
            if conclusion not in FAILING_CONCLUSIONS:
                continue
            found.append(
                CheckRun(
                    name=plain_check_name(str(row.get("name") or "")),
                    conclusion=conclusion,
                    details_url=safe_check_url(row.get("details_url")),
                    register="check_run",
                )
            )

        combined = self._call(
            [
                "api",
                f"repos/{repo}/commits/{pr.head_sha}/status"
                f"?per_page={CHECK_PAGE_SIZE}",
            ],
            repo=repo,
            retry_transient=True,
        )
        for row in (combined or {}).get("statuses") or []:
            state = str(row.get("state") or "").strip().lower()
            if state not in FAILING_STATUS_STATES:
                continue
            found.append(
                CheckRun(
                    name=plain_check_name(str(row.get("context") or "")),
                    conclusion=state,
                    details_url=safe_check_url(row.get("target_url")),
                    register="status",
                )
            )

        return found


#: Host substring -> provider. One entry today; the point of the table is that
#: adding a second is a registration rather than a branch in the sweep.
PROVIDERS: dict[str, type] = {"github.com": GitHubProvider}


def provider_for(repo: Optional[str] = None, **kwargs) -> ForgeProvider:
    """Pick a provider from the host in repo or default to GitHub.

    A bare `owner/repo` — which the operator accepts and writes through
    verbatim — names no host, so it means GitHub: that shorthand is `gh -R`'s
    own form and no other forge shares it.
    """
    lowered = (repo or "").lower()
    for host, cls in PROVIDERS.items():
        if host in lowered:
            return cls(**kwargs)
    return GitHubProvider(**kwargs)
