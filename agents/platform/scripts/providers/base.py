#!/usr/bin/env python3
"""What a forge is, expressed as the ten things that differ between forges.

Less differs than it looks. Cloning, bundling, the publish safety checks, the
scratch lifecycle, the size ceilings and every subprocess belong to the broker
and are the same everywhere. A forge decides which hostnames are its own, what
a URL of its names, where to clone from, which of the collaboration verbs it
serves, how its credential is acquired and presented, which transport reaches
its API, how many instances of itself this install has -- and, for each verb it
serves, what to ask for and how to translate the answer.

Two members are worth reading twice, because they are the ones a
single-forge design would not have.

`transport` is a *declaration*, not an implementation. The forge names what it
needs and the broker constructs it. That keeps the rule that a forge says what
to call and never how to execute it, while allowing a transport that is not a
subprocess -- which is what an interface shaped as `api_command() -> argv`
would have ruled out without ever saying so.

`for_config` is what lets the registry stay ignorant of any particular forge.
Of a hosted forge an install has exactly one or none; of a self-managed one it
may have four, at hostnames chosen by whoever runs them. Asking the class how
many of itself to build is the only version of that question that does not put
a hostname in a shared file.

What a forge may **not** do, in any implementation:

- run a subprocess, or choose a working directory for one
- choose a scratch path, or set a timeout, or bypass a size ceiling
- reach the network except through the transport the broker built for it

Those are not style rules. On `main` a single executor is the one place that
applies the executable allowlist, the argv refusal list, `GIT_ALLOW_PROTOCOL`,
the forced git config, `GIT_EDITOR=false`, and the timeout and output ceilings.
A forge that ran its own subprocess would be a second path past all of them,
and the controls would still look present in the file that no longer decides.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from workspace_paths import WorkspaceError

from .credentials import Credential, NoCredential
from .errors import Override
from .identity import SEGMENT_RE, path_segments

# Re-exported so a forge package can refuse a request without importing outside
# the shared contract. `WorkspaceError` is the broker's "the caller sent
# something wrong" with an HTTP status attached; a forge raises it for anything
# specific to its own request shapes that the shared validators cannot know.
__all__ = [
    "BROKER_VERBS",
    "COLLABORATION_VERBS",
    "Forge",
    "ForgeUnsupported",
    "StubForge",
    "WorkspaceError",
    "listing",
]

# The eight verbs a forge may serve, plus the three the broker serves for every
# forge. Spelled with hyphens because that is how they appear in a route and in
# the `verbs` list a caller reads back from `capabilities`.
COLLABORATION_VERBS: tuple[str, ...] = (
    "proposal-create",
    "proposal-list",
    "proposal-view",
    "proposal-comment",
    "issue-create",
    "issue-list",
    "issue-view",
    "issue-comment",
)

BROKER_VERBS: tuple[str, ...] = ("capabilities", "clone", "publish")


class ForgeUnsupported(WorkspaceError):
    """This host is not one this install has a credential and a client for."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=501, code="FORGE_UNSUPPORTED")


def listing(
    items: list[dict], limit: int, key: str, returned: int | None = None
) -> dict[str, Any]:
    """A page of results that says when it is a page rather than the answer.

    `returned` is how many the forge sent, which is not always how many come
    out. A forge whose issue endpoint also carries change proposals is filtered
    after the page is fetched, and a full page filtered down to three is still a
    page: judging `truncated` on what survived would tell the caller it has
    everything while the forge is holding more. It defaults to the length of
    `items`, which is right for every verb that filters nothing.
    """
    fetched = len(items) if returned is None else returned
    return {key: items, "count": len(items), "truncated": fetched >= limit}


