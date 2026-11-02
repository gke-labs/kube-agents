#!/usr/bin/env python3
"""The readings sweep: the install's own proposals, read from the forge on a timer.

One responsibility: every interval, read each managed repository's proposals
that the install authored, and the events other accounts made on them, and hand
two readings to the metrics object. The outcomes reading is the count of those
proposals by state (step 1, the listing). The details reading is who closed the
closed ones, the touches on all of them by kind and actor, how many human
touches each terminal proposal carried, and how many old open proposals nobody
has touched (step 2, per-proposal reads behind a cache and an hourly budget).

Each reading is all or nothing over the repositories that require it: a
repository that fails a step withholds the reading that step feeds, and the
metrics object keeps the previous one. Nothing here serves a request, and no
answer the broker gives depends on the cache this keeps.
`docs/designs/proposal-outcome-readings.md` sections 4 and 5 are the design.
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from providers import ForgeUnsupported, IdentityUnknown, MAX_PAGE_SIZE, WorkspaceError
from providers.validate import MAX_CONVERSATION_SIZE
from vcs_broker import _login_key

LOGGER = logging.getLogger("credential-proxy.vcs-readings")

# A managed key is `provider:host/path` (`credential_proxy._repository_key`).
# The registry resolves the `host/path` part; the provider names the forge
# the entry was registered as, and no other forge serves it.
MANAGED_KEY_SEPARATOR = ":"

# The sweep's timing and bounds (design section 5).
VCS_READINGS_TTL_SECONDS = 240
# Read by the metrics object: past this age a reading's series are omitted.
VCS_READINGS_STALE_SECONDS = 3600
# The listing walk's bound in pages, for a forge that serves full pages; on
# GitHub it is also the search API's ceiling of a thousand results.
VCS_READINGS_MAX_PAGES = 10
# The walk's bound in proposals, for a server that clamps the page size (Gitea's
# `MAX_RESPONSE_ITEMS`), where a page bound would mean fewer proposals.
VCS_READINGS_MAX_PROPOSALS_GITEA = 1000
# Pages past the proposal bound's own page count a clamped walk may take, read
# at the smallest page seen: room for a repeat or two from a listing that
# shifted mid-walk, and a hard ceiling for a server that never reaches its total.
VCS_READINGS_CLAMPED_PAGE_SLACK = 2
# Step 2's forge calls per hour: a fifth of an installation token's quota,
# spread evenly over the sweeps in the hour.
VCS_READINGS_DETAIL_CALLS_PER_HOUR = 1000
VCS_READINGS_IGNORED_AFTER_SECONDS = 14 * 24 * 3600
# The per-proposal gauge's cumulative `le` edges.
VCS_READINGS_BUCKETS = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, math.inf)
# The most commits any forge serves for one proposal; the forges cap there too.
# For a forge class that declares no `COMMIT_CAP` of its own; GitHub, GitLab
# and Gitea each declare theirs, and the sweep reads the bound forge's.
COMMIT_CAP_FALLBACK = 250
SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60
# GitHub serves thirty searches a minute; the sweep keeps under that, so a
# large install stretches the sweep rather than having its tail rate-limited.
VCS_READINGS_SEARCH_CALLS_PER_MINUTE = 25
# How many more times a listing that came back short of the forge's total is
# walked before the repository's outcomes reading is withheld.
VCS_READINGS_SHORT_LISTING_RETRIES = 1
METHOD_GET = "GET"

# The series the metrics object renders from these readings.
VCS_PROPOSALS_METRIC = "kubeagents_vcs_proposals"
VCS_PROPOSALS_CLOSED_BY_METRIC = "kubeagents_vcs_proposals_closed_by"
VCS_PROPOSAL_TOUCHES_METRIC = "kubeagents_vcs_proposal_touches"
VCS_PROPOSAL_TOUCHES_PER_PROPOSAL_METRIC = "kubeagents_vcs_proposal_touches_per_proposal"
VCS_PROPOSALS_IGNORED_METRIC = "kubeagents_vcs_proposals_ignored"
VCS_READINGS_AGE_METRIC = "kubeagents_vcs_readings_age_seconds"
VCS_READINGS_FAILURES_METRIC = "kubeagents_vcs_readings_failures_total"

# The closed label vocabularies.
STATE_OPEN = "open"
STATE_MERGED = "merged"
STATE_CLOSED = "closed"
STATES = (STATE_OPEN, STATE_MERGED, STATE_CLOSED)
OUTCOMES = (STATE_MERGED, STATE_CLOSED)
ACTOR_INSTALL = "install"
ACTOR_HUMAN = "human"
ACTOR_BOT = "bot"
CLOSERS = (ACTOR_INSTALL, ACTOR_HUMAN, ACTOR_BOT)
KIND_REVIEW_APPROVED = "review_approved"
KIND_REVIEW_CHANGES_REQUESTED = "review_changes_requested"
KIND_REVIEW_COMMENTED = "review_commented"
KIND_REVIEW_COMMENT = "review_comment"
KIND_COMMENT = "comment"
KIND_COMMIT = "commit"
KIND_AGENT_ADDRESS = "agent_address"
READING_OUTCOMES = "outcomes"
READING_DETAILS = "details"
REASON_FORGE = "forge"
REASON_IDENTITY = "identity"
REASON_TRUNCATED = "truncated"
REASON_DETAIL_TRUNCATED = "detail_truncated"
REASON_RATE_LIMITED = "rate_limited"
REASON_WARMING = "warming"

# A provider review state to its touch kind. `dismissed` has no kind: the
# vocabulary is closed, and a dismissed review is no longer the reviewer's say.
REVIEW_KINDS = {
    "approved": KIND_REVIEW_APPROVED,
    "changes_requested": KIND_REVIEW_CHANGES_REQUESTED,
    "commented": KIND_REVIEW_COMMENTED,
}
# A conversation item's `kind` to its touch kind. `review` items are left out:
# reviews are read from `proposal_reviews`, and one with a body would count twice.
COMMENT_KIND_ISSUE = "issue"
COMMENT_KINDS = {COMMENT_KIND_ISSUE: KIND_COMMENT, KIND_REVIEW_COMMENT: KIND_REVIEW_COMMENT}
EVENT_REVIEW = "review"
EVENT_COMMENT = "comment"
EVENT_COMMIT = "commit"
# The form the `pr_comments` sweep accepts as a person steering the agent.
AGENT_ADDRESS_PREFIX = "/agent"

# The install's git author addresses, none of which a forge links to an
# account, so its commits are told by email (design section 4).
INSTALL_COMMIT_EMAILS = ("platform-agent@kube-agents.invalid", "kube-agents@users.noreply.invalid")
INSTALL_COMMIT_EMAIL_ENVS = ("CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL", "KUBE_AGENTS_VCS_AUTHOR_EMAIL")
WORKSPACE_NOREPLY_EMAIL = "platform-agent@users.noreply.{host}"
APP_NOREPLY_EMAIL = "{app_id}+{slug}[bot]@users.noreply.github.com"
GITHUB_APP_ID_ENV = "GITHUB_APP_ID"
# An App installation's login carries this marking, and the forge's author
# filter names the App as `app/<slug>`.
AUTOMATION_SUFFIX = "[bot]"
APP_AUTHOR = "app/{slug}"
# The field of the current-user answer that holds the account's numeric id,
# which a forge that filters by poster id needs.
USER_ID_FIELD = "id"
RATE_LIMITED_CODE = "FORGE_RATE_LIMITED"
THREAD_NAME = "vcs-readings"
UTC_SUFFIX = "Z"
UTC_OFFSET = "+00:00"


@dataclass(frozen=True)
class Touch:
    """One kind of event by one class of actor; frozen so it keys a Counter."""

    kind: str
    actor: str


@dataclass
class ProposalRecord:
    """What step 2 read of one proposal, cached until its `updated` moves."""

    number: int
    state: str
    updated: str
    created: str
    touches: Counter = field(default_factory=Counter)
    closer: str = ""  # "install" | "human" | "bot" | ""


@dataclass
class OutcomesReading:
    open: int
    merged: int
    closed: int
    taken_at: float


@dataclass
class DetailsReading:
    closed_by: dict[str, int]
    touches: dict[Touch, int]
    # Outcome, then `le` edge, cumulative, `inf` last.
    buckets: dict[str, dict[float, int]]
    ignored: int
    taken_at: float


def install_commit_emails(host: str, app_login: str, app_id: str | None) -> frozenset[str]:
    """Every git author address the install's own commits carry, casefolded."""
    emails = set(INSTALL_COMMIT_EMAILS)
    for name in INSTALL_COMMIT_EMAIL_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            emails.add(value)
    if host:
        emails.add(WORKSPACE_NOREPLY_EMAIL.format(host=host))
    slug = _login_key(app_login)
    if app_id and slug:
        emails.add(APP_NOREPLY_EMAIL.format(app_id=app_id, slug=slug))
    return frozenset(email.casefold() for email in emails)


