"""The post-merge check's steps over Session KV's ``post_merge_records`` (design gke-labs/kube-agents#5054).

Step 2, notice the merge: :func:`notice_merges` reads each waiting record's
pull request from the forge, one read per record per tick and at most
:data:`READS_PER_TICK` records, and writes what it found back to the row:

- merged: ``merged_at`` (the forge's close time, else now) and ``merge_commit``,
  the commit the merge became (the forge's ``mergeCommit``). The later steps
  ask the GitOps tool whether that commit is live.
- closed without a merge: ``closed_at``. It is read again for
  :data:`CLOSED_GRACE`, since closing and reopening is how some teams re-run
  CI: a reopen clears ``closed_at`` and a later merge is noticed. Past the
  grace the record ends, with no outcome and nothing said: a fix someone
  declined is not a failed fix.
- still open, or a read that failed: ``merge_checked_at`` only, which moves
  the record to the back of the queue so every waiting record is read in turn.

A record waits while it has no outcome, no ``merged_at`` and no ``closed_at``
older than the grace. It reads the forge per record rather than sharing the
pull-request sweep, whose list is not exposed to other callers; at the MVP's
scale (a handful of fixes, :data:`READS_PER_TICK` reads a tick) that costs
little. No model is involved, and nothing here posts: the reporter (a later
step) decides what to say, under ``KUBEAGENTS_POST_MERGE_CHECK``. Nothing
schedules it yet; ``post_merge_check.py notice`` is one tick.

Step 3, wait until applied, will read the GitOps reconcilers that deliver the
repository into the record's namespace (:func:`reconcilers_for`) against the
merge commit (:func:`combined_sync`, over ``reconciler_sync``), asking the
broker (:func:`forge_contains`) whether a later applied commit contains it,
and :class:`SandboxKubectl` reads a cluster through the shell sandbox.
Nothing calls these helpers yet; the step that uses them comes next.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("post_merge_check")

RECORDS_TABLE = "post_merge_records"
#: The most records one tick reads from the forge.
READS_PER_TICK = 20
#: The forge read's ceiling, per record, on the agent pod's hop to the sandbox
#: (``forge.call``); a run with no sandbox reaches the broker without one.
VIEW_TIMEOUT_SECONDS = 10.0
#: How long a pull request closed without a merge is still read, for a reopen.
CLOSED_GRACE = timedelta(days=1)

STATE_MERGED = "merged"
STATE_CLOSED = "closed"

CANDIDATES_SQL = (
    f"SELECT url, repo, number, closed_at, merge_checked_at, opened_at FROM {RECORDS_TABLE} "
    "WHERE outcome IS NULL AND merged_at IS NULL"
)

#: Reads one pull request: ``view(repo_url, number)`` -> the forge's proposal dict.
View = Callable[[str, int], dict]


def forge_view(repo: str, number: int) -> dict:
    """The forge's proposal for ``repo`` ``number``, through the broker (``forge.call``)."""
    import forge

    answer = forge.call(
        "proposal-view", {"number": number}, repo, retry_transient=True, timeout_s=VIEW_TIMEOUT_SECONDS
    )
    proposal = answer.get("proposal") if isinstance(answer, dict) else None
    if not isinstance(proposal, dict):
        raise TypeError("the forge answered without a proposal")
    return proposal