class Forge:
    """The interface. See the module docstring for what is not on it."""

    # Which hostnames are this forge's. Also what the credential allowlist is
    # built from: a host with no entry is refused before a token is spent.
    name = "abstract"
    hosts: tuple[str, ...] = ()
    # What this forge calls a change proposal, for messages the caller reads.
    proposal_noun = "change proposal"
    # Which of the collaboration verbs this forge serves. A forge that serves
    # all of them says so; one with no issue tracker omits four and gets a
    # named refusal for free.
    verbs: tuple[str, ...] = ()
    # "cli" or "http". A declaration; the broker builds the thing. `cli` is the
    # second half of the same declaration and is meaningful only for the first:
    # the binary this forge's API is reached through. The broker reads it twice
    # -- once to construct the transport, once to derive which executables the
    # credentialed process is allowed to run at all -- so an install with no
    # CLI-backed forge grants no forge CLI.
    transport = "http"
    cli = ""
    # The few statuses whose shared guidance this forge disagrees with.
    error_overrides: Mapping[int, Override] = {}

    def __init__(self) -> None:
        self.credential: Credential = NoCredential()

    # -- registration -------------------------------------------------------

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable["Forge"]:
        """Every instance of this forge that `config` describes.

        Zero, one, or several. The registry calls this on each registered class
        and concatenates the results, so a forge that is not configured
        contributes nothing without the registry knowing it exists.
        """
        raise NotImplementedError

    # -- identity -----------------------------------------------------------

    def parse(self, url: str) -> str:
        """The repository this URL names, in whatever form `clone_url` wants.

        Also the only validator of that repository's shape. Everything
        downstream -- the clone URL, every API path -- is composed from what
        this returns, so a `parse` that accepts a segment containing a slash or
        a leading dash has handed the caller a say in an argv.
        """
        raise NotImplementedError

    def clone_url(self, repo: str) -> str:
        """The URL to clone, composed from validated segments.

        Never the caller's URL. The caller's URL decided *which forge*; it does
        not get to decide the host a credential is presented to.
        """
        raise NotImplementedError

    def capabilities(self, repo: str) -> dict[str, Any]:
        """What this install can do here, before anything is spent.

        No credential, no network. A caller that discovers the gap by failing
        halfway through a publish has already written the revision it cannot
        deliver.
        """
        return {
            "forge": self.name,
            "repo": repo,
            "proposalNoun": self.proposal_noun,
            "verbs": sorted({*BROKER_VERBS, *self.verbs}),
            "missing": [],
        }

    # -- the eight verbs ----------------------------------------------------
    #
    # Each takes the transport's `api` callable, the parsed repository, and the
    # caller's payload; each returns the normalised shape for its concept.
    #
    # A forge that does not serve one leaves it off `verbs` and does not
    # implement it. What it inherits is a refusal naming the verb, not a
    # `NotImplementedError`: an agent that asked for something this install
    # cannot do should get an answer it can report and route around, and a
    # traceback in the credentialed process is not one. The refusal is here
    # rather than in a check the broker runs first so that a forge which can
    # say something more specific -- what exactly is missing, and why -- says
    # it instead, without the broker having to ask.

    def _unsupported(self, verb: str) -> dict[str, Any]:
        raise ForgeUnsupported(
            f"{self.name} does not serve `{verb}` in this install. "
            "`capabilities` lists what it does serve."
        )

    def proposal_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("proposal-create")

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("proposal-list")

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("proposal-view")

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("proposal-comment")

    def issue_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("issue-create")

    def issue_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("issue-list")

    def issue_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("issue-view")

    def issue_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        return self._unsupported("issue-comment")


class StubForge(Forge):
    """A host this install recognises and cannot yet serve.

    Present rather than absent on purpose. A caller asking about a host with no
    configured credential gets the gap named -- which is an answer it can
    report and act on -- where falling through to some other forge would answer
    the question with "that is not a valid repository for a forge you did not
    ask about", which is not.

    It still parses. Naming the repository back in the refusal is what tells a
    caller its URL was understood and the install is what is missing.
    """

    def __init__(
        self,
        name: str,
        hosts: tuple[str, ...],
        proposal_noun: str,
        missing: Iterable[str],
        segments: int = 2,
    ) -> None:
        super().__init__()
        self.name = name
        self.hosts = hosts
        self.proposal_noun = proposal_noun
        self.missing = list(missing)
        self._segments = segments

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable["Forge"]:
        # Stubs are constructed by the registry from what is *not* configured,
        # never discovered from configuration.
        return ()

    def parse(self, url: str) -> str:
        parts = path_segments(url, self.hosts)
        if len(parts) < self._segments or not all(
            SEGMENT_RE.match(part) for part in parts
        ):
            raise WorkspaceError(f"{url!r} is not a {self.name} repository")
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        raise ForgeUnsupported(f"{self.name}: {self.missing[0]}")

    def capabilities(self, repo: str) -> dict[str, Any]:
        return {
            "forge": self.name,
            "repo": repo,
            "proposalNoun": self.proposal_noun,
            "verbs": [],
            "missing": list(self.missing),
        }

    def _refuse(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ForgeUnsupported(f"{self.name}: {self.missing[-1]}")

    proposal_create = proposal_list = proposal_view = proposal_comment = _refuse
    issue_create = issue_list = issue_view = issue_comment = _refuse