def classify_touch(event: dict, install_login: str, install_emails: frozenset[str]) -> Touch | None:
    """The touch one event is, or None for the install's own or an uncounted one.

    The install's own is a login match (`_login_key`), or, for a commit, a git
    author email among `install_emails`, since a forge links none of the
    install's addresses to its account.
    """
    author = str(event.get("author") or "")
    install = _login_key(install_login)
    if author and install and _login_key(author) == install:
        return None
    kind = None
    kind_of_event = event.get("type")
    if kind_of_event == EVENT_REVIEW:
        kind = REVIEW_KINDS.get(event.get("state"))
    elif kind_of_event == EVENT_COMMENT:
        kind = COMMENT_KINDS.get(event.get("kind"))
        body = str(event.get("body") or "").lstrip()
        if kind == KIND_COMMENT and body.startswith(AGENT_ADDRESS_PREFIX):
            kind = KIND_AGENT_ADDRESS
    elif kind_of_event == EVENT_COMMIT:
        if str(event.get("email") or "").strip().casefold() in install_emails:
            return None
        kind = KIND_COMMIT
    if kind is None:
        return None
    return Touch(kind, ACTOR_BOT if event.get("bot") else ACTOR_HUMAN)


def bucket_counts(touch_totals: list[int], edges: Iterable[float] = VCS_READINGS_BUCKETS) -> dict[float, int]:
    """Cumulative counts: for each edge, how many totals are at most it."""
    return {edge: sum(1 for total in touch_totals if total <= edge) for edge in edges}


