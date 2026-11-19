#!/usr/bin/env python3
"""The clusters the credential broker refuses to reach: `spec.scope.exclude.clusters`.

The operator renders the PlatformAgent's scope into `scope.json` and mounts it on
the broker (`CREDENTIAL_PROXY_SCOPE_FILE`). An excluded cluster is one named there
by its full `projectId`, `location` and `clusterName`. The broker refuses every
route to that cluster's Kubernetes API, for every caller: a kubeconfig it would
mint or use, a `get-credentials`, the watcher's cluster relay. Reads of the GKE
control plane (`clusters list`, `clusters describe`) stay allowed, because they
are reads of the project, not of the cluster.

Matching is exact, field by field. A cluster's location is the one GKE gives it,
a zone or a region, so a zonal name never matches a regional entry. A project
named by its number can't be told apart from an excluded project's ID without a
lookup, so while any cluster is excluded a target whose project is all digits is
refused too, naming the project ID as the way through; and an entry spelled by
its project number refuses its location and cluster name in any project, which
over-refuses rather than miss the cluster. Spell entries by project ID.

Fail-closed: when the broker is told where the file is and can't read or parse
it, every cluster target is refused until it can. The variable unset is a broker
started outside the operator, and excludes nothing.

`spec.scope.hold` is refused the same way, from a file of its own
(`CREDENTIAL_PROXY_HOLD_FILE`, the `<agent>-scope-hold` ConfigMap's `hold.json`)
that no pod-template hash covers, so an operator's hold applies, and lifts,
without a restart. A held cluster keeps its Cluster Agent profile and its place in
fleet audits; only its Kubernetes API is refused, under `scope.held-cluster`, with the hold's
reason. `all` holds every cluster. `Exclusions` checks both, so every route that
refuses an excluded cluster refuses a held one.

Standard library only. No refusal message repeats caller text beyond the three
name parts, each of which the caller's target already held to `[a-z0-9-]`; a
hold's reason is the operator's text, with control characters dropped and its
length capped.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable

SCOPE_FILE_ENV = "CREDENTIAL_PROXY_SCOPE_FILE"
HOLD_FILE_ENV = "CREDENTIAL_PROXY_HOLD_FILE"

RULE_EXCLUDED_CLUSTER = "scope.excluded-cluster"
RULE_EXCLUDE_UNREADABLE = "scope.exclude-unreadable"
RULE_HELD_CLUSTER = "scope.held-cluster"
RULE_HOLD_UNREADABLE = "scope.hold-unreadable"

EXCLUDE_KEY = "exclude"
CLUSTERS_KEY = "clusters"
HOLD_ALL_KEY = "all"
HOLD_REASON_KEY = "reason"
# The CRD caps spec.scope.hold.reason at 256; the broker holds it to that too.
HOLD_REASON_MAX_LENGTH = 256
HOLD_STOP_NOTICE = (
    "An operator paused agent access to it; report this and stop, do not retry or work around it"
)
CLUSTER_REF_KEYS = ("projectId", "location", "clusterName")

NAME_PART = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PROJECT_NUMBER = re.compile(r"^[0-9]+$")


class ExcludedCluster(Exception):
    """A cluster target the install's scope excludes, or one it can't vouch for."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


class HeldCluster(ExcludedCluster):
    """A cluster target an operator's spec.scope.hold pauses, or one it can't vouch for.

    An ExcludedCluster, so every route that answers an exclusion answers a hold.
    """


def parse_exclusions(document: object) -> frozenset[tuple[str, str, str]]:
    """The `(project, location, cluster)` triples a rendered `scope.json` excludes.

    Raises ValueError on a document whose exclusion list isn't the shape the
    operator renders: a broker that skipped a malformed entry would let that
    cluster through, so the whole document is refused instead.
    """
    if not isinstance(document, dict):
        raise ValueError("scope.json is not an object")
    exclude = document.get(EXCLUDE_KEY)
    if exclude is None:
        return frozenset()
    if not isinstance(exclude, dict):
        raise ValueError("scope.json's exclude is not an object")
    entries = exclude.get(CLUSTERS_KEY)
    if entries is None:
        return frozenset()
    return _parse_cluster_refs(entries, "exclude.clusters")


