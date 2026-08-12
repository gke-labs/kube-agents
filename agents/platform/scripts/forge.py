#!/usr/bin/env python3
"""forge.py — the five forge operations this harness needs, behind one seam.

Staged into `$HERMES_HOME/scripts` by the entrypoint's step 2b force-sync, so
every skill script on the Platform Agent's `sys.path` can import it.

What this is for
----------------
Reading and answering a pull-request conversation needs exactly five things from
a code-hosting service: who am I, which pull requests are open, what has been
said on one, say something back, and acknowledge that a request was seen. Those
five are the whole forge-shaped surface of the feature; everything above them —
what counts as addressing the agent, who is allowed to, when a request has
already been answered — is harness policy that does not change between forges.

Splitting the two here is what makes a second forge a new class rather than a
second copy of the sweep. It is *not* a claim that a second forge is cheap:
`docs/designs/pr-comment-conversation.md` §3 lists the four places under this
module — token brokering, the credential sidecar's executable allowlist, git
credential shape, and the CRD — that would each need work first. The seam is
here so that when that work happens it lands in one place.

Why `_call` exists
------------------
Every provider method reaches `gh` through one method. The agent container holds
no GitHub token: `gh` is proxied to the credential sidecar, which is also why
`ALLOWED_EXECUTABLES` is a closed list. Bitbucket has no comparable CLI, so a
Bitbucket provider cannot shell anything at all — it needs a `/v1/<forge>/…`
route on that sidecar. Funnelling every call through one override point means
that provider replaces one method instead of reimplementing five.

Three normalisations, and the forge that forced each
----------------------------------------------------
* **`Comment.can_write` is a boolean, not GitHub's `authorAssociation`.** GitHub
  hands the association over free on every comment. GitLab and Bitbucket have no
  equivalent field and need a members lookup, so the *provider* answers "may this
  account direct the agent?" and the caller never sees a forge's vocabulary.
* **`supports_acknowledge` is a capability, not an assumption.** Bitbucket Cloud
  has no reactions on pull-request comments. A caller that assumed the 👀 would
  either crash there or silently skip it; a flag makes the absence legible.
* **`self_login` strips a trailing `[bot]`.** GitHub's REST and GraphQL APIs
  disagree about whether an App's login carries the suffix — `AGENTS.md` records
  the same discrepancy for `kube-agents-bot`. Comparing an unnormalised login
  against a comment author is how an agent ends up answering itself forever.

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
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, Sequence

SETTINGS_PATH = "/opt/data/SETTINGS.md"

#: Shell convention for "command not found". Kept distinguishable from a `gh`
#: command that ran and failed, because the two need different operators.
GH_MISSING_RC = 127

#: How long any single `gh` call may take. A hung proxy must not hold the cron
#: tick's per-job lock open indefinitely.
GH_TIMEOUT_S = 60

#: `gh` list commands take a limit, not a cursor, and silently truncate. A full
#: page therefore means "there may be more", which callers have to notice.
PR_PAGE_LIMIT = 100

#: The branch prefix `submit_suggestion.check_branch` and
#: `audit_report.group_branch_for` both write. It is how a pull request is known
#: to be the agent's own without asking the forge who authored it — an App's
#: login is not stable across the REST/GraphQL split (see `self_login`).
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

#: `authorAssociation`/`author_association` values that imply write access, and
#: therefore the standing to direct the agent. The same set `audit_report.py`
#: uses to gate `/remediate`; the two must not disagree about who is trusted.
WRITE_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class ForgeError(Exception):
    """A fault with a machine-readable reason code.

    The gate turns `reason` into the `⚠️` line an operator reads, so the codes
    are part of the contract rather than debug text. They match the vocabulary
    `resolver.py handle_poll` already emits, so one operator-facing glossary
    covers both sweeps.
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

    @property
    def is_agent_authored(self) -> bool:
        """Head branch, not author login.

        The author is an App whose login spelling depends on which API answered
        (`self_login`), and on a fork the head ref is still written by us. The
        branch prefix is the one thing both writers of these pull requests —
        `submit_suggestion.py` and `audit_report.py` — control directly.
        """
        return self.head_ref.startswith(AGENT_BRANCH_PREFIX)

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


class ForgeProvider(Protocol):
    """The complete forge-shaped surface of the PR-conversation feature."""

    #: False on a forge with no reaction API (Bitbucket Cloud), so a caller can
    #: skip the acknowledgement rather than discover it fails.
    supports_acknowledge: bool

    def preflight(self) -> None: ...

    def self_login(self, pr: PullRequest) -> str: ...

    def list_open_prs(self, repo: str) -> list[PullRequest]: ...

    def list_comments(self, repo: str, pr: PullRequest) -> list[Comment]: ...

    def post_comment(self, repo: str, pr: PullRequest, body_file: str) -> None: ...

    def acknowledge(self, repo: str, comment: Comment) -> bool: ...


def normalise_login(login: str) -> str:
    """Strip a trailing `[bot]` and lowercase. See the module docstring."""
    text = str(login or "").strip()
    if text.endswith("[bot]"):
        text = text[: -len("[bot]")]
    return text.lower()


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


def target_repo(settings_path: Optional[str] = None) -> Optional[str]:
    """The configured repository as `owner/name`, or None when there is none.

    None means "nothing configured", which is a supported install. A configured
    value that cannot be read raises instead, because those two must never
    reach an operator as the same silence.
    """
    path = settings_path or SETTINGS_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    configured = None
    for line in lines:
        if "Git Repo:" in line:
            configured = line.split("Git Repo:", 1)[1].replace("*", "").strip()
            break

    if not configured or configured.lower() == SETTINGS_REPO_UNSET:
        return None
    return _parse_repo(configured)