def _now_text(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def waiting(conn: sqlite3.Connection, now: datetime, limit: int = READS_PER_TICK) -> list[tuple[str, str, int]]:
    """``(url, repo, number)`` of the records to read this tick, least recently read first.

    A record closed within :data:`CLOSED_GRACE` still waits; one whose close
    time cannot be read is ended, as a close past the grace is.
    """
    picked = []
    for url, repo, number, closed_at, checked_at, opened_at in conn.execute(CANDIDATES_SQL):
        if closed_at is not None:
            closed = _parse_time(closed_at)
            if closed is None or now - closed > CLOSED_GRACE:
                continue
        if not repo or not isinstance(number, int):
            continue
        picked.append((checked_at is not None, checked_at or "", opened_at or "", url, repo, number))
    picked.sort()
    return [(url, repo, number) for _, _, _, url, repo, number in picked[:limit]]


def notice_merges(
    conn: sqlite3.Connection, view: View = forge_view, now: datetime | None = None, limit: int = READS_PER_TICK
) -> dict[str, list[str]]:
    """Read each waiting record's pull request once and record a merge or a close.

    Returns the urls under ``merged``, ``closed``, ``open`` and ``failed``.
    One record's failed read is logged and does not stop the others.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {"merged": [], "closed": [], "open": [], "failed": []}
    for url, repo, number in waiting(conn, now, limit):
        try:
            proposal = view(repo, number)
            state = proposal.get("state")
        except Exception as exc:  # noqa: BLE001 — one unreadable pull request must not stop the tick
            logger.warning("post-merge check: could not read %s: %r", url, exc)
            state, proposal = None, {}
        if state == STATE_MERGED:
            merge_commit = proposal.get("mergeCommit")
            update = (
                "merged_at = ?, merge_commit = ?, closed_at = NULL",
                (str(proposal.get("closed") or stamp), merge_commit if isinstance(merge_commit, str) and merge_commit else None),
                "merged",
            )
        elif state == STATE_CLOSED:
            update = ("closed_at = COALESCE(closed_at, ?)", (str(proposal.get("closed") or stamp),), "closed")
        elif state is not None:
            # Open, or reopened after a close: it waits again.
            update = ("closed_at = NULL", (), "open")
        else:
            update = (None, (), "failed")
        sets, values, bucket = update
        try:
            with conn:
                conn.execute(
                    f"UPDATE {RECORDS_TABLE} SET {sets + ', ' if sets else ''}merge_checked_at = ? WHERE url = ?",
                    (*values, stamp, url),
                )
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record what %s is: %r", url, exc)
            bucket = "failed"
        seen[bucket].append(url)
    return seen


# --------------------------------------------------------------------------
# Step 3, wait until applied: reading reconcilers and selectors
# --------------------------------------------------------------------------

#: The reconciler objects that can deliver a merge, as kubectl names them.
ARGO_APPLICATIONS = "applications.argoproj.io"
FLUX_KUSTOMIZATIONS = "kustomizations.kustomize.toolkit.fluxcd.io"
FLUX_GIT_REPOSITORIES = "gitrepositories.source.toolkit.fluxcd.io"
#: Named so a test can show it never counts; not read, since a RootSync names no namespace.
CONFIGSYNC_ROOTSYNCS = "rootsyncs.configsync.gke.io"
CONFIGSYNC_REPOSYNCS = "reposyncs.configsync.gke.io"
#: The lists one tick reads per cluster.
RECONCILER_RESOURCES = (ARGO_APPLICATIONS, FLUX_KUSTOMIZATIONS, FLUX_GIT_REPOSITORIES, CONFIGSYNC_REPOSYNCS)
#: An Argo CD Application deploys to the cluster it runs on when its
#: destination names this server or this name (or none).
ARGO_IN_CLUSTER = ({"", "https://kubernetes.default.svc"}, {"", "in-cluster"})
#: kubectl's answers for a resource type the cluster does not serve, and for an object that is gone.
NOT_SERVED = "doesn't have a resource type"
NOT_FOUND = "NotFound"
KUBECTL_REQUEST_TIMEOUT_SECONDS = 20
KUBECTL_HOP_TIMEOUT_SECONDS = 45


class ClusterReadError(RuntimeError):
    """A cluster read that did not answer."""


class WorkloadGone(ClusterReadError):
    """The workload the fix is for no longer exists, or cannot be judged."""


#: Reads one cluster: ``kubectl_json(record, argv)`` -> the parsed ``-o json`` answer,
#: ``{"items": []}`` for a resource type the cluster does not serve. Raises
#: :class:`WorkloadGone` for an object kubectl reports NotFound.
KubectlJson = Callable[[dict, list], dict]


def forge_contains(repo: str, ancestor: str, revision: str) -> bool | None:
    """The broker's ``revision-contains`` answer; None when it cannot say."""
    try:
        import forge

        answer = forge.call(
            "revision-contains", {"ancestor": ancestor, "revision": revision}, repo,
            retry_transient=True, timeout_s=VIEW_TIMEOUT_SECONDS * 3,
        )
    except Exception as exc:  # noqa: BLE001 — unknown ancestry reads as unconfirmed
        logger.warning("post-merge check: could not ask whether %s contains %s: %r", revision, ancestor, exc)
        return None
    contains = answer.get("contains") if isinstance(answer, dict) else None
    return contains if isinstance(contains, bool) else None


def reconcilers_for(repo: str, namespace: str, lists: dict[str, dict]) -> list[dict]:
    """The reconciler objects on this cluster that deliver ``repo`` into ``namespace``.

    An Argo CD Application counts when one of its Git sources is ``repo``, it
    deploys to this cluster and its destination namespace is ``namespace``; a
    Flux Kustomization when its GitRepository is ``repo`` and its
    ``targetNamespace`` is ``namespace``; a Config Sync RepoSync in ``namespace``
    with ``repo`` as its source. A RootSync names no namespace, so it never
    counts: with nothing targeting the namespace, the fix is observed directly.
    ``lists`` maps the resource names above to their ``kubectl get -A -o json`` answers.
    """
    import clean_merges

    repo_key = clean_merges.repo_key
    key = repo_key(repo)
    if not key:
        return []
    found = []
    for app in lists.get(ARGO_APPLICATIONS, {}).get("items", []):
        spec = app.get("spec") or {}
        destination = spec.get("destination") or {}
        if str(destination.get("server") or "") not in ARGO_IN_CLUSTER[0] or str(destination.get("name") or "") not in ARGO_IN_CLUSTER[1]:
            continue
        sources = ([spec["source"]] if isinstance(spec.get("source"), dict) else []) + list(spec.get("sources") or [])
        if destination.get("namespace") == namespace and any(
            repo_key(source.get("repoURL")) == key for source in sources if isinstance(source, dict)
        ):
            found.append(app)
    repositories = {
        ((r.get("metadata") or {}).get("namespace"), (r.get("metadata") or {}).get("name")): r
        for r in lists.get(FLUX_GIT_REPOSITORIES, {}).get("items", [])
    }
    for kustomization in lists.get(FLUX_KUSTOMIZATIONS, {}).get("items", []):
        meta, spec = kustomization.get("metadata") or {}, kustomization.get("spec") or {}
        ref = spec.get("sourceRef") or {}
        source = repositories.get((ref.get("namespace") or meta.get("namespace"), ref.get("name")))
        if (ref.get("kind") == "GitRepository" and source and spec.get("targetNamespace") == namespace
                and repo_key((source.get("spec") or {}).get("url")) == key):
            found.append(kustomization)
    for sync in lists.get(CONFIGSYNC_REPOSYNCS, {}).get("items", []):
        git = (sync.get("spec") or {}).get("git") or {}
        if (sync.get("metadata") or {}).get("namespace") == namespace and repo_key(git.get("repo")) == key:
            found.append(sync)
    return found


def combined_sync(objects: list[dict], merge_commit: str, contains: Callable[[str, str], "bool | None"] | None = None) -> tuple[str, str]:
    """``(state, detail)`` over the reconcilers that deliver the fix: applied only when every one is."""
    import reconciler_sync

    if not objects:
        return reconciler_sync.UNKNOWN, "no reconciler delivers the fix"
    readings = []
    for obj in objects:
        try:
            readings.append(reconciler_sync.read_sync(obj, merge_commit, contains))
        except (TypeError, ValueError) as exc:
            readings.append(reconciler_sync.SyncReading(reconciler_sync.UNKNOWN, "", "", str(exc)))
    for state in (reconciler_sync.ERROR, reconciler_sync.UNKNOWN, reconciler_sync.UNCONFIRMED, reconciler_sync.PENDING):
        for reading in readings:
            if reading.state == state:
                return state, reading.detail
    return reconciler_sync.APPLIED, readings[0].detail


SELECTOR_OPERATORS = {"In": "in", "NotIn": "notin"}


def label_selector(selector: dict) -> str:
    """A Kubernetes LabelSelector as kubectl's ``-l`` text, ``matchExpressions`` included."""
    parts = [f"{key}={value}" for key, value in sorted((selector.get("matchLabels") or {}).items())]
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if operator in SELECTOR_OPERATORS:
            parts.append(f"{key} {SELECTOR_OPERATORS[operator]} ({','.join(values)})")
        elif operator == "Exists":
            parts.append(str(key))
        elif operator == "DoesNotExist":
            parts.append(f"!{key}")
        else:
            raise WorkloadGone(f"the selector operator {operator!r} is not one the health window reads")
    return ",".join(parts)


def selects(selector: dict, labels: dict) -> bool:
    """Whether a LabelSelector matches ``labels`` (a pod template's)."""
    if any(labels.get(key) != value for key, value in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if (operator == "In" and labels.get(key) not in values) or (operator == "NotIn" and labels.get(key) in values) \
                or (operator == "Exists" and key not in labels) or (operator == "DoesNotExist" and key in labels):
            return False
    return bool(selector.get("matchLabels") or selector.get("matchExpressions"))


def cluster_coordinates(record: dict) -> tuple[str, str, str]:
    """``(project, name, location)`` for ``gcloud``: the fleet audit names a cluster
    ``<project>/<location>/<name>``, so a qualified ``cluster`` gives the bare name, and its parts
    fill a ``project`` or ``location`` the record left empty."""
    cluster = str(record.get("cluster") or "")
    project, location = record.get("project") or "", record.get("location") or ""
    parts = cluster.split("/")
    if len(parts) == 3 and all(parts):
        project, location, cluster = project or parts[0], location or parts[1], parts[2]
    return project, cluster, location


class SandboxKubectl:
    """``kubectl ... -o json`` in the shell sandbox, against the record's cluster.

    The agent pod carries no kubectl (``sandbox_exec``). Credentials are the
    stall watch's per-cluster kubeconfig in the hermes-owned directory, fetched
    once per cluster per run.
    """

    def __init__(self) -> None:
        self.kubeconfigs: dict[tuple[str, str, str], str] = {}

    def __call__(self, record: dict, argv: list) -> dict:
        import stall_watch

        key = cluster_coordinates(record)
        if key not in self.kubeconfigs:
            self.kubeconfigs[key] = stall_watch.fetch_credentials(*key)
        done = stall_watch.run_sandbox(
            ["kubectl", *argv, f"--request-timeout={KUBECTL_REQUEST_TIMEOUT_SECONDS}s"],
            timeout=KUBECTL_HOP_TIMEOUT_SECONDS,
            kubeconfig=self.kubeconfigs[key],
        )
        if done.returncode != 0:
            stderr = done.stderr or ""
            if NOT_SERVED in stderr:
                return {"items": []}
            if NOT_FOUND in stderr and "-A" in argv:
                return {"items": []}  # a list that 404s is a kind this cluster no longer serves, not a gone workload
            if NOT_FOUND in stderr:
                raise WorkloadGone(stall_watch.stderr_excerpt(stderr))
            raise ClusterReadError(stall_watch.stderr_excerpt(stderr))
        return json.loads(done.stdout or "{}")


#: Session KV's database, as ``session_kv_server.py`` reads the same variable.
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
SESSION_KV_DB_DEFAULT = "/var/lib/kube-agents/session/session_kv.db"
SWITCH = "POST_MERGE_CHECK"


def main(argv: list[str] | None = None, view: View = forge_view) -> int:
    """``post_merge_check.py notice``: one tick of the merge step; prints what it saw as JSON.

    Reads nothing while the install's ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``.
    """
    import json
    import os
    import sys
    from contextlib import closing

    argv = sys.argv[1:] if argv is None else argv
    if argv != ["notice"]:
        print("usage: post_merge_check.py notice", file=sys.stderr)
        return 2
    import kubeagents_switches

    if kubeagents_switches.switch(SWITCH) == kubeagents_switches.OFF:
        print(json.dumps({"switch": kubeagents_switches.OFF}))
        return 0
    path = os.environ.get(SESSION_KV_DB_ENV) or SESSION_KV_DB_DEFAULT
    with closing(sqlite3.connect(path, timeout=5.0)) as conn:
        print(json.dumps(notice_merges(conn, view)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