def _parse_cluster_refs(entries: object, label: str) -> frozenset[tuple[str, str, str]]:
    """The `(project, location, cluster)` triples of a list the operator renders."""
    if not isinstance(entries, list):
        raise ValueError(f"{label} is not a list")
    triples = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"a {label} entry is not an object")
        parts = tuple(entry.get(key) for key in CLUSTER_REF_KEYS)
        if not all(isinstance(part, str) and NAME_PART.match(part) for part in parts):
            raise ValueError(f"a {label} entry lacks a valid projectId, location or clusterName")
        triples.add(parts)
    return frozenset(triples)


def _matches(triples: frozenset[tuple[str, str, str]], project: str, location: str, cluster: str) -> bool:
    """Whether a target is one of triples, an entry spelled by project number matching
    its location and name in any project."""
    return (project, location, cluster) in triples or any(
        PROJECT_NUMBER.match(p) and (l, c) == (location, cluster) for p, l, c in triples
    )


class HoldState:
    """A parsed hold.json: every cluster, these clusters, and the operator's reason."""

    def __init__(self, held_all: bool = False, triples: frozenset = frozenset(), reason: str = ""):
        self.held_all = held_all
        self.triples = triples
        self.reason = reason

    def any(self) -> bool:
        return self.held_all or bool(self.triples)

    def reason_suffix(self) -> str:
        return f": {self.reason}" if self.reason else ""


def parse_holds(document: object) -> HoldState:
    """The hold a rendered `hold.json` declares. `{}` holds nothing.

    Raises ValueError on a document that isn't the shape the operator renders,
    so a malformed hold refuses every cluster rather than none.
    """
    if not isinstance(document, dict):
        raise ValueError("hold.json is not an object")
    held_all = document.get(HOLD_ALL_KEY, False)
    if not isinstance(held_all, bool):
        raise ValueError("hold.json's all is not a boolean")
    reason = document.get(HOLD_REASON_KEY, "")
    if not isinstance(reason, str):
        raise ValueError("hold.json's reason is not a string")
    entries = document.get(CLUSTERS_KEY)
    triples = frozenset() if entries is None else _parse_cluster_refs(entries, "hold.clusters")
    reason = "".join(ch for ch in reason if ch.isprintable()).strip()[:HOLD_REASON_MAX_LENGTH]
    return HoldState(held_all, triples, reason)