def run_gh(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """One `gh` invocation, never raising for a non-zero exit.

    Callers here always need the reason code more than the exception: a token
    without scope for this repository and a repository that 404s both exit
    non-zero with usable stderr, and turning that into a traceback loses it.
    A missing binary is reported as `GH_MISSING_RC` so it stays distinguishable
    from a command that ran and failed.
    """
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
            1,
            stdout="",
            stderr=f"'gh' timed out after {GH_TIMEOUT_S}s.",
        )


def gh_preflight(run: Callable[[Sequence[str]], subprocess.CompletedProcess] = run_gh):
    """Raise `ForgeError` when `gh` cannot authenticate at all.

    Passing means only that *some* host is authenticated — a token without scope
    for the target repository still fails later, which is why every list call
    below reports `REPO_UNREACHABLE` on its own rather than trusting this.
    """
    result = run(["auth", "status"])
    if result.returncode == 0:
        return
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
        self._run = run or run_gh

    # -- the seam ---------------------------------------------------------
    def _call(self, argv: Sequence[str], *, expect_json: bool = True):
        """Every forge round trip goes through here. See the module docstring.

        Returns parsed JSON, or None for a call made only for its effect. A
        non-zero exit raises `REPO_UNREACHABLE`, which is the honest reading of
        a `gh` failure that survived the preflight: the credential works
        somewhere, just not here.
        """
        result = self._run(list(argv))
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

    # -- the five operations ----------------------------------------------
    def preflight(self) -> None:
        """`gh_preflight` against *this* provider's runner.

        A method rather than a bare call so a caller holding a provider never
        has to reach past it to the module-level default — which under test
        would be the real `gh`.
        """
        gh_preflight(self._run)

    def self_login(self, pr: PullRequest) -> str:
        """The agent's own handle, taken from a pull request it authored.

        Deriving it from the data rather than from configuration is what makes
        the mention trigger work with nothing to set up, and it cannot drift
        from whatever account actually opened the pull request. It is only
        meaningful for an agent-authored PR, which is the only scope the sweep
        has.
        """
        return normalise_login(pr.author)

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        rows = self._call(
            [
                "pr", "list", "-R", repo,
                "--state", "open",
                "--json", "number,headRefName,labels,author,url",
                "--limit", str(PR_PAGE_LIMIT),
            ]
        )
        prs = [
            PullRequest(
                number=int(row.get("number", 0)),
                head_ref=str(row.get("headRefName", "")),
                author=str((row.get("author") or {}).get("login", "")),
                labels=tuple(
                    str(label.get("name", "")) for label in (row.get("labels") or [])
                ),
                url=str(row.get("url", "")),
            )
            for row in (rows or [])
        ]
        if len(prs) >= PR_PAGE_LIMIT:
            # `gh` truncates silently. Saying so beats a sweep that quietly
            # stops seeing the oldest open pull requests.
            raise ForgeError("PR_PAGE_TRUNCATED", str(PR_PAGE_LIMIT))
        return prs

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
                f"repos/{repo}/issues/{pr.number}/comments", kind="issue"
            )
        )
        out.extend(
            self._collect(
                f"repos/{repo}/pulls/{pr.number}/comments", kind="review_comment"
            )
        )
        out.extend(
            self._collect(f"repos/{repo}/pulls/{pr.number}/reviews", kind="review")
        )
        # Oldest first: the cap takes the oldest unanswered triggers, so a
        # request must not be starved by newer ones arriving in the same tick.
        out.sort(key=lambda c: (c.created_at, c.node_id))
        return out

    def _collect(self, path: str, *, kind: str) -> Iterable[Comment]:
        rows = self._call(["api", path, "--paginate"]) or []
        for row in rows:
            body = str(row.get("body") or "")
            # A review with no summary body is an approval or a state change,
            # not an utterance. Keeping it would give the marker scan an empty
            # comment to match nothing against on every tick.
            if kind == "review" and not body.strip():
                continue
            association = str(row.get("author_association") or "").upper()
            yield Comment(
                node_id=str(row.get("node_id") or ""),
                numeric_id=int(row.get("id") or 0),
                author=str((row.get("user") or {}).get("login", "")),
                body=body,
                can_write=association in WRITE_ASSOCIATIONS,
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
                ["api", "-X", "POST", path, "-f", "content=eyes"], expect_json=False
            )
        except ForgeError:
            return False
        return True


#: Host substring -> provider. One entry today; the point of the table is that
#: adding a second is a registration rather than a branch in the sweep.
PROVIDERS: dict[str, type] = {"github.com": GitHubProvider}


def provider_for(settings_path: Optional[str] = None, **kwargs) -> ForgeProvider:
    """Pick a provider from the host in SETTINGS.md's `Git Repo:` line.

    A bare `owner/repo` — which the operator accepts and writes through
    verbatim — names no host, so it means GitHub: that shorthand is `gh -R`'s
    own form and no other forge shares it.
    """
    path = settings_path or SETTINGS_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = ""
    lowered = text.lower()
    for host, cls in PROVIDERS.items():
        if host in lowered:
            return cls(**kwargs)
    return GitHubProvider(**kwargs)