def _human_touches(record: ProposalRecord) -> int:
    return sum(count for touch, count in record.touches.items() if touch.actor == ACTOR_HUMAN)


def _epoch(stamp: str) -> float | None:
    """An ISO-8601 timestamp as seconds since the epoch, or None."""
    if not stamp:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(stamp.replace(UTC_SUFFIX, UTC_OFFSET))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _reason(exc: BaseException) -> str:
    if isinstance(exc, WorkspaceError) and exc.fields.get("code") == RATE_LIMITED_CODE:
        return REASON_RATE_LIMITED
    return REASON_FORGE


class _ForeignAuthor(Exception):
    """The forge returned a proposal another account authored."""


class _ShortListing(Exception):
    """A complete-looking listing held fewer proposals than the forge counted."""


class _MalformedListing(Exception):
    """A listed proposal carried no usable number, so the listing cannot be read whole."""


@dataclass
class _Repository:
    """One managed repository bound for one sweep."""

    key: str
    forge: Any
    path: str
    host: str
    transport: Any
    api: Callable[..., Any]

    @property
    def scope(self) -> tuple[str, str]:
        return (self.host, self.path.casefold())


def _host(forge: Any) -> str:
    return forge.hosts[0] if forge.hosts else forge.name


@dataclass
class _Attempt:
    """What one repository's attempt gave each reading."""

    counts: Counter | None = None  # None withholds the outcomes reading
    details: bool = False  # False withholds the details reading
    # Every failure the attempt produced, in order: a repository past the walk
    # bound that is still warming records both `truncated` and `warming`.
    reasons: list[str] = field(default_factory=list)
    nameless: bool = False