class Holds:
    """The broker's view of spec.scope.hold, re-read when the file changes.

    The same identity check as Exclusions: the ConfigMap directory mount swaps
    the file in place, and the parse is redone only when it moves. Fail-closed
    on a configured file it can't read or parse; an unset variable holds nothing.
    """

    def __init__(self, path: Callable[[], str] = lambda: os.getenv(HOLD_FILE_ENV, "")):
        self._path = path
        self._lock = threading.Lock()
        self._seen: tuple | None = None
        self._state = HoldState()
        self._error: str | None = None

    def _load(self) -> tuple[HoldState, str | None]:
        path = self._path()
        if not path:
            return HoldState(), None
        try:
            stat = os.stat(path)
        except OSError:
            return HoldState(), "the install's hold file can't be read"
        identity = (path, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if identity != self._seen:
                try:
                    with open(path, encoding="utf-8") as stream:
                        self._state, self._error = parse_holds(json.load(stream)), None
                except (OSError, ValueError):
                    self._state, self._error = HoldState(), "the install's hold file can't be parsed"
                self._seen = identity
            return self._state, self._error

    def check_unnamed(self) -> None:
        """Raise HeldCluster for a call that reaches a cluster this broker can't name."""
        state, error = self._load()
        if error is not None:
            raise HeldCluster(RULE_HOLD_UNREADABLE, f"{error}, so the broker reaches no cluster until it can")
        if state.held_all:
            raise HeldCluster(
                RULE_HELD_CLUSTER,
                f"every cluster is on hold (spec.scope.hold.all){state.reason_suffix()}. {HOLD_STOP_NOTICE}",
            )
        if state.triples:
            raise HeldCluster(
                RULE_HELD_CLUSTER,
                "this install holds clusters, and this call doesn't name its cluster by"
                " projects/<project>/locations/<location>/clusters/<cluster>; name it that way",
            )

    def check(self, project: str, location: str, cluster: str) -> None:
        """Raise HeldCluster when an operator's hold pauses this cluster."""
        state, error = self._load()
        if error is not None:
            raise HeldCluster(
                RULE_HOLD_UNREADABLE,
                f"{error}, so the broker can't tell whether this cluster is on hold"
                " and reaches no cluster until it can",
            )
        if state.held_all:
            raise HeldCluster(
                RULE_HELD_CLUSTER,
                f"every cluster is on hold (spec.scope.hold.all){state.reason_suffix()}. {HOLD_STOP_NOTICE}",
            )
        if _matches(state.triples, project, location, cluster):
            raise HeldCluster(
                RULE_HELD_CLUSTER,
                f"cluster {cluster} ({project}, {location}) is on hold (spec.scope.hold)"
                f"{state.reason_suffix()}. {HOLD_STOP_NOTICE}",
            )
        if state.triples and PROJECT_NUMBER.match(project):
            raise HeldCluster(
                RULE_HELD_CLUSTER,
                f"project {project} is named by its number while this install holds"
                " clusters by project ID; name the project by its ID",
            )


class Exclusions:
    """The broker's view of the excluded clusters, re-read when the file changes.

    The mount is a ConfigMap directory, so an edit lands in place; the file's
    identity (inode, size, mtime) is checked on every call and the parse is
    redone only when it moves. Each check also consults the holds (Holds), so a
    caller of check or check_unnamed refuses a held cluster too.
    """

    def __init__(
        self,
        path: Callable[[], str] = lambda: os.getenv(SCOPE_FILE_ENV, ""),
        holds: "Holds | None" = None,
    ):
        self._path = path
        self.holds = holds if holds is not None else Holds()
        self._lock = threading.Lock()
        self._seen: tuple | None = None
        self._triples: frozenset[tuple[str, str, str]] = frozenset()
        self._error: str | None = None

    def _load(self) -> tuple[frozenset[tuple[str, str, str]], str | None]:
        path = self._path()
        if not path:
            return frozenset(), None
        try:
            stat = os.stat(path)
        except OSError:
            return frozenset(), "the install's scope file can't be read"
        identity = (path, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if identity != self._seen:
                try:
                    with open(path, encoding="utf-8") as stream:
                        self._triples, self._error = parse_exclusions(json.load(stream)), None
                except (OSError, ValueError):
                    # json.JSONDecodeError and UnicodeDecodeError are ValueErrors.
                    self._triples, self._error = frozenset(), "the install's scope file can't be parsed"
                self._seen = identity
            return self._triples, self._error

    def check_unnamed(self) -> None:
        """Raise ExcludedCluster for a call that reaches a cluster this broker can't name.

        Nothing is refused on an install that excludes no cluster; with one, a
        call that might be the excluded cluster is refused.
        """
        triples, error = self._load()
        if error is not None:
            raise ExcludedCluster(RULE_EXCLUDE_UNREADABLE, f"{error}, so the broker reaches no cluster until it can")
        if triples:
            raise ExcludedCluster(
                RULE_EXCLUDED_CLUSTER,
                "this install excludes clusters, and this call doesn't name its cluster by"
                " projects/<project>/locations/<location>/clusters/<cluster>; name it that way",
            )
        self.holds.check_unnamed()

    def check(self, project: str, location: str, cluster: str) -> None:
        """Raise ExcludedCluster when the broker must not reach this cluster."""
        triples, error = self._load()
        if error is not None:
            raise ExcludedCluster(
                RULE_EXCLUDE_UNREADABLE,
                f"{error}, so the broker can't tell whether this cluster is excluded"
                " and reaches no cluster until it can",
            )
        if _matches(triples, project, location, cluster):
            raise ExcludedCluster(
                RULE_EXCLUDED_CLUSTER,
                f"cluster {cluster} ({project}, {location}) is excluded by this install's"
                " spec.scope.exclude.clusters; the agents don't reach its Kubernetes API",
            )
        if triples and PROJECT_NUMBER.match(project):
            raise ExcludedCluster(
                RULE_EXCLUDED_CLUSTER,
                f"project {project} is named by its number while this install excludes"
                " clusters by project ID; name the project by its ID",
            )
        self.holds.check(project, location, cluster)
