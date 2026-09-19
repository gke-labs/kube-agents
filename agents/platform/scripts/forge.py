#!/usr/bin/env python3
"""forge.py — the pull-request conversation's view of a forge.

Staged into `$HERMES_HOME/scripts` by the entrypoint's step 2b force-sync, so
every skill script on the Platform Agent's `sys.path` can import it.

What this is for
----------------
Reading and answering a pull-request conversation needs a handful of things
from a code-hosting service, and `ForgeProvider` below is the whole list: who
am I, which change proposals are open, what has been said on one, what has
landed on one, say something back, and acknowledge that a request was seen.
Those are the whole forge-shaped surface of the feature; everything above
them — what counts as addressing the agent, who is allowed to, when a request
has already been answered — is harness policy that does not change between
forges, and it lives here rather than in a provider so that it is written once
instead of once per forge.

Where the forge went
--------------------
Nowhere in this file. Every method below is a version-control verb sent to the
credential broker through `vcs_client`, and the answers arrive already
translated into shapes no forge's vocabulary appears in. This module used to
hold a `GitHubProvider` that shelled `gh` — a REST client with its own retry
policy, its own status-code parsing, its own collaborator-permission lookup and
its own view of what `[bot]` means. All of that exists on the broker side too,
which made it a second GitHub implementation: two places to add the next forge
to, and two answers to every question the third one asks.
`docs/designs/version-control-support.md` §4 "One provider implementation, not
two" is the decision that this one converges rather than standing beside it.

What is left is the half the broker cannot supply across a JSON boundary:
typed values instead of raw dicts, the three policy rules — `is_ignored`,
`is_bot`, `is_agent_pull_request` — that decide which of the proposals a forge
reports are this agent's business, and the hop that gets a call from the pod
this runs in to the pod that holds the credential. That last one is `call` and
`main` at the bottom of the file: the sweep runs in the agent pod, which has no
`CREDENTIAL_PROXY_URL` by design, so each verb is answered by a copy of this
same module inside the shell sandbox. It replaces one ssh hop per `gh`
invocation with one per verb, which is fewer.

Three normalisations, and the forge that forced each
----------------------------------------------------
* **`Comment.can_write` is a boolean, not a forge's membership vocabulary.**
  The question asked is "may this account direct the agent?" and the caller
  never sees how the forge spells the answer. It is the `identity` verb with a
  `login`, cached per account for the tick; GitHub's per-comment
  `authorAssociation` looks free and is not usable, because it is reported
  relative to what the authenticated viewer can see and an App installation
  token cannot see organisation membership — a repository admin comes back
  `CONTRIBUTOR` under this credential, observed live.
* **`supports_acknowledge` is a capability, not an assumption.** Bitbucket
  Cloud has no reactions on pull-request comments. It is answered from the
  repository's `capabilities`, which the broker computes with no token and no
  network, so asking costs nothing and the absence is legible rather than
  discovered as a failure.
* **`normalise_login` strips a trailing `[bot]` and a leading `app/`.** The
  broker strips the suffix on everything it translates, but not every login
  this module compares came from the broker: an @-mention a human types is
  plain text out of a comment body. Comparing an unnormalised login against a
  comment author is how an agent ends up answering itself forever.

On the repository parser
------------------------
There is no longer one here. `repo_ref.py` parses every repository value this
harness sees, and this module is one of its callers — see `provider_for`, which
uses it only to tell a misconfigured repository from an unreachable one before
spending a round trip.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Protocol

import repo_ref
import sandbox_exec
import vcs_client

LOGGER = logging.getLogger(__name__)

#: How many proposals, comments or commits one read asks for. The verbs cap a
#: page at 100 and report `truncated` rather than silently cutting the list, so
#: this is the ceiling rather than a first page — see `BrokerProvider._page`,
#: which turns a truncated answer into a log line instead of letting a partial
#: read look like a complete one.
PAGE_SIZE = 100

#: The branch prefix the agent's own pull requests carry. Only one place writes
#: it in code — `audit_report.group_branch_for` — and `submit-suggestion`'s
#: SKILL.md instructs the model to use it, which `submit_suggestion.check_branch`
#: does not enforce: that function rejects an empty or protected branch name and
#: nothing else. So this is a convention held up by a prompt, which is exactly
#: why a prefix alone is not enough to call a pull request the agent's — see
#: `is_agent_pull_request` for the three checks that actually carry the weight.
AGENT_BRANCH_PREFIX = "platform-agent/"

#: A label that opts a pull request out of every sweep, matching the convention
#: `github-issue-resolver` already honours on issues.
IGNORE_LABEL = "agent:ignore"

#: Reason code for a repository whose host no forge module serves. Named here
#: beside the other operator-facing strings rather than inline in
#: `UnknownForgeHost`, the way `RepoUnparseable` takes
#: `repo_ref.REASON_UNPARSEABLE`. `_forge_warning` renders it verbatim.
REASON_HOST_UNSUPPORTED = "FORGE_HOST_UNSUPPORTED"

#: The broker's refusal for a repository on a host it has no module for. It is
#: the answer a fallback-to-GitHub would have hidden, so it is mapped to this
#: module's own reason code rather than passed through as a generic failure.
BROKER_UNSUPPORTED = "FORGE_UNSUPPORTED"

#: What a failed verb is called when the broker named no code. The honest
#: reading of a call that reached the broker and came back wrong, and the code
#: this module used to raise for every failure alike.
REASON_UNREACHABLE = "REPO_UNREACHABLE"

#: Where this same file lands in the shell sandbox, in the copy the model cannot
#: reach. There are two: `/opt/data/scripts/forge.py` is the agent's own, on a
#: volume uid 1000 owns and may rewrite, and it is the path the skills name; this
#: one is staged root-owned, mode 0755, by `deploy/sandbox/Dockerfile`. The
#: distinction matters because of who runs it. `_forward` below connects as
#: `hermes` — the trusted login, holding this pod's credential path — so running
#: the agent's copy would execute whatever the model last wrote there against
#: that credential. The Dockerfile stages the whole import closure beside it and
#: fails the build if the import can be satisfied any other way.
SANDBOX_FORGE = "/opt/vcs/libexec/platform/forge.py"

#: Bounds the ssh hop around a forwarded verb. The broker has its own ceiling on
#: the work inside it; this is that plus room for the connection.
FORWARD_TIMEOUT_S = 90

#: A conversation that does not fit one page. Its own code because it is not a
#: fault anywhere -- the forge answered, the credential worked, the pull request
#: is fine -- and an operator reading `REPO_UNREACHABLE` would go looking in the
#: wrong three places. What it means is that this thread has outgrown what one
#: verb call can read, and the sweep is holding rather than guessing.
REASON_CONVERSATION_TRUNCATED = "CONVERSATION_TRUNCATED"

#: The verb never ran: ssh could not connect, or the hop timed out. Its own code
#: rather than `REPO_UNREACHABLE`, because the two send an operator to different
#: places — one to the sandbox, one to the forge — and `resolver.py` already
#: reports the same distinction under the same name.
REASON_SANDBOX_UNREACHABLE = "SANDBOX_UNREACHABLE"

#: The refusals worth one more attempt, and only these. Both are the broker's
#: own reading of a failure on the forge's side of the call, and both say so in
#: the guidance it returns: `FORGE_UNAVAILABLE` is "wait a few minutes and retry
#: the same call unchanged", `FORGE_CALL_FAILED` is "one retry is reasonable;
#: two is not". Everything else is definitive or needs a wait this module has no
#: business taking inside a ten-minute tick — `FORGE_RATE_LIMITED` in particular
#: gets worse when it is retried at once, and `FORGE_CONFLICT` says to re-read
#: before trying again, which is a decision for the caller.
#:
#: `SANDBOX_UNREACHABLE` is deliberately not here. It is the transport, not the
#: forge, and the timeout it usually means has already spent the budget once.
TRANSIENT_CODES = frozenset({"FORGE_UNAVAILABLE", "FORGE_CALL_FAILED"})

#: How much of a refusal's detail reaches the operator warning. The warnings go
#: into a Chat card, and a forge that answers a rejected write with a page of
#: JSON would otherwise push everything else in the card out of sight.
MAX_DETAIL_CHARS = 200


class ForgeError(Exception):
    """A fault with a machine-readable reason code.

    The gate turns `reason` into the `⚠️` line an operator reads, so the codes
    are part of the contract rather than debug text. Most of them are now the
    broker's own refusal codes, which is the same vocabulary `resolver.py
    handle_poll` reports — one operator-facing glossary covers both sweeps
    because both are quoting the same source.
    """

    def __init__(self, reason: str, value: str = ""):
        super().__init__(f"{reason}: {value}" if value else reason)
        self.reason = reason
        self.value = value


class RepoUnparseable(ForgeError):
    """A registered repository value that could not be understood.

    Distinct from absent on purpose. Configuring nothing is a supported install
    with no work to do; configuring something unreadable is a fault, and
    silence there means the watcher stops working and nobody finds out.

    The parse itself lives in `repo_ref`, which raises a plain `ValueError`
    because the credential sidecar imports it and must not import this module.
    This is where that becomes a reason code an operator sees.
    """

    def __init__(self, value: str):
        super().__init__(repo_ref.REASON_UNPARSEABLE, value)


class UnknownForgeHost(ForgeError):
    """A repository on a host this harness has no forge module for.

    Reported rather than fallen back from. The fallback is what would send
    GitHub calls on behalf of a GitLab URL, at a same-named repository
    belonging to somebody else. The broker is where the decision is made — it
    holds the host table — and this is that refusal given the reason code an
    operator reads.
    """

    def __init__(self, host: str):
        super().__init__(REASON_HOST_UNSUPPORTED, host)


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_ref: str
    author: str
    labels: tuple[str, ...] = ()
    url: str = ""
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

    `ref` rather than `numeric_id` is the identity used in answered-markers.
    The number is unique only within the endpoint that issued it — a
    conversation comment and a review comment can share one, which would let an
    answer to either suppress the other — so `ref` is the number and the kind
    together, which is also exactly the pair `acknowledge` takes back.
    """

    ref: str
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
    #: Routes the reaction, which has a different path per kind and none at all
    #: for a review.
    kind: str = "issue"
    numeric_id: int = 0
    path: str = ""
    line: Optional[int] = None
    #: Whether the author is an automation rather than a person, as the forge
    #: said and not as the login spells it. It cannot be read off `author`: the
    #: forge module strips the `[bot]` suffix from every login it translates,
    #: because a caller comparing one against an @-mention a human typed has to
    #: compare the same thing. `pr_triggers.is_addressable_bot` is what this
    #: feeds, and it is what stops two agents answering each other forever.
    is_bot: bool = False


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


class ForgeProvider(Protocol):
    """The complete forge-shaped surface of the PR-conversation feature."""

    def viewer_login(self, repo: str) -> str: ...

    def supports_acknowledge(self, repo: str) -> bool: ...

    def list_open_prs(self, repo: str) -> list[PullRequest]: ...

    def list_comments(self, repo: str, pr: PullRequest) -> list[Comment]: ...

    def post_comment(self, repo: str, pr: PullRequest, body: str) -> None: ...

    def acknowledge(self, repo: str, pr: PullRequest, comment: Comment) -> bool: ...

    def list_commits(self, repo: str, pr: PullRequest) -> list[Commit]: ...

    def truncations(self) -> list[str]: ...


def normalise_login(login: str) -> str:
    """Reduce every spelling of one account to a single key.

    A forge can give an App several logins for the same identity, and this
    sweep sees more than one in a tick:

    * REST comment authors       → `kube-agents[bot]`
    * an @-mention a human types → `kube-agents`
    * some list endpoints        → `app/kube-agents`

    Both affixes are stripped, because the comparison this feeds is what stops
    the agent answering itself. Matching `app/x` against `x[bot]` fails, no
    marker the agent wrote is ever recognised as its own, and every tick
    re-answers the same comment — observed live before this was normalised.

    The broker already strips a trailing `[bot]` from every login it
    translates, which does not make this redundant: the mention spelling comes
    out of a comment body as plain text and reaches no translator at all.
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
      carries the bare branch name in `head_ref`, so anybody who can fork this
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
    is refused. So does an empty `head_repo`, which is what a forge reports once
    the fork a proposal came from has been deleted: the branch is then nobody's
    that anyone can name, and the agent's own is something it must be able to
    name before it pushes to it.
    """
    if not viewer or not pr.head_repo:
        return False
    return (
        normalise_login(pr.author) == normalise_login(viewer)
        and pr.head_ref.startswith(AGENT_BRANCH_PREFIX)
        and pr.head_repo.lower() == repo.lower()
    )


@contextmanager
def _as_forge_error(repo: str):
    """Re-raise a broker refusal in the vocabulary this module's callers catch.

    The reason code is the broker's own wherever it gave one. That is a gain
    rather than a translation cost: the single `REPO_UNREACHABLE` this used to
    raise for every non-zero `gh` exit covered a dead credential, an absent
    repository and a rate limit alike, and the operator warning it produced
    could not tell an operator which of the three to go and fix. The codes are
    the ones `github-issue-resolver` already reports, so the two sweeps now
    need one glossary between them rather than one each.
    """
    try:
        yield
    except vcs_client.VcsError as refusal:
        detail = (refusal.detail or str(refusal)).strip()[:MAX_DETAIL_CHARS]
        if refusal.code == BROKER_UNSUPPORTED:
            raise UnknownForgeHost(_host_of(repo) or repo) from refusal
        raise ForgeError(refusal.code or REASON_UNREACHABLE, detail) from refusal


def _host_of(repo: str) -> str:
    """The host named in a repository value, or "" when it names none."""
    try:
        return repo_ref.parse(repo).host or ""
    except repo_ref.RepoRefError:
        return ""


def call(verb: str, payload: dict, repo: str, *, retry_transient: bool = False) -> dict:
    """One version-control verb against one repository, wherever this is running.

    Every forge call in this module and its consumers goes through here, so
    there is one place that knows how the broker is reached and one exception
    type — `ForgeError`, with the broker's own reason code — above it.

    **The hop.** `vcs_client` reaches the broker over `CREDENTIAL_PROXY_URL`,
    and the agent pod does not have one. That is deliberate and permanent: the
    split that moved the model's shell into the sandbox gave the gateway the
    chat relays and the sandbox the credential proxy, so the pod holding the
    model's API keys holds no forge credential. `github_scan_gate.py` runs in
    the agent pod on a ten-minute tick, so its sweep has to cross.

    **Once per verb, not once per sweep.** `resolver.py` crosses once, carrying
    its whole subcommand, and argues for that in `_forward_to_sandbox` — but it
    is one self-contained answer with one JSON envelope, and this is a library
    six call sites in three consumers share, several of which interleave forge
    reads with decisions taken from the agent pod's own state. Crossing per verb
    is what those callers can use unchanged, and it costs strictly fewer hops
    than the `gh` calls it replaces: one per verb where there was one per `gh`
    invocation, and reading a conversation was three of those.

    When no sandbox is configured this calls the broker directly, which is both
    the install that turned the sandbox off — the call then fails saying there
    is no broker, which is the honest report — and the normal case for a skill
    script the model runs, since that is already across the boundary.

    **`retry_transient` is for reads, and the callers set it, not this.** One
    more attempt on a failure the broker itself calls retryable
    (`TRANSIENT_CODES`) is what the `gh`-era `_call(..., retry_transient=True)`
    gave the three read paths, and losing it silently made a single 502 on
    `proposal-list` enough to skip every repository for a tick and post a
    "watcher is not running" card. It is off by default because a write is not
    safe to repeat: `proposal-comment` that failed after the forge accepted it
    would post the reviewer's answer twice, and there is no idempotency key on
    that route to make the second call a no-op.
    """
    attempts = 2 if retry_transient else 1
    for attempt in range(1, attempts + 1):
        try:
            with _as_forge_error(repo):
                if sandbox_exec.sandbox_enabled():
                    return _forward(verb, payload, repo)
                return vcs_client.forge(verb, dict(payload), repository=repo)
        except ForgeError as error:
            if attempt == attempts or error.reason not in TRANSIENT_CODES:
                raise
            LOGGER.warning(
                "%s on %s failed with %s; retrying once", verb, repo, error.reason
            )
    raise AssertionError("unreachable")  # pragma: no cover -- the loop returns or raises


def _forward(verb: str, payload: dict, repo: str) -> dict:
    """Ask a copy of this file inside the sandbox, and read its answer back.

    The payload travels as JSON on fd 0 rather than as arguments. A comment body
    is a reviewer's own words quoted back at them and runs to thousands of
    characters, and there is no path both ends can see — so it is neither a
    command line nor a file.

    A refusal is carried across as a refusal and re-raised here with its code
    intact. Flattening it into "the far side exited 1" would lose exactly the
    distinction the callers act on: `FORGE_RATE_LIMITED` is a wait,
    `FORGE_NOT_FOUND` is a misconfiguration, and an operator is sent to a
    different place by each.

    The forwarded process cannot forward again. `sandbox_enabled()` reads the
    agent pod's managed Hermes config, and the sandbox image does not carry it.
    """
    try:
        completed = sandbox_exec.run(
            ["python3", SANDBOX_FORGE, "call", verb, "--repository", repo],
            timeout=FORWARD_TIMEOUT_S,
            stdin=json.dumps(payload),
        )
    except sandbox_exec.SandboxUnavailable as unreachable:
        # The verb never ran. Reported as its own code rather than as a forge
        # refusal, because a sweep that reads "the repository is quiet" out of a
        # transport that was down is the one answer this path must not produce.
        raise vcs_client.VcsError(
            str(unreachable), code=REASON_SANDBOX_UNREACHABLE
        ) from unreachable
    except subprocess.TimeoutExpired as expired:
        raise vcs_client.VcsError(
            f"{verb} on {repo} did not answer within {FORWARD_TIMEOUT_S}s",
            code=REASON_SANDBOX_UNREACHABLE,
        ) from expired
    if completed.returncode != 0:
        # `main` answers a refusal with exit 0 and a `refusal` key, so a
        # non-zero exit is the far side itself failing: no interpreter, no file,
        # an unhandled fault. stderr is where that is legible.
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise vcs_client.VcsError(
            f"{SANDBOX_FORGE} exited {completed.returncode}: "
            f"{detail[-1] if detail else 'no output'}"
        )
    try:
        answer = json.loads(completed.stdout or "")
    except ValueError as exc:
        raise vcs_client.VcsError(
            f"{verb} on {repo} answered with something that is not JSON: {exc}"
        ) from exc
    refusal = answer.get("refusal")
    if refusal:
        raise vcs_client.VcsError(
            str(refusal.get("message") or f"{verb} was refused"),
            code=refusal.get("code"),
            detail=refusal.get("detail"),
        )
    return answer.get("answer") or {}


def main(argv: Optional[list[str]] = None) -> int:
    """Answer one forwarded verb. This is the far side of `_forward`.

    Reached only as a subprocess inside the sandbox; nothing a person or a
    model runs comes through here. It exists so the forwarding hop needs no
    second file to keep in step with this one — the copy of this module already
    in the sandbox is the thing that answers.

    A refusal exits 0. It is an answer, and the caller reads its code off the
    payload; making it an exit status would leave the code nowhere to go.
    """
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("command", choices=["call"])
    parser.add_argument("verb", help="the version-control verb to send")
    parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except ValueError as exc:
        sys.stderr.write(f"forge.py: unreadable payload on stdin: {exc}\n")
        return 2

    try:
        answer = vcs_client.forge(args.verb, payload, repository=args.repository)
    except vcs_client.VcsError as refusal:
        print(json.dumps({"refusal": {
            "message": str(refusal), "code": refusal.code, "detail": refusal.detail,
        }}))
        return 0
    print(json.dumps({"answer": answer}))
    return 0


class BrokerProvider:
    """`ForgeProvider` over the credential broker's version-control verbs.

    One class, every forge. Which forge a repository is on is decided from its
    host on the credential side, and nothing here branches on the answer — that
    is the whole reason this is no longer called `GitHubProvider`.

    Everything cached is cached for the life of the instance, which the sweep
    builds fresh on each tick. Nothing here is a cache across ticks: a
    collaborator can be added between two of them, and a sweep that remembered
    yesterday's answer would refuse them for as long as the process lived.
    """

    def __init__(self) -> None:
        # Keyed on (repository, login), because writing is a permission on a
        # repository and not a property of the account. None is a cached
        # "nothing answered", which is not the same as False -- see `_has_write`.
        self._permissions: dict[tuple[str, str], Optional[bool]] = {}
        # Per repository for the same reason the credential is: two forges in
        # one install authenticate as two different accounts. "" is a real
        # answer, meaning the credential could not name itself, and is cached.
        self._viewers: dict[str, str] = {}
        self._acknowledges: dict[str, bool] = {}
        # Every listing this instance read that filled its page, in the words an
        # operator reads. Accumulated rather than only logged because the
        # sweep's channel to a human is the `warnings` list it prints on stdout:
        # `github_scan_gate.py` configures no logging, so a `LOGGER.warning`
        # reaches the cron job's stderr and nothing else.
        self._truncations: list[str] = []

    # -- the seam ----------------------------------------------------------
    def _verb(
        self, verb: str, payload: dict, repo: str, *, retry_transient: bool = False
    ) -> dict:
        return call(verb, payload, repo, retry_transient=retry_transient)

    def _page(self, answer: dict, key: str, repo: str, what: str) -> list:
        """One listing's items, with a truncated page reported rather than hidden.

        A truncated list looks exactly like a complete one, and every reading
        the sweep makes of a complete one is wrong if it is not: a pull request
        past the ceiling is one the agent never answers, and a commit past it
        is one an amendment claim is checked against and does not find. The
        verbs say `truncated` rather than leaving that to be guessed at, and
        this is the one place the flag is read.

        It is recorded as well as logged. `list_comments` refuses outright,
        because a short conversation is read *backwards* rather than merely
        short; these two are read short, which is recoverable -- but only if
        somebody knows it happened, so the note also goes where an operator
        will see it, through `truncations()`.
        """
        if answer.get("truncated"):
            LOGGER.warning(
                "%s on %s filled a page of %d; anything past it is not being "
                "read this tick",
                what,
                repo,
                PAGE_SIZE,
            )
            self._truncations.append(f"{what} on {repo}")
        return answer.get(key) or []

    def truncations(self) -> list[str]:
        """Every listing this instance read short, in the order it read them.

        The sweep drains this into its operator warnings at the end of a tick.
        One list rather than one warning apiece: a repository over the ceiling
        produces the same note on every tick and on more than one listing, and
        an operator reads one line more reliably than five.
        """
        return list(self._truncations)

    # -- identity ----------------------------------------------------------
    def viewer_login(self, repo: str) -> str:
        """The account this credential authenticates as on `repo`'s forge.

        Empty when the forge answered and the answer carried no login. Every
        caller treats that as "sweep nothing" for this repository: this login is
        what separates the agent's own pull requests from a stranger's, and its
        own comments from a reviewer's, so proceeding without it is how the
        agent starts answering itself.

        **A call that did not happen is not an empty login, and raises.** The
        two outcomes send an operator to different places — a credential with no
        readable account is a configuration fault that will not clear on its
        own, while `SANDBOX_UNREACHABLE` is a pod that is down and needs
        nothing — and identity is the first verb a tick sends, so collapsing
        them would report every transport outage as a broken credential. The
        callers put this inside the guard that turns a `ForgeError` into a
        warning carrying its reason code, which is where the distinction
        becomes visible; swallowing it here would leave only a `LOGGER.warning`
        on a cron job's stderr.

        It takes a repository because identity is asked of a forge and the
        repository is how the broker knows which one. There is no install-wide
        viewer to ask for: an install serving two forges authenticates as two
        accounts, and the one that matters is the one that can write here.
        """
        if repo not in self._viewers:
            self._viewers[repo] = self._identity(repo, None)["login"]
        return self._viewers[repo]

    def _identity(self, repo: str, login: Optional[str]) -> dict:
        """One `identity` call, normalised. `login` absent asks about the credential."""
        payload = {"login": login} if login else {}
        answer = self._verb("identity", payload, repo, retry_transient=True)
        identity = answer.get("identity") or {}
        viewer = normalise_login(identity.get("login") or "")
        # The viewer rides along on every `identity` answer, including one
        # asked for somebody else's permission, so a sweep that resolves a
        # commenter first has already learned it and spends no second call.
        self._viewers.setdefault(repo, viewer)
        return {"login": viewer, "canWrite": identity.get("canWrite")}

    def _has_write(self, repo: str, login: str) -> Optional[bool]:
        """May `login` write to `repo`? None when nothing answered.

        A non-member is a definitive no. Any other failure — a proxy fault, a
        timeout, a 5xx — is not an answer, and collapsing the two into one
        `False` is worse than it looks: the sweep answers `False` with a public
        refusal carrying `<!-- agent-refused:… -->`, and that marker is exactly
        what stops the request ever being retried. A five-second network blip
        would refuse a maintainer permanently, and they would have to notice
        and re-comment. So the unknown keeps its own value — the broker answers
        `canWrite: null` for it — and the caller waits for the next tick.
        """
        if not login:
            return False
        # Keyed on the login this actually asks about, not on `normalise_login`,
        # which exists to make a mention match a handle and collapses accounts
        # that are not the same account: it strips a trailing `[bot]` and a
        # leading `app/`, so the App `foo[bot]` and the user `foo` shared one
        # slot and whichever comment arrived first decided trust for both. One
        # direction hands a non-collaborator `can_write=True` and clears the
        # sweep's only trust gate; the other refuses a maintainer and writes a
        # public `agent-refused` marker that `refused_refs` treats as
        # permanent. Neither needs timing luck — permission is resolved for
        # every comment author on every swept pull request, so once both
        # accounts have commented the wrong answer is re-derived every tick.
        #
        # Case is folded because logins are case-insensitive on the forges this
        # runs against, so `Foo` and `foo` are one account and one lookup.
        # Nothing else is folded: the `app/` and bare-name spellings
        # `normalise_login` handles come out of mention text, which never
        # reaches here.
        key = (repo, login.lower())
        if key not in self._permissions:
            try:
                self._permissions[key] = self._identity(repo, login)["canWrite"]
            except ForgeError as error:
                LOGGER.info("permission for %s on %s unreadable: %s", login, repo, error)
                self._permissions[key] = None
        return self._permissions[key]

    # -- capability --------------------------------------------------------
    def supports_acknowledge(self, repo: str) -> bool:
        """Does this repository's forge have somewhere to leave the 👀?

        Read from `capabilities`, which the broker answers from the forge
        module alone — no credential, no network — so asking is cheap enough to
        ask per repository rather than assumed once per install. The flag is
        its own field rather than the presence of `proposal-acknowledge` in
        `verbs`: every forge routes that verb, and one with no reactions
        answers it `{"acknowledged": false}` having done nothing. The verb list
        says the call is accepted; this says it would achieve something.

        A repository whose capabilities cannot be read answers False. The
        acknowledgement is a courtesy and never a reason to stop, so not
        knowing costs the same as not being able to.
        """
        if repo not in self._acknowledges:
            try:
                answer = self._verb("capabilities", {}, repo)
            except ForgeError as error:
                LOGGER.info("capabilities unreadable for %s: %s", repo, error)
                return False
            self._acknowledges[repo] = bool(answer.get("acknowledge"))
        return self._acknowledges[repo]

    # -- the operations ----------------------------------------------------
    def list_open_prs(self, repo: str) -> list[PullRequest]:
        """Every open change proposal on one repository.

        The whole proposal rather than a branch name, because `sourceRepo` is
        the only field that tells a branch in this repository from a same-named
        branch on somebody's fork, and telling those apart is the first of the
        three things `is_agent_pull_request` checks.
        """
        answer = self._verb(
            "proposal-list",
            {"state": "open", "limit": PAGE_SIZE},
            repo,
            retry_transient=True,
        )
        return [
            PullRequest(
                number=int(node.get("number") or 0),
                head_ref=str(node.get("source") or ""),
                # Empty once the fork it came from is deleted, which
                # `is_agent_pull_request` reads as "not ours" rather than
                # assuming it means here.
                head_repo=str(node.get("sourceRepo") or ""),
                head_sha=str(node.get("sourceRevision") or ""),
                author=str(node.get("author") or ""),
                labels=tuple(str(name) for name in (node.get("labels") or [])),
                url=str(node.get("url") or ""),
            )
            for node in self._page(answer, "proposals", repo, "the open proposals")
        ]

    def list_comments(self, repo: str, pr: PullRequest) -> list[Comment]:
        """Every utterance on one pull request, oldest first.

        One read. A forge may split a single human-visible conversation across
        several endpoints — the shipped one splits it across three, and a
        reviewer typing "@agent please fix this" has no idea which they used —
        but which endpoints those are, and how their answers are merged and
        ordered, is the forge module's business and is settled by the time this
        answer arrives.

        The permission lookups are not part of that read. They are one call per
        distinct author, cached for the tick, and they are what turns a list of
        utterances into a list of utterances the agent may act on.

        **A truncated conversation raises rather than returning short.** This is
        the one listing where a partial answer is not merely incomplete, it is
        wrong in the other direction: the caller subtracts the requests it has
        already answered by finding its own markers in this list, so a marker
        past the ceiling reads as a request nobody answered. It would file a
        card, post a duplicate reply to a reviewer who was already answered, and
        do it again on the next tick, because the reply it just wrote lands past
        the ceiling too. Refusing puts the pull request in the sweep's
        `unreadable` list, which names it in an operator warning and leaves the
        thread alone. Losing an answer is recoverable by hand; a comment loop on
        somebody else's review is not.
        """
        answer = self._verb(
            "proposal-view",
            {"number": pr.number, "comments": True, "limit": PAGE_SIZE},
            repo,
            retry_transient=True,
        )
        if answer.get("commentsTruncated"):
            raise ForgeError(
                REASON_CONVERSATION_TRUNCATED,
                f"{repo}#{pr.number} has more comments than one page of "
                f"{PAGE_SIZE} and cannot be read completely",
            )
        out: list[Comment] = []
        for node in answer.get("comments") or []:
            author = str(node.get("author") or "")
            access = self._has_write(repo, author)
            out.append(
                Comment(
                    ref=str(node.get("ref") or ""),
                    numeric_id=int(node.get("id") or 0),
                    author=author,
                    body=str(node.get("body") or ""),
                    can_write=bool(access),
                    can_write_known=access is not None,
                    created_at=str(node.get("created") or ""),
                    kind=str(node.get("kind") or "issue"),
                    path=str(node.get("path") or ""),
                    line=node.get("line"),
                    is_bot=bool(node.get("bot")),
                )
            )
        return out

    def post_comment(self, repo: str, pr: PullRequest, body: str) -> None:
        """Say something back on the conversation.

        The body travels as a field in the request, not on a command line and
        not as a path. There is no path both ends can see — this call is made
        in the agent's pod and the forge is reached from the broker's — and a
        reviewer's own words quoted back to them run to thousands of
        characters, which is not something to put through the quoting rules of
        a shell.
        """
        self._verb("proposal-comment", {"number": pr.number, "body": body}, repo)

    def acknowledge(self, repo: str, pr: PullRequest, comment: Comment) -> bool:
        """React 👀, returning whether the reaction landed.

        Best-effort by contract: the acknowledgement exists so the reviewer
        sees something inside the tick, and failing to leave it must never stop
        the request being answered. A comment kind with no reaction endpoint —
        a review summary — is a False the broker answers without a call.

        The comment is named by `{id, kind}` rather than by `ref`. They carry
        the same two facts, but the pair is what the verb takes, and a `ref`
        split back apart here would be this module re-deriving something it was
        handed.

        The proposal is named too, and the shipped forge does not need it: a
        GitHub reaction endpoint is keyed on the comment alone. It is in the
        verb's request shape because a comment id is not everywhere sufficient
        to locate a comment — the award-emoji route of at least one other forge
        takes the merge request as well — and a verb whose request depended on
        which forge answered it would be the abstraction failing at the first
        thing it was built for. The caller has the proposal in hand, so passing
        it costs nothing.
        """
        try:
            answer = self._verb(
                "proposal-acknowledge",
                {
                    "number": pr.number,
                    "comment": {"id": comment.numeric_id, "kind": comment.kind},
                },
                repo,
            )
        except ForgeError as error:
            # Logged rather than swallowed: a silent failure leaves nobody able
            # to explain a missing eyes emoji. The likeliest cause is a policy
            # refusal upstream — the same rule that stops the agent merging its
            # own pull request also refuses some mutations — rather than
            # anything wrong here, so it is an INFO and not a warning.
            LOGGER.info("acknowledgement not left on %s: %s", comment.ref, error)
            return False
        return bool(answer.get("acknowledged"))

    def list_commits(self, repo: str, pr: PullRequest) -> list[Commit]:
        """Every commit on the pull request, tip last, each with its date.

        Exists so a reply claiming to have amended the branch can be checked
        against the branch before it is posted. The head sha alone is not
        enough: an amend that made two commits leaves the model naming the one
        it wrote about, which is real and is not the tip.

        The committer date rather than the author date, because a rebase or a
        cherry-pick preserves the author date of a commit written weeks ago.
        The question is "did this land on the branch after the request", and
        the committer date is the one that answers it — which is the date the
        verb reports as `committed`.
        """
        answer = self._verb(
            "proposal-commits",
            {"number": pr.number, "limit": PAGE_SIZE},
            repo,
            retry_transient=True,
        )
        commits = []
        for node in self._page(answer, "commits", repo, f"the commits on #{pr.number}"):
            sha = str(node.get("sha") or "")
            # A commit with no sha is not a commit. Dropped rather than carried
            # as an empty one, which would match no claim and read as a forge
            # that lost a commit.
            if sha:
                commits.append(
                    Commit(sha=sha, committed_at=str(node.get("committed") or ""))
                )
        return commits


def provider_for(repo: Optional[str] = None) -> ForgeProvider:
    """A provider, after checking `repo` is at least readable as a repository.

    The parse is local and costs nothing, and it separates two faults an
    operator fixes in different places: a repository value nobody can read is a
    configuration error here, while a host no forge module serves is a refusal
    the broker makes and `_as_forge_error` turns into `UnknownForgeHost`.

    Which forge a readable repository is on is not decided here at all. There
    is no host table on this side of the boundary any more, which is the point
    of the exercise — one of the two had to be the one that knows, and a table
    here would be a second answer to give the third forge.
    """
    if repo:
        try:
            repo_ref.parse(repo)
        except repo_ref.RepoRefError as error:
            raise RepoUnparseable(str(repo)) from error
    return BrokerProvider()


if __name__ == "__main__":
    sys.exit(main())