class ReadingsSweep:
    """The timer, the per-repository steps, the detail cache and its budget."""

    def __init__(
        self,
        *,
        registry: Any,
        transport_for: Callable[[Any, str], Any],
        metrics: Any,
        managed_repositories: Callable[[], frozenset[str]],
        clock: Callable[[], float] = time.monotonic,
        interval: float = VCS_READINGS_TTL_SECONDS,
        app_credential: Callable[[Any], bool] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._registry = registry
        self._transport_for = transport_for
        # Whether a forge's credential is a GitHub App installation token,
        # which the forge's author filter names as `app/<slug>`. The primary
        # signal; the login's `[bot]` marking is the fallback, since the REST
        # and GraphQL APIs disagree on whether an App login carries it.
        self._app_credential = app_credential
        self._metrics = metrics
        self._managed = managed_repositories
        self._clock = clock
        self._interval = interval
        # (forge host, casefolded repository, number) -> what step 2 read.
        self._cache: dict[tuple[str, str, int], ProposalRecord] = {}
        # Per managed repository, for the process: the login, the numeric id
        # where the forge filters by it, and the install's commit addresses.
        self._logins: dict[str, str] = {}
        self._author_ids: dict[str, Any] = {}
        self._emails: dict[str, frozenset[str]] = {}
        self._nameless: set[str] = set()
        self._nameless_logged = False
        # Step 2's token bucket, in forge calls: refilled at the hourly rate,
        # holding at most one sweep's share so the hour's calls spread evenly.
        self._capacity = VCS_READINGS_DETAIL_CALLS_PER_HOUR * interval / SECONDS_PER_HOUR
        self._budget = self._capacity
        self._refilled_at = clock()
        self._calls = 0
        self._bound: dict[str, _Repository] = {}
        self._running = threading.Lock()
        self._stop = threading.Event()
        # The search calls' pacing: per forge host, when each of the last
        # minute's searches went out, and how the sweep waits for the next.
        self._searches: dict[str, deque] = {}
        self._sleep = sleep if sleep is not None else self._stop.wait

    # ---- the timer -----------------------------------------------------

    def start(self) -> threading.Thread:
        """A daemon thread running one attempt per interval, a failed one included."""
        thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 -- the timer outlives any one attempt
                LOGGER.exception("proposal readings sweep failed")
            self._stop.wait(self._interval)

    def run_once(self) -> None:
        """One attempt over every managed repository; returns at once if one is running."""
        if not self._running.acquire(blocking=False):
            return
        try:
            self._sweep()
        finally:
            self._running.release()

    # ---- one sweep -----------------------------------------------------

    def _sweep(self) -> None:
        try:
            managed = sorted(self._managed())
        except Exception as exc:  # noqa: BLE001 -- an unreadable list is no sweep
            LOGGER.warning("proposal readings skipped: managed repositories unreadable type=%s", type(exc).__name__)
            return
        if not managed:
            return
        self._refill()
        self._bound = {}
        totals: Counter = Counter()
        outcomes_whole = details_whole = True
        scopes: set[tuple[str, str]] = set()
        required = 0
        for key in managed:
            if key in self._nameless:
                continue
            attempt = self._attempt(key)
            if attempt.nameless:
                self._nameless.add(key)
                self._metrics.record_reading_failure(REASON_IDENTITY)
                self._log_nameless(key)
                continue
            required += 1
            for reason in attempt.reasons:
                self._metrics.record_reading_failure(reason)
            if attempt.counts is None:
                outcomes_whole = False
            else:
                totals.update(attempt.counts)
            if attempt.details:
                scopes.add(self._bound[key].scope)
            else:
                details_whole = False
        self._evict_unmanaged(managed)
        if not required:
            return
        now = time.time()
        outcomes = None
        if outcomes_whole:
            outcomes = OutcomesReading(totals[STATE_OPEN], totals[STATE_MERGED], totals[STATE_CLOSED], now)
        details = self._details_reading(scopes, now) if details_whole else None
        if outcomes is not None or details is not None:
            self._metrics.set_readings(outcomes, details)

    def _attempt(self, key: str) -> _Attempt:
        try:
            self._bind(key)
            if not self._login(key):
                return _Attempt(nameless=True)
            proposals, truncated, _ = self._list(key)
        except _ForeignAuthor:
            LOGGER.warning("proposal readings: %s listed a proposal another account authored", key)
            return _Attempt(reasons=[REASON_FORGE])
        except _ShortListing:
            LOGGER.warning("proposal readings: %s listed fewer proposals than the forge counted", key)
            return _Attempt(reasons=[REASON_FORGE])
        except _MalformedListing:
            LOGGER.warning("proposal readings: %s listed a proposal with no usable number", key)
            return _Attempt(reasons=[REASON_FORGE])
        except Exception as exc:  # noqa: BLE001 -- one repository never ends the sweep
            return self._failed(key, exc, _Attempt())
        attempt = _Attempt(reasons=[REASON_TRUNCATED] if truncated else [])
        if truncated:
            # A tally that fails withholds the outcomes reading alone: the
            # listing is whole enough for the details, which go on below.
            try:
                attempt.counts = self._tally(key)
            except Exception as exc:  # noqa: BLE001
                self._failed(key, exc, attempt)
        else:
            attempt.counts = Counter(p.get("state") for p in proposals if p.get("state") in STATES)
        try:
            if not truncated:
                self._evict_missing(key, proposals)
            if not self._warm(key, proposals):
                attempt.reasons.append(REASON_WARMING)
                return attempt
        except Exception as exc:  # noqa: BLE001
            return self._failed(key, exc, attempt)
        attempt.details = True
        return attempt

    @staticmethod
    def _failed(key: str, exc: Exception, attempt: _Attempt) -> _Attempt:
        reason = _reason(exc)
        attempt.reasons.append(reason)
        LOGGER.warning(
            "proposal readings: %s failed reason=%s type=%s code=%s",
            key,
            reason,
            type(exc).__name__,
            exc.fields.get("code") if isinstance(exc, WorkspaceError) else "",
        )
        return attempt

    def _log_nameless(self, key: str) -> None:
        if self._nameless_logged:
            return
        self._nameless_logged = True
        LOGGER.info(
            "proposal readings are off for %s: the credential cannot name itself, "
            "so no proposal can be told to be this install's own",
            key,
        )

    def _resolve(self, key: str) -> tuple[Any, str]:
        """The forge and path a managed key names.

        The registry takes `host/path`, so the key's `provider:` prefix comes off
        first; handed whole, the registry would read the provider as a host and
        refuse it. A key whose provider is not the forge serving its host admits
        nothing, as every other gate on the managed list holds.
        """
        provider, separator, locator = key.partition(MANAGED_KEY_SEPARATOR)
        if not separator:
            provider, locator = "", key
        forge, path = self._registry.resolve(locator)
        if provider and forge.name != provider:
            raise ForgeUnsupported(
                f"{key} is registered as {provider!r}, and {_host(forge)} is served by {forge.name!r}"
            )
        return forge, path

    def _bind(self, key: str) -> _Repository:
        forge, path = self._resolve(key)
        transport = self._transport_for(forge, path)
        host = _host(forge)
        paced = getattr(forge, "search_routes", frozenset())

        def api(method: str, route: str, **kwargs: Any) -> Any:
            if route in paced:
                self._pace(host)
            self._calls += 1
            return transport.api(method, route, **kwargs)

        bound = _Repository(key, forge, path, host, transport, api)
        self._bound[key] = bound
        return bound

    def _pace(self, host: str) -> None:
        """Wait until a search to `host` keeps the last minute within the rate."""
        sent = self._searches.setdefault(host, deque())
        now = self._clock()
        while sent and now - sent[0] >= SECONDS_PER_MINUTE:
            sent.popleft()
        if len(sent) >= VCS_READINGS_SEARCH_CALLS_PER_MINUTE:
            self._sleep(SECONDS_PER_MINUTE - (now - sent[0]))
            now = self._clock()
            while sent and now - sent[0] >= SECONDS_PER_MINUTE:
                sent.popleft()
        sent.append(now)

    def _refill(self) -> None:
        now = self._clock()
        rate = VCS_READINGS_DETAIL_CALLS_PER_HOUR / SECONDS_PER_HOUR
        self._budget = min(self._capacity, self._budget + (now - self._refilled_at) * rate)
        self._refilled_at = now

    # ---- the per-repository steps --------------------------------------

    def _login(self, repo: str) -> str:
        """The credential's login, from the sweep's own transport, once per process.

        "" for a credential that cannot name itself, which a forge says with an
        empty answer or `IdentityUnknown`/`ForgeUnsupported`; remembered, so the
        repository is not attempted again. Any other failure is an outage and
        raises, unremembered, so the next sweep asks again.
        """
        if repo in self._logins:
            return self._logins[repo]
        bound = self._bound[repo]
        try:
            login = (bound.transport.whoami() or "").strip()
        except (IdentityUnknown, ForgeUnsupported):
            login = ""
        if login and bound.forge.whoami_route and not bound.forge.whoami_graphql:
            # A forge whose author filter takes the account's numeric id.
            answer = bound.api(METHOD_GET, bound.forge.whoami_route[0])
            self._author_ids[repo] = answer.get(USER_ID_FIELD) if isinstance(answer, dict) else None
        self._logins[repo] = login
        return login

    def _author_payload(self, repo: str) -> dict[str, Any]:
        login = self._logins[repo]
        author = login
        forge = self._bound[repo].forge
        app = self._app_credential is not None and self._app_credential(forge)
        if app or login.endswith(AUTOMATION_SUFFIX):
            author = APP_AUTHOR.format(slug=login.removesuffix(AUTOMATION_SUFFIX))
        payload: dict[str, Any] = {"author": author}
        if self._author_ids.get(repo) is not None:
            payload["authorId"] = self._author_ids[repo]
        return payload

    def _list(self, repo: str) -> tuple[list[dict], bool, int | None]:
        """Step 1: the install's proposals, newest activity first, and whether
        the walk stopped at its bound with more remaining, and the forge's total.

        A walk that ends inside the bound must hold every proposal the forge
        counted: one short of the total (a proposal that moved to a page already
        read, or a page the forge marked incomplete) is walked again, and short
        again raises, so no short listing is ever published as complete.
        """
        for _ in range(1 + VCS_READINGS_SHORT_LISTING_RETRIES):
            read, truncated, total, short = self._walk(repo)
            if not short:
                return read, truncated, total
        raise _ShortListing()

    def _walk(self, repo: str) -> tuple[list[dict], bool, int | None, bool]:
        """One listing walk: what `_list` returns, and whether a walk that
        ended inside its bound is short of the forge's count.

        The bound is in pages while pages come back full and in proposals once
        one comes back shorter than asked with more remaining, which is a server
        clamping the page size; a clamped walk also stops at a page ceiling
        read from the smallest page seen. The walk goes on while the forge's
        total says more remain, whatever a page's `truncated` says, since a
        clamping server can make a page look like the last; a page that adds
        no proposal or is marked incomplete ends it short. Any item by another
        author raises.
        """
        bound = self._bound[repo]
        install = _login_key(self._logins[repo])
        payload = self._author_payload(repo)
        payload["limit"] = MAX_PAGE_SIZE
        read: list[dict] = []
        seen: set[Any] = set()
        clamped = False
        smallest = MAX_PAGE_SIZE
        total: int | None = None
        page = 1
        while True:
            answer = bound.forge.proposal_list_by_author(bound.api, bound.path, {**payload, "page": page})
            batch = [item for item in answer.get("proposals") or [] if isinstance(item, dict)]
            before = len(read)
            for item in batch:
                if _login_key(str(item.get("author") or "")) != install:
                    raise _ForeignAuthor()
                if not isinstance(item.get("number"), int) or isinstance(item.get("number"), bool):
                    raise _MalformedListing()
                if item.get("number") not in seen:
                    seen.add(item.get("number"))
                    read.append(item)
            if isinstance(answer.get("total"), int):
                total = answer["total"]
            if answer.get("incomplete") or (batch and len(read) == before):
                # A page the forge marked incomplete, or one that added no
                # proposal (a server that ignores `page`): the walk cannot
                # finish whole, so it ends here, short.
                return read, False, total, True
            exhausted = total is not None and len(read) >= total
            more = bool(answer.get("truncated")) or (total is not None and len(read) < total)
            if not batch or not more or exhausted:
                short = total is not None and len(read) != total
                return read, False, total, short
            if len(batch) < MAX_PAGE_SIZE:
                clamped = True
                smallest = min(smallest, len(batch))
            if clamped:
                ceiling = -(-VCS_READINGS_MAX_PROPOSALS_GITEA // smallest) + VCS_READINGS_CLAMPED_PAGE_SLACK
                bounded = len(read) >= VCS_READINGS_MAX_PROPOSALS_GITEA or page >= ceiling
            else:
                bounded = page >= VCS_READINGS_MAX_PAGES
            if bounded:
                return read, True, total, False
            page += 1

    def _tally(self, repo: str) -> Counter | None:
        """State counts for a repository past the walk bound, as the forge's
        tally gives them (`tally_with_open` derives open from the one count on a
        forge that counts the states separately). None when the forge cannot
        split them, which withholds the outcomes reading."""
        bound = self._bound[repo]
        answer = bound.forge.proposal_tally(bound.api, bound.path, self._author_payload(repo))
        counts = Counter()
        for state in STATES:
            value = answer.get(state)
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            counts[state] = value
        return counts

    def _warm(self, repo: str, proposals: list[dict]) -> bool:
        """Step 2 for every listed proposal not current in the cache, under the
        budget; True when every one is current. Each record is cached as read."""
        bound = self._bound[repo]
        current = True
        for proposal in proposals:
            key = (*bound.scope, int(proposal.get("number")))
            record = self._cache.get(key)
            if (
                record is not None
                and record.updated == proposal.get("updated")
                and record.state == proposal.get("state")
            ):
                continue
            if self._budget <= 0:
                current = False
                continue
            before = self._calls
            try:
                self._cache[key] = self._details(repo, proposal)
            finally:
                self._budget -= self._calls - before
        return current

    def _details(self, repo: str, proposal: dict) -> ProposalRecord:
        """Step 2 for one proposal: its touches by kind and actor, and its closer."""
        bound = self._bound[repo]
        login = self._logins[repo]
        emails = self._install_emails(repo)
        number = int(proposal.get("number"))
        state = str(proposal.get("state") or "")
        events: list[dict] = []

        reviews = bound.forge.proposal_reviews(
            bound.api, bound.path, {"number": number, "limit": MAX_CONVERSATION_SIZE}
        )
        truncated = bool(reviews.get("truncated"))
        events += [{**item, "type": EVENT_REVIEW} for item in reviews.get("reviews") or []]

        view = bound.forge.proposal_view(
            bound.api, bound.path, {"number": number, "comments": True, "limit": MAX_CONVERSATION_SIZE}
        )
        truncated = truncated or bool(view.get("commentsTruncated"))
        events += [
            {**item, "type": EVENT_COMMENT}
            for item in view.get("comments") or []
            if item.get("kind") in COMMENT_KINDS
        ]

        commits, capped = self._commits(bound, number)
        events += [{**item, "type": EVENT_COMMIT} for item in commits]

        touches: Counter = Counter()
        for event in events:
            touch = classify_touch(event, login, emails)
            if touch is not None:
                touches[touch] += 1
        closer = self._closer(bound, number, login) if state == STATE_CLOSED else ""
        if truncated or capped:
            self._metrics.record_reading_failure(REASON_DETAIL_TRUNCATED)
        return ProposalRecord(
            number=number,
            state=state,
            updated=str(proposal.get("updated") or ""),
            created=str(proposal.get("created") or ""),
            touches=touches,
            closer=closer,
        )

    @staticmethod
    def _commits(bound: _Repository, number: int) -> tuple[list[dict], bool]:
        """A proposal's commits up to the cap, and whether the cap was reached."""
        rows: list[dict] = []
        page = 1
        cap = getattr(bound.forge, "COMMIT_CAP", COMMIT_CAP_FALLBACK)
        while len(rows) < cap:
            answer = bound.forge.proposal_commits(
                bound.api, bound.path, {"number": number, "limit": MAX_PAGE_SIZE, "page": page}
            )
            batch = answer.get("commits") or []
            rows.extend(batch)
            if len(batch) < MAX_PAGE_SIZE:
                break
            page += 1
        rows = rows[:cap]
        return rows, len(rows) >= cap

    @staticmethod
    def _closer(bound: _Repository, number: int, login: str) -> str:
        answer = bound.forge.proposal_closer(bound.api, bound.path, {"number": number})
        actor = str(answer.get("actor") or "")
        if not actor:
            return ""
        if _login_key(actor) == _login_key(login):
            return ACTOR_INSTALL
        return ACTOR_BOT if answer.get("bot") else ACTOR_HUMAN

    def _install_emails(self, repo: str) -> frozenset[str]:
        if repo not in self._emails:
            self._emails[repo] = install_commit_emails(
                self._bound[repo].host,
                self._logins[repo],
                os.environ.get(GITHUB_APP_ID_ENV, "").strip() or None,
            )
        return self._emails[repo]

    # ---- the cache -----------------------------------------------------

    def _evict_missing(self, repo: str, proposals: list[dict]) -> None:
        """A repository within the walk bound: a proposal absent from its
        complete listing was deleted. Past the bound nothing is evicted, since
        absent may mean fallen off the end."""
        scope = self._bound[repo].scope
        listed = {int(p.get("number")) for p in proposals}
        for key in [k for k in self._cache if k[:2] == scope and k[2] not in listed]:
            del self._cache[key]

    def _evict_unmanaged(self, managed: list[str]) -> None:
        """A repository no longer managed takes its proposals out of the readings.

        Judged against the managed list, not against the repositories bound this
        sweep: a bind that failed (a mint outage in `transport_for`) leaves the
        repository managed, and its records, the fallen-off ones of a truncated
        repository among them, stay for the life of the process.
        """
        live: set[tuple[str, str]] = set()
        for key in managed:
            try:
                forge, path = self._resolve(key)
            except Exception:  # noqa: BLE001 -- a host no forge serves holds no records
                continue
            live.add((_host(forge), path.casefold()))
        for key in [k for k in self._cache if k[:2] not in live]:
            del self._cache[key]

    def _details_reading(self, scopes: set[tuple[str, str]], now: float) -> DetailsReading:
        closed_by = dict.fromkeys(CLOSERS, 0)
        touches: Counter = Counter()
        totals: dict[str, list[int]] = {outcome: [] for outcome in OUTCOMES}
        ignored = 0
        for key, record in self._cache.items():
            if key[:2] not in scopes:
                continue
            touches.update(record.touches)
            human = _human_touches(record)
            if record.state in totals:
                totals[record.state].append(human)
            if record.state == STATE_CLOSED and record.closer in closed_by:
                closed_by[record.closer] += 1
            created = _epoch(record.created)
            if (
                record.state == STATE_OPEN
                and not human
                and created is not None
                and now - created > VCS_READINGS_IGNORED_AFTER_SECONDS
            ):
                ignored += 1
        return DetailsReading(
            closed_by=closed_by,
            touches=dict(touches),
            buckets={outcome: bucket_counts(totals[outcome]) for outcome in OUTCOMES},
            ignored=ignored,
            taken_at=now,
        )
