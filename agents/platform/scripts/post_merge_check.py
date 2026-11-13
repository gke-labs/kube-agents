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

Step 3, wait until applied (``post_merge_check.py health``, one tick, also
unscheduled): :func:`watch_health` reads each merged record's cluster through
the shell sandbox. The GitOps reconcilers that deliver the repository into the
record's namespace are read against the merge commit (``reconciler_sync``),
asking the broker (``revision-contains``) whether a later applied commit
contains it; with none, or when the broker cannot say, the object the fix adds
is observed directly. Once applied, the pods' restart counts are the baseline
for the health window, the next step.
"""

from __future__ import annotations

import json
import logging
import re
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

HEALTH_FAILED = "failed"
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
#: The workload kinds a disruption-budget fix covers.
WORKLOAD_RESOURCES = {"Deployment": "deployments.apps", "StatefulSet": "statefulsets.apps"}
#: How the fix is seen when no reconciler on the cluster targets the record's
#: namespace: the object the fix adds, read directly (design: "the driver
#: observes the object directly").
DIRECT_OBJECTS = {"no-pdb": "poddisruptionbudgets.policy"}
#: ``sync_state``'s reading for a fix observed directly.
OBSERVED = "observed"

#: Merged records not yet applied, least recently read first.
HEALTH_SQL = (
    f"SELECT url, repo, kind, project, location, cluster, namespace, object, merge_commit "
    f"FROM {RECORDS_TABLE} WHERE outcome IS NULL AND merged_at IS NOT NULL "
    "AND merge_commit IS NOT NULL AND health_result IS NULL AND applied_at IS NULL "
    "ORDER BY health_checked_at IS NOT NULL, health_checked_at, merged_at LIMIT ?"
)
HEALTH_COLUMNS = ("url", "repo", "kind", "project", "location", "cluster", "namespace", "object", "merge_commit")


class ClusterReadError(RuntimeError):
    """A cluster read that did not answer."""


class WorkloadGone(ClusterReadError):
    """The workload the fix is for no longer exists, or cannot be judged."""


#: Reads one cluster: ``kubectl_json(record, argv)`` -> the parsed ``-o json`` answer,
#: ``{"items": []}`` for a resource type the cluster does not serve. Raises
#: :class:`WorkloadGone` for an object kubectl reports NotFound.
KubectlJson = Callable[[dict, list], dict]
#: ``contains(repo, ancestor, revision)`` -> whether ``revision`` contains ``ancestor``; None when unknown.
Contains = Callable[[str, str, str], "bool | None"]


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


def _repo_key(url: object) -> str:
    """A git url as one comparable key: host and path, lower-case, no scheme, user, ``.git`` or trailing slash."""
    text = str(url or "").strip().lower()
    text = re.sub(r"^[a-z+]+://", "", text)
    if "@" in text.split("/", 1)[0]:
        text = text.split("@", 1)[1].replace(":", "/", 1)
    return re.sub(r"(\.git)?/*$", "", text)


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
    key = _repo_key(repo)
    found = []
    for app in lists.get(ARGO_APPLICATIONS, {}).get("items", []):
        spec = app.get("spec") or {}
        destination = spec.get("destination") or {}
        if str(destination.get("server") or "") not in ARGO_IN_CLUSTER[0] or str(destination.get("name") or "") not in ARGO_IN_CLUSTER[1]:
            continue
        sources = ([spec["source"]] if isinstance(spec.get("source"), dict) else []) + list(spec.get("sources") or [])
        if destination.get("namespace") == namespace and any(
            _repo_key(source.get("repoURL")) == key for source in sources if isinstance(source, dict)
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
                and _repo_key((source.get("spec") or {}).get("url")) == key):
            found.append(kustomization)
    for sync in lists.get(CONFIGSYNC_REPOSYNCS, {}).get("items", []):
        git = (sync.get("spec") or {}).get("git") or {}
        if (sync.get("metadata") or {}).get("namespace") == namespace and _repo_key(git.get("repo")) == key:
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


def restart_baseline(pods: dict) -> dict[str, int]:
    """``{"<pod uid>/<container>": restartCount}`` for every container of every pod."""
    out = {}
    for pod in pods.get("items", []):
        uid = (pod.get("metadata") or {}).get("uid") or (pod.get("metadata") or {}).get("name") or ""
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            out[f"{uid}/{status.get('name')}"] = int(status.get("restartCount") or 0)
    return out


def _read_workload(kubectl_json: KubectlJson, record: dict) -> tuple[dict, dict]:
    kind, _, name = str(record["object"]).partition("/")
    resource = WORKLOAD_RESOURCES.get(kind)
    if resource is None or not name:
        raise WorkloadGone(f"{record['object']} is not a workload the health window reads")
    namespace = record["namespace"] or "default"
    workload = kubectl_json(record, ["get", resource, name, "-n", namespace, "-o", "json"])
    selector = label_selector((workload.get("spec") or {}).get("selector") or {})
    if not selector:
        raise WorkloadGone(f"{record['object']} has an empty selector")
    pods = kubectl_json(record, ["get", "pods", "-n", namespace, "-l", selector, "-o", "json"])
    return workload, pods


def observed_directly(kubectl_json: KubectlJson, record: dict, workload: dict) -> tuple[bool, str]:
    """Whether the object the fix adds is on the cluster: for ``no-pdb``, a PodDisruptionBudget selecting the workload."""
    namespace = record["namespace"] or "default"
    resource = DIRECT_OBJECTS.get(record["kind"] or "")
    if resource is None:
        return False, f"no reconciler targets {namespace} and a {record['kind']} fix cannot be observed directly"
    labels = (((workload.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
    budgets = kubectl_json(record, ["get", resource, "-n", namespace, "-o", "json"])
    for budget in budgets.get("items", []):
        if selects((budget.get("spec") or {}).get("selector") or {}, labels):
            return True, f"no reconciler targets {namespace}; budget {budget['metadata']['name']} selects the workload"
    return False, f"no reconciler targets {namespace}; no budget selects the workload yet"


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

        key = (record["project"], record["cluster"], record["location"])
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


def watch_health(
    conn: sqlite3.Connection,
    kubectl_json: KubectlJson | None = None,
    now: datetime | None = None,
    limit: int = READS_PER_TICK,
    contains: Contains = forge_contains,
) -> dict[str, list[str]]:
    """Step 3 for each merged record: wait until the fix is applied.

    Not yet applied: read the reconcilers on the record's cluster that deliver
    its repository into its namespace (:func:`reconcilers_for`) against the
    merge commit, asking the broker whether a later applied commit contains it
    (``contains``); with none, observe the fix's object directly
    (:func:`observed_directly`). The reading goes in ``sync_state``. Applied:
    ``applied_at`` and the pods' restart counts (``health_baseline``). A
    workload that is gone or cannot be judged fails with the reason
    (``health_result``, ``health_detail``); any other read failure is kept in
    ``sync_state`` and retried. Every read stamps
    ``health_checked_at``, which orders the next tick, so no record starves the
    rest. Everything lives on the row, so a restart loses nothing. Returns urls
    under ``applied``, ``waiting``, ``failed`` and ``unreadable``.
    """
    import reconciler_sync

    kubectl_json = kubectl_json or SandboxKubectl()
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {"applied": [], "waiting": [], "failed": [], "unreadable": []}
    lists_by_cluster: dict[tuple, dict] = {}
    answers: dict[tuple, bool | None] = {}
    for values in conn.execute(HEALTH_SQL, (limit,)).fetchall():
        record = dict(zip(HEALTH_COLUMNS, values))
        url = record["url"]
        updates: dict[str, object] = {"health_checked_at": stamp}
        try:
            if not (record["project"] and record["location"] and record["cluster"] and record["object"]):
                raise ClusterReadError("the record names no cluster or workload to read")
            cluster = (record["project"], record["location"], record["cluster"])
            if cluster not in lists_by_cluster:
                lists_by_cluster[cluster] = {
                    resource: kubectl_json(record, ["get", resource, "-A", "-o", "json"])
                    for resource in RECONCILER_RESOURCES
                }

            def asked(ancestor: str, revision: str, repo: str = record["repo"]) -> bool | None:
                if (repo, ancestor, revision) not in answers:
                    answers[(repo, ancestor, revision)] = contains(repo, ancestor, revision)
                return answers[(repo, ancestor, revision)]

            objects = reconcilers_for(record["repo"], record["namespace"] or "default", lists_by_cluster[cluster])
            workload, pods = _read_workload(kubectl_json, record)
            if objects:
                state, detail = combined_sync(objects, record["merge_commit"], asked)
                applied = state == reconciler_sync.APPLIED
                if state == reconciler_sync.UNCONFIRMED:
                    # The broker could not say whether the applied commit contains the merge (an
                    # abbreviated or SHA-256 merge commit, a fetch it refused): look for the fix itself.
                    applied, direct = observed_directly(kubectl_json, record, workload)
                    if applied:
                        state, detail = OBSERVED, direct
            else:
                applied, detail = observed_directly(kubectl_json, record, workload)
                state = OBSERVED if applied else reconciler_sync.PENDING
            updates["sync_state"] = f"{state}: {detail}"[:500]
            if applied:
                updates["applied_at"] = stamp
                updates["health_baseline"] = json.dumps(restart_baseline(pods), sort_keys=True)
                seen["applied"].append(url)
            else:
                seen["waiting"].append(url)
        except WorkloadGone as exc:
            logger.warning("post-merge check: %s cannot be judged: %s", url, exc)
            updates.update(health_result=HEALTH_FAILED, health_detail=f"cannot judge: {exc}"[:500], health_at=stamp)
            seen["failed"].append(url)
        except Exception as exc:  # noqa: BLE001 — one unreadable cluster must not stop the tick
            logger.warning("post-merge check: could not read the cluster for %s: %r", url, exc)
            updates["sync_state"] = f"unreadable: {exc}"[:500]
            seen["unreadable"].append(url)
        try:
            with conn:
                conn.execute(
                    f"UPDATE {RECORDS_TABLE} SET {', '.join(f'{key} = ?' for key in updates)} WHERE url = ?",
                    (*updates.values(), url),
                )
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record the health step for %s: %r", url, exc)
    return seen


#: Session KV's database, as ``session_kv_server.py`` reads the same variable.
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
SESSION_KV_DB_DEFAULT = "/var/lib/kube-agents/session/session_kv.db"
SWITCH = "POST_MERGE_CHECK"


def main(argv: list[str] | None = None, view: View = forge_view, kubectl_json: KubectlJson | None = None) -> int:
    """``post_merge_check.py notice|health``: one tick of the merge step, or of the apply step;
    prints what it saw as JSON.

    Reads nothing while the install's ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``.
    """
    import os
    import sys
    from contextlib import closing

    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["notice"], ["health"]):
        print("usage: post_merge_check.py notice|health", file=sys.stderr)
        return 2
    import kubeagents_switches

    if kubeagents_switches.switch(SWITCH) == kubeagents_switches.OFF:
        print(json.dumps({"switch": kubeagents_switches.OFF}))
        return 0
    path = os.environ.get(SESSION_KV_DB_ENV) or SESSION_KV_DB_DEFAULT
    with closing(sqlite3.connect(path, timeout=5.0)) as conn:
        if argv == ["notice"]:
            print(json.dumps(notice_merges(conn, view)))
        else:
            print(json.dumps(watch_health(conn, kubectl_json)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
