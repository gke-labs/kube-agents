#!/usr/bin/env python3
"""repo_ref.py — one repository identity, parsed once and passed.

What this replaces
------------------
`owner/repo` used to be asserted in a handful of Python modules, each with its
own regex and its own way of saying no: some raised, some returned `None`, some
returned `False`. No module could see what another was asserting, so the shapes
drifted — `credential_proxy.is_valid_repository` accepted `acme/..` that every
other copy rejected, and `audit_report` matched the bare regex directly rather
than the validator wrapping it. `docs/designs/multi-forge-support.md` §3 has the
census.

A `RepoRef` carries a host and an opaque path of arbitrary depth. The
two-segment rule is not an invariant of this module: it is a property of
GitHub, checked by the callers that need it (`github_slug` and friends below).
That split is what `docs/designs/multi-forge-support.md` §3 asks for, and it is
why a GitLab `group/subgroup/project` parses here and is refused only where a
GitHub slug is actually required.

Why it imports nothing but the standard library
-----------------------------------------------
`credential_proxy.py` is a caller and runs in the sidecar — the container that
holds the credentials, under a different UID from the agent. A validator on
that boundary must not pull in `gitops_workspace`, which shells `kubectl`.
Standard library only, and no module-level I/O.

On the host
-----------
A host is read only where the syntax states one: a scheme (`https://host/path`)
or an scp-style remote (`[user@]host:path`). A schemeless slash-path has no
host, because inferring one would read `my.org/repo` as a host and a
single-segment path — and `my.org` is a legal owner in the bare form the
operator writes through verbatim. The one exception is `KNOWN_HOSTS`: a
schemeless value whose first segment is a spelling of a forge this harness
knows does name that host, which is what keeps `github.com/owner/repo` working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Upper bound on the whole input, applied before parsing — so it bounds a URL
#: or an scp remote, not only the `owner/name` that comes out. Untrusted input
#: reaches `credential_proxy.is_valid_repository`, and a bound keeps the segment
#: scan linear in something small. 256 is the value that validator already
#: enforced, kept rather than raised: a GitHub clone URL runs to about forty
#: characters and the same URL carrying a fine-grained token to about a hundred
#: and forty, so nothing legitimate is near it.
MAX_REPO_LENGTH = 256

#: One path segment. This character class is the one every validator this
#: module replaces already agreed on; it is matched with `fullmatch` against a
#: single group rather than as adjacent `+` groups around a separator, so it
#: cannot be forced into polynomial backtracking.
SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")

#: scp-like remote syntax — `[user@]host:path` — which is not a URL, so the
#: host has to be split off before it can be compared to anything.
SCP_REMOTE_RE = re.compile(r"^(?:[^/@]+@)?(?P<host>[^/:]+):(?P<path>.+)$")

SCHEME_SEPARATOR = "://"
PATH_SEPARATOR = "/"
GIT_SUFFIX = ".git"

#: Segments that are a filesystem instruction rather than a name. The character
#: class permits "." and "-", so it matches ".." as happily as a real name.
TRAVERSAL_SEGMENTS = frozenset({".", ".."})

#: A leading dash makes `gh -R <slug>` parse the slug as a flag.
FLAG_PREFIX = "-"

#: Every spelling of GitHub that can appear in a remote this install produces.
#: `ssh.github.com` is GitHub's SSH-over-443 endpoint, which shows up in a clone
#: URL. An enterprise host is deliberately absent: Minty issues tokens for
#: github.com installations only.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})

#: The one spelling that appears in a repository *registration*, as opposed to a
#: git remote. `gitops_workspace` accepts only this one, and widening it would
#: change which entries an existing install's state ConfigMap resolves.
GITHUB_CANONICAL_HOST = "github.com"

#: A GitHub project is exactly `owner/name`. This is the per-provider rule §3 of
#: the design moves out of the shared parser, not a property of a repository.
GITHUB_PATH_DEPTH = 2

#: Hosts a schemeless value may name in its first segment — see "On the host".
#: Spelled out rather than aliased to `GITHUB_HOSTS`: this set is the shorthand
#: an operator types, not the set of remotes git can produce, and the two grow
#: for different reasons. Aliasing them would silently extend the schemeless
#: shortcut to every spelling added for a clone URL's sake.
KNOWN_HOSTS = frozenset({GITHUB_CANONICAL_HOST})

#: Matches `forge.RepoUnparseable`, whose reason codes are operator-facing.
REASON_UNPARSEABLE = "GIT_REPO_UNPARSEABLE"


class RepoRefError(ValueError):
    """A value that does not name a repository, with a machine-readable reason.

    A `ValueError` rather than a `forge.ForgeError` because the sidecar imports
    this module and must not import `forge`. `forge` re-raises it as a
    `RepoUnparseable` so the reason code reaching an operator is unchanged.
    """

    def __init__(self, value: object, reason: str = REASON_UNPARSEABLE):
        super().__init__(f"{reason}: {value!r}")
        self.reason = reason
        self.value = value


@dataclass(frozen=True)
class RepoRef:
    """A parsed repository: a host, possibly empty, and a path of any depth."""

    host: str
    path: str

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(self.path.split(PATH_SEPARATOR))

    @property
    def is_github(self) -> bool:
        return self.host in GITHUB_HOSTS

    def __str__(self) -> str:
        return f"{self.host}{PATH_SEPARATOR}{self.path}" if self.host else self.path


def _safe_segment(segment: str) -> bool:
    """One path segment that is a name rather than an instruction."""
    return (
        SEGMENT_RE.fullmatch(segment) is not None
        and segment not in TRAVERSAL_SEGMENTS
        and not segment.startswith(FLAG_PREFIX)
    )


def _trim(path: str) -> str:
    """Drop surrounding slashes and one trailing `.git`, in either order."""
    path = path.strip(PATH_SEPARATOR)
    if path.endswith(GIT_SUFFIX):
        path = path[: -len(GIT_SUFFIX)]
    return path.strip(PATH_SEPARATOR)


def parse(value: object) -> RepoRef:
    """A `RepoRef` from a URL, an scp-style remote, or a bare path.

    Raises `RepoRefError` for anything else, including a path segment that is
    unsafe to hand to a CLI or use as a directory name. Depth is not checked
    here — a three-segment GitLab path is a valid `RepoRef`.
    """
    if not isinstance(value, str):
        raise RepoRefError(value)
    text = value.strip()
    if not text or len(text) > MAX_REPO_LENGTH:
        raise RepoRefError(value)

    if SCHEME_SEPARATOR in text:
        # The host from a real parse, never a substring search: both
        # `https://evil.example/github.com/o/r` and
        # `https://github.com.evil.example/o/r` contain "github.com".
        #
        # `urlsplit` raises a bare `ValueError` on a malformed bracket host --
        # `https://[::1/x`, `http://[abc]:x/a/b`, `https://a]b/c/d`. Converting
        # it here is what makes `try_parse`'s `None` contract true: every
        # caller of this module catches `RepoRefError` and nothing else, so an
        # escaping `ValueError` is a traceback out of a sweep that should have
        # skipped one entry.
        try:
            split = urlsplit(text)
            host, path = (split.hostname or ""), split.path
        except ValueError as error:
            raise RepoRefError(value) from error
    else:
        scp = SCP_REMOTE_RE.match(text)
        host, path = (scp.group("host"), scp.group("path")) if scp else ("", text)

    path = _trim(path)
    if not host and PATH_SEPARATOR in path:
        first, _, rest = path.partition(PATH_SEPARATOR)
        if first.lower() in KNOWN_HOSTS and rest:
            host, path = first, rest

    if not path or not all(_safe_segment(s) for s in path.split(PATH_SEPARATOR)):
        raise RepoRefError(value)
    return RepoRef(host=host.lower(), path=path)


def try_parse(value: object) -> RepoRef | None:
    """`parse`, for the callers whose contract is `None` rather than an error."""
    try:
        return parse(value)
    except RepoRefError:
        return None


def github_slug(
    value: object,
    *,
    hosts: frozenset[str] = GITHUB_HOSTS,
    require_host: bool = False,
) -> str:
    """`owner/name` from a value that names a GitHub repository.

    `hosts` narrows which spellings count, because the callers disagree on
    purpose: a git remote may carry any of `GITHUB_HOSTS`, while a registered
    repository URL may carry only `GITHUB_CANONICAL_HOST`. `require_host`
    refuses the bare shorthand, which a remote never produces.
    """
    ref = parse(value)
    if ref.host:
        if ref.host not in hosts:
            raise RepoRefError(value)
    elif require_host:
        raise RepoRefError(value)
    if len(ref.segments) != GITHUB_PATH_DEPTH:
        raise RepoRefError(value)
    return ref.path


def try_github_slug(
    value: object,
    *,
    hosts: frozenset[str] = GITHUB_HOSTS,
    require_host: bool = False,
) -> str | None:
    """`github_slug`, for the callers whose contract is `None`."""
    try:
        return github_slug(value, hosts=hosts, require_host=require_host)
    except RepoRefError:
        return None


def is_github_slug(value: object) -> bool:
    """True for a bare `owner/name` that is safe to pass to a CLI.

    Bare only: a value carrying a host is not a slug, however well-formed. This
    is the allowlist shape, where the caller has already decided which
    repository it means and is checking that it may act on it.

    The value must already *be* the slug, not merely normalise to one.
    `parse` strips whitespace, surrounding slashes and a trailing `.git`, and a
    predicate that returned True about the normalised form would be answering a
    question about a string its caller does not hold: `is_valid_repository`
    guards the credential sidecar and its caller then execs
    `github_token_refresh.py` with the *original* value, which splits it on `/`
    and sends the left half to Minty as an org name. `" acme/toolkit\\n"` would
    reach the broker as `"  acme"`. Every caller here checks and then uses the
    raw string, so this is the one place that can refuse the difference.

    The owner slot may not be a spelling of GitHub's own host. `parse` lifts
    `github.com/acme` to a host and a one-segment path, so that shape fails the
    depth check on its own — but `www.github.com/acme` and `ssh.github.com/acme`
    are outside `KNOWN_HOSTS` by design (see "On the host") and read as an owner
    called `www.github.com`. That owner cannot exist, GitHub forbids a dot in a
    namespace, and treating it as one sends a hostname to Minty as an org name.
    Refusing all three spellings here is what makes the rule statable rather
    than an accident of which lifts `KNOWN_HOSTS` performs.
    """
    ref = try_parse(value)
    return (
        ref is not None
        and not ref.host
        and len(ref.segments) == GITHUB_PATH_DEPTH
        and ref.segments[0].lower() not in GITHUB_HOSTS
        and value == ref.path
    )
