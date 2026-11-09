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

Standard library only. No refusal message repeats caller text beyond the three
name parts, each of which the caller's target already held to `[a-z0-9-]`.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable

SCOPE_FILE_ENV = "CREDENTIAL_PROXY_SCOPE_FILE"

RULE_EXCLUDED_CLUSTER = "scope.excluded-cluster"
RULE_EXCLUDE_UNREADABLE = "scope.exclude-unreadable"

EXCLUDE_KEY = "exclude"
CLUSTERS_KEY = "clusters"
CLUSTER_REF_KEYS = ("projectId", "location", "clusterName")

NAME_PART = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PROJECT_NUMBER = re.compile(r"^[0-9]+$")


class ExcludedCluster(Exception):
    """A cluster target the install's scope excludes, or one it can't vouch for."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


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
    if not isinstance(entries, list):
        raise ValueError("scope.json's exclude.clusters is not a list")
    triples = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("an exclude.clusters entry is not an object")
        parts = tuple(entry.get(key) for key in CLUSTER_REF_KEYS)
        if not all(isinstance(part, str) and NAME_PART.match(part) for part in parts):
            raise ValueError("an exclude.clusters entry lacks a valid projectId, location or clusterName")
        triples.add(parts)
    return frozenset(triples)


class Exclusions:
    """The broker's view of the excluded clusters, re-read when the file changes.

    The mount is a ConfigMap directory, so an edit lands in place; the file's
    identity (inode, size, mtime) is checked on every call and the parse is
    redone only when it moves.
    """

    def __init__(self, path: Callable[[], str] = lambda: os.getenv(SCOPE_FILE_ENV, "")):
        self._path = path
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

    def check(self, project: str, location: str, cluster: str) -> None:
        """Raise ExcludedCluster when the broker must not reach this cluster."""
        triples, error = self._load()
        if error is not None:
            raise ExcludedCluster(
                RULE_EXCLUDE_UNREADABLE,
                f"{error}, so the broker can't tell whether this cluster is excluded"
                " and reaches no cluster until it can",
            )
        if (project, location, cluster) in triples or any(
            PROJECT_NUMBER.match(p) and (l, c) == (location, cluster) for p, l, c in triples
        ):
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
