"""The environment of a checked fix's cluster, for the clean-merge counter (design gke-labs/kube-agents#5054).

``clean_merges.py`` and the dial's progress count merges per repository, kind
of fix and environment, so a record with no ``environment`` counts under
``""`` with every unlabelled cluster's, not under its own cluster's.
:func:`fill_environments` gives each ``verified_fixed`` or ``still_broken``
record without one its cluster's environment: the cluster's GKE resource
labels, read with ``gcloud`` in the shell sandbox and resolved by
``automation_rules.cluster_environment`` (the same rule the rules file and the
broker's auto-merge label use), ``""`` when the cluster declares none.

``post_merge_environment.py fill`` is one tick. It reads at most
``post_merge_check.READS_PER_TICK`` clusters, each once a run, newest outcome
first, and reads nothing while ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``. A
read that fails is tried again next run until the outcome is
:data:`GIVE_UP_AFTER` old, or at once when the outcome time will not parse;
then the record is given ``""``, so a deleted cluster is not read forever. The ``post-merge-check`` cron job runs it
(``post_merge_outcome.tick``). No model is involved,
and nothing here posts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import post_merge_check

logger = logging.getLogger(__name__)

#: The outcomes a clean merge or a step-down is counted from.
VERIFIED_FIXED = "verified_fixed"
STILL_BROKEN = "still_broken"
#: How long after the outcome an unreadable cluster is still read.
GIVE_UP_AFTER = timedelta(days=1)

EnvironmentOf = Callable[[dict], str]

FILL_SQL = (
    f"SELECT url, project, location, cluster, outcome_at FROM {post_merge_check.RECORDS_TABLE} "
    f"WHERE outcome IN ('{VERIFIED_FIXED}', '{STILL_BROKEN}') AND environment IS NULL "
    "AND COALESCE(cluster, '') <> '' ORDER BY outcome_at DESC"
)


def sandbox_environment_of(record: dict) -> str:
    """The record's cluster's environment, from its resource labels read with gcloud in the sandbox."""
    import automation_rules
    import stall_watch

    project, cluster, location = post_merge_check.cluster_coordinates(record)
    if not (project and cluster and location):
        raise post_merge_check.ClusterReadError("the record does not name the cluster's project and location")
    done = stall_watch.run_sandbox(
        ["gcloud", "container", "clusters", "describe", cluster, f"--location={location}",
         f"--project={project}", "--format=json(resourceLabels)"],
        timeout=post_merge_check.KUBECTL_HOP_TIMEOUT_SECONDS,
    )
    if done.returncode != 0:
        raise post_merge_check.ClusterReadError(stall_watch.stderr_excerpt(done.stderr))
    labels = (json.loads(done.stdout or "{}") or {}).get("resourceLabels") or {}
    return automation_rules.cluster_environment(labels)


def fill_environments(conn: sqlite3.Connection, environment_of: EnvironmentOf = sandbox_environment_of,
                      now: datetime | None = None,
                      limit: int = post_merge_check.READS_PER_TICK) -> dict[str, list[str]]:
    """``environment`` for each ``verified_fixed`` or ``still_broken`` record with none yet.

    Returns the urls under ``filled``, ``unreadable`` (read again next run),
    ``given-up`` (given ``""``) and ``errors``. ``limit`` caps clusters read,
    not records: records on one cluster share its read.
    """
    now = now or datetime.now(timezone.utc)
    seen: dict[str, list[str]] = {"filled": [], "unreadable": [], "given-up": [], "errors": []}
    read: dict[tuple[str, str, str], str | None] = {}
    reads = 0
    for url, project, location, cluster, outcome_at in conn.execute(FILL_SQL).fetchall():
        record = {"url": url, "project": project, "location": location, "cluster": cluster}
        key = post_merge_check.cluster_coordinates(record)
        if key not in read and not all(key):
            # Named too little to read: no sandbox call, so none of ``limit``.
            read[key] = None
        if key not in read:
            if reads >= limit:
                continue
            reads += 1
            try:
                read[key] = environment_of(record)
            except Exception as exc:  # noqa: BLE001 — read again next run
                logger.warning("post-merge check: could not read the environment of %s: %r", url, exc)
                read[key] = None
        environment, bucket = read[key], "filled"
        if environment is None:
            when = post_merge_check._parse_time(outcome_at)
            if when is not None and now - when <= GIVE_UP_AFTER:
                seen["unreadable"].append(url)
                continue
            environment, bucket = "", "given-up"
        try:
            with conn:
                conn.execute(f"UPDATE {post_merge_check.RECORDS_TABLE} SET environment = ? WHERE url = ?",
                             (environment, url))
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record the environment of %s: %r", url, exc)
            bucket = "errors"
        seen[bucket].append(url)
    return seen


def main(argv: list[str] | None = None, environment_of: EnvironmentOf = sandbox_environment_of) -> int:
    """``post_merge_environment.py fill``: one tick; prints what it saw as JSON.

    Reads nothing while the install's ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``.
    """
    import sys
    from contextlib import closing

    argv = sys.argv[1:] if argv is None else argv
    if argv != ["fill"]:
        print("usage: post_merge_environment.py fill", file=sys.stderr)
        return 2
    import kubeagents_switches

    if kubeagents_switches.switch(post_merge_check.SWITCH) == kubeagents_switches.OFF:
        print(json.dumps({"switch": kubeagents_switches.OFF}))
        return 0
    path = os.environ.get(post_merge_check.SESSION_KV_DB_ENV) or post_merge_check.SESSION_KV_DB_DEFAULT
    with closing(sqlite3.connect(path, timeout=5.0)) as conn:
        print(json.dumps(fill_environments(conn, environment_of)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
