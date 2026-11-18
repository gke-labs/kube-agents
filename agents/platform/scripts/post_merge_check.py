"""The post-merge check's steps over Session KV's ``post_merge_records`` (design gke-labs/kube-agents#5054).

Step 2, notice the merge: :func:`notice_merges` reads each waiting record's
pull request from the forge, one read per record per tick and at most
:func:`reads_per_tick` records (:data:`READS_PER_TICK` unless the install sets
:data:`READS_PER_TICK_ENV`), and writes what it found back to the row:

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

Steps 3 and 4, wait until applied and watch health (``post_merge_check.py
health``, one tick, also unscheduled): :func:`watch_health` reads each merged
record's cluster through the shell sandbox. The GitOps reconcilers that deliver the repository into the
record's namespace are read against the merge commit (``reconciler_sync``),
asking the broker (``revision-contains``) whether a later applied commit
contains it; with none, or when the broker cannot say, the object the fix adds
is observed directly. Once applied, the pods' restart counts are the baseline,
and every run reads the workload again until :data:`HEALTH_SAMPLES` healthy
readings, the last past the window; any reading that is not healthy fails it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("post_merge_check")

RECORDS_TABLE = "post_merge_records"
#: The most records one tick reads, per step, unless :data:`READS_PER_TICK_ENV`
#: says otherwise.
READS_PER_TICK = 20
#: The agent container's variable (``spec.deployment.env``) that sets the per-step
#: limit: a whole number from 1 to :data:`READS_PER_TICK_MAX`; anything else is the default.
READS_PER_TICK_ENV = "POST_MERGE_READS_PER_TICK"
READS_PER_TICK_MAX = 500
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
# Steps 3 and 4: wait until applied, then the health window
# --------------------------------------------------------------------------

#: The health window after the GitOps tool applies the merge, per kind of fix
#: (design gke-labs/kube-agents#5054, "Settings per fix").
HEALTH_WINDOW = {"no-pdb": timedelta(minutes=5)}
#: However short a kind's window, the workload must stay healthy at least this
#: long after the GitOps tool reported the fix applied: Argo CD can read synced
#: and healthy, then a pod fails a minute later.
HEALTHY_AFTER_SYNC = timedelta(seconds=60)
#: Healthy readings needed before a pass, each on its own run, the last one
#: after the window: one reading at the end would miss a pod that went red and
#: recovered inside the window. Nothing else spaces them; how far apart they
#: fall is the schedule's cadence.
HEALTH_SAMPLES = 2
#: How long past the window a record may go without a healthy reading (reads
#: failing, or a rollout or scale still under way) before it fails as not settled.
HEALTH_SETTLE_LIMIT = timedelta(minutes=30)
#: ``health_detail``'s prefix while the workload is still rolling out or scaling.
NOT_SETTLED = "not settled: "
#: ``health_detail``'s prefix while the window is being sampled.
SAMPLED = "healthy samples: "
HEALTH_PASSED = "passed"
HEALTH_FAILED = "failed"
OOM_KILLED = "OOMKilled"
#: The reconciler objects that can deliver a merge, as kubectl names them.
ARGO_APPLICATIONS = "applications.argoproj.io"
FLUX_KUSTOMIZATIONS = "kustomizations.kustomize.toolkit.fluxcd.io"
FLUX_GIT_REPOSITORIES = "gitrepositories.source.toolkit.fluxcd.io"
#: Counted only when its source is the fix's repository and its dir holds a changed file.
CONFIGSYNC_ROOTSYNCS = "rootsyncs.configsync.gke.io"
CONFIGSYNC_REPOSYNCS = "reposyncs.configsync.gke.io"
#: The lists one tick reads per cluster.
RECONCILER_RESOURCES = (ARGO_APPLICATIONS, FLUX_KUSTOMIZATIONS, FLUX_GIT_REPOSITORIES, CONFIGSYNC_ROOTSYNCS, CONFIGSYNC_REPOSYNCS)
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

#: Merged records the step has not judged, least recently read first.
HEALTH_SQL = (
    f"SELECT url, repo, number, kind, project, location, cluster, namespace, object, merge_commit, applied_at, "
    f"health_baseline, health_detail, changed_paths FROM {RECORDS_TABLE} WHERE outcome IS NULL "
    "AND merged_at IS NOT NULL AND merge_commit IS NOT NULL AND health_result IS NULL "
    "ORDER BY health_checked_at IS NOT NULL, health_checked_at, merged_at LIMIT ?"
)
HEALTH_COLUMNS = ("url", "repo", "number", "kind", "project", "location", "cluster", "namespace", "object",
                  "merge_commit", "applied_at", "health_baseline", "health_detail", "changed_paths")


class ClusterReadError(RuntimeError):
    """A cluster read that did not answer."""


class ScopeRefused(Exception):
    """The install's declared scope leaves the record's cluster out, or cannot say: nothing is read."""


#: ``post_merge_scope.UNKNOWN_PREFIX``, repeated so this module does not import it.
SCOPE_UNKNOWN_PREFIX = "unknown: "


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


#: ``paths(repo, number)`` -> the files the pull request changed; None when unknown.
Paths = Callable[[str, int, "str | None"], "list[str] | None"] | None
DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
DIFF_RENAME_RE = re.compile(r"^rename (?:from|to) (.+)$")
DIFF_PATH_RE = re.compile(r'^(?:\+\+\+|---) (?:"([ab]/(?:[^"\\]|\\.)*)"|([ab]/.*?))\t?$')


#: How a forge adapter says the diff it returned is not the whole change (GitLab's fallback).
DIFF_CUT_RE = re.compile(r"^# diff (?:may be )?cut short", re.MULTILINE)
C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
C_QUOTED_RE = re.compile(r"\\([0-7]{3}|.)")


def _unquote(text: str) -> str:
    """git's C-quoted path body as text: octal escapes are bytes, and raw UTF-8 is left as it is."""
    def byte(match: re.Match) -> str:
        code = match.group(1)
        value = int(code, 8) if len(code) == 3 else C_ESCAPES.get(code)
        return match.group(0) if value is None else chr(value)

    try:
        return C_QUOTED_RE.sub(byte, text).encode("latin-1").decode("utf-8")
    except UnicodeError:
        # Raw UTF-8 beside the escapes: decode the escapes as bytes one run at a time.
        out = bytearray()
        for part in re.split(r"((?:\\(?:[0-7]{3}|.))+)", text):
            out += C_QUOTED_RE.sub(byte, part).encode("latin-1") if part.startswith("\\") else part.encode("utf-8")
        return out.decode("utf-8", "replace")


def _names(text: str, name: str) -> bool:
    """Whether ``text`` holds ``name`` as a whole word (``checkout`` in ``app: checkout`` or ``checkout-pdb``)."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text) is not None


def diff_paths(diff: str, name: str | None = None) -> list[str]:
    """The files a unified diff changes, old and new names both, in order of appearance.

    With ``name``, only the files whose section of the diff (its paths or its
    changed lines) holds ``name`` as a whole word: the files that carry the
    workload, so a reconciler that delivers some other file of the same pull
    request is not taken to deliver the fix.

    Only a file's header is read (from ``diff --git`` to its first ``@@``), so
    a content line that happens to start ``--- a/`` is not taken for a path:
    its ``---``/``+++`` names (C-quoted ones unquoted), a rename's names, and
    the ``diff --git`` names when they hold no space, which covers a binary
    file or a forge's collapsed entry with no ``---``/``+++`` lines.
    """
    found: list[str] = []
    section: list[str] = []
    mentioned = name is None
    in_header = False

    def add(path: str) -> None:
        if path and path not in section:
            section.append(path)

    def close() -> None:
        if mentioned or any(_names(path, name) for path in section):
            found.extend(path for path in section if path not in found)
        section.clear()

    # Split on "\n" alone, as git does: splitlines() also breaks on a bare "\r" or U+2028
    # inside a changed line, which could start a header that is not one.
    for line in (raw.removesuffix("\r") for raw in diff.split("\n")):
        if line.startswith("diff --git "):
            close()
            mentioned = name is None
            in_header = True
            simple = DIFF_GIT_RE.match(line)
            if simple:
                add(simple.group(1))
                add(simple.group(2))
            continue
        if line.startswith("@@"):
            in_header = False
            continue
        if not in_header:
            if not mentioned and line[:1] in "+-" and _names(line[1:], name):
                mentioned = True
            continue
        match = DIFF_PATH_RE.match(line)
        if match:
            add((_unquote(match.group(1)) if match.group(1) is not None else match.group(2))[2:])
            continue
        renamed = DIFF_RENAME_RE.match(line)
        if renamed:
            renamed_to = renamed.group(1)
            add(_unquote(renamed_to[1:-1]) if renamed_to.startswith('"') and renamed_to.endswith('"') else renamed_to)
    close()
    return found


def forge_paths(repo: str, number: int, name: str | None = None) -> list[str] | None:
    """The files pull request ``number`` changed that name ``name`` (:func:`diff_paths`), from the
    forge's diff; None when it cannot say or no file names it."""
    try:
        import forge

        answer = forge.call(
            "proposal-view", {"number": number, "diff": True}, repo, retry_transient=True,
            timeout_s=VIEW_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — unknown paths read as unknown, never as none
        logger.warning("post-merge check: could not read the files of %s#%s: %r", repo, number, exc)
        return None
    diff = answer.get("diff") if isinstance(answer, dict) else None
    if not isinstance(diff, str) or DIFF_CUT_RE.search(diff):
        # A cut diff lists only some of the files, and kept as the whole list it would
        # narrow the match to the reconcilers of those files for good.
        return None
    try:
        return diff_paths(diff, name) or None
    except ValueError as exc:  # an escape no path decodes (\\400): unknown, never a crash
        logger.warning("post-merge check: could not read the files of %s#%s: %r", repo, number, exc)
        return None


def _dir_key(value: object) -> str:
    """A repository directory as a prefix, its empty and ``.`` segments dropped (``/./a//b/.`` is ``a/b``);
    ``""`` is the root."""
    return "/".join(part for part in str(value or "").strip().split("/") if part not in ("", "."))


def _holds(directory: object, paths: list[str]) -> bool:
    """Whether any of ``paths`` lies in ``directory`` (the root holds every path)."""
    prefix = _dir_key(directory)
    return not prefix or any(path == prefix or path.startswith(prefix + "/") for path in paths)


def reconcilers_for(repo: str, namespace: str, lists: dict[str, dict],
                    paths: list[str] | Callable[[], list[str] | None] | None = None) -> list[dict]:
    """The reconciler objects on this cluster that deliver the fix in ``repo`` to ``namespace``.

    An Argo CD Application counts when one of its Git sources is ``repo``, it
    deploys to this cluster and its destination namespace is ``namespace``; a
    Flux Kustomization when its GitRepository is ``repo`` and its
    ``targetNamespace`` is ``namespace``; a Config Sync RepoSync in ``namespace``
    with ``repo`` as its source. With ``paths`` (the files the fix changed),
    those whose path (the Argo source's ``path``, the Kustomization's
    ``path``, the RepoSync's ``dir``) holds one of them are preferred; when
    none does, all of them still count, since a kustomization can reach files
    outside its own directory. So a path narrows the match and never empties it.
    The cost: a match that reaches a changed file only through a ``../base``
    is dropped when another match holds it directly, so the record can read
    applied once that one has synced.

    A Config Sync RootSync names no namespace: it counts when its source is a
    git ``repo`` and its ``dir`` holds one of ``paths``, which ``watch_health``
    narrows to the files that name the record's workload, so a RootSync that
    delivers some other file of the pull request never counts. Without ``paths`` no
    RootSync counts, and with nothing else delivering the fix it is observed
    directly. ``lists`` maps the resource names above to their ``kubectl get
    -A -o json`` answers. ``paths`` may be a function, called only when they
    can change the answer (a RootSync of ``repo``, or more than one namespace
    match), so a fix delivered by one Application costs no diff read.
    """
    import clean_merges

    repo_key = clean_merges.repo_key
    key = repo_key(repo)
    if not key:
        return []
    scoped: list[tuple[dict, list[object]]] = []
    for app in lists.get(ARGO_APPLICATIONS, {}).get("items", []):
        spec = app.get("spec") or {}
        destination = spec.get("destination") or {}
        if str(destination.get("server") or "") not in ARGO_IN_CLUSTER[0] or str(destination.get("name") or "") not in ARGO_IN_CLUSTER[1]:
            continue
        sources = ([spec["source"]] if isinstance(spec.get("source"), dict) else []) + list(spec.get("sources") or [])
        matching = [source for source in sources if isinstance(source, dict) and repo_key(source.get("repoURL")) == key]
        if destination.get("namespace") == namespace and matching:
            scoped.append((app, [source.get("path") for source in matching]))
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
            scoped.append((kustomization, [spec.get("path")]))
    for sync in lists.get(CONFIGSYNC_REPOSYNCS, {}).get("items", []):
        git = (sync.get("spec") or {}).get("git") or {}
        if (sync.get("metadata") or {}).get("namespace") == namespace and repo_key(git.get("repo")) == key:
            scoped.append((sync, [git.get("dir")]))
    found = [obj for obj, _ in scoped]
    roots = [
        sync for sync in lists.get(CONFIGSYNC_ROOTSYNCS, {}).get("items", [])
        if str((sync.get("spec") or {}).get("sourceType") or "git") == "git"
        and repo_key(((sync.get("spec") or {}).get("git") or {}).get("repo")) == key
    ]
    if callable(paths):
        paths = paths() if roots or len(scoped) > 1 else None
    if paths:
        within = [obj for obj, directories in scoped if any(_holds(d, paths) for d in directories)]
        found = (within or found) + [
            sync for sync in roots if _holds(((sync.get("spec") or {}).get("git") or {}).get("dir"), paths)
        ]
    return found


def combined_sync(objects: list[dict], merge_commit: str, contains: Callable[[str, str], bool | None] | None = None) -> tuple[str, str]:
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
    """``{"<pod uid>/<container>": restartCount}`` for every container of every pod, sidecars and init containers included."""
    out = {}
    for pod in pods.get("items", []):
        uid = (pod.get("metadata") or {}).get("uid") or (pod.get("metadata") or {}).get("name") or ""
        for status in _container_statuses(pod):
            out[f"{uid}/{status.get('name')}"] = int(status.get("restartCount") or 0)
    return out


def _container_statuses(pod: dict) -> list[dict]:
    """Every container's status: the app containers, and the init containers, where a native sidecar reports."""
    status = pod.get("status") or {}
    return list(status.get("containerStatuses") or []) + list(status.get("initContainerStatuses") or [])


def _rolled_out(workload: dict) -> bool:
    """Whether the controller has finished changing the pod set: the spec is observed, every pod is at the
    current template or revision, and the count matches the spec. Readiness is not part of it; the pods are
    judged one by one (:func:`judge_health`), so a pod that goes NotReady is red rather than "rolling out"."""
    spec, status, meta = workload.get("spec") or {}, workload.get("status") or {}, workload.get("metadata") or {}
    replicas = int(spec.get("replicas", 1) or 0)
    if int(status.get("observedGeneration") or 0) < int(meta.get("generation") or 0):
        return False
    if int(status.get("replicas", replicas) or 0) != replicas:
        return False
    if workload.get("kind") == "StatefulSet":
        return not (status.get("updateRevision") and status.get("currentRevision") != status.get("updateRevision"))
    return int(status.get("updatedReplicas") or 0) == replicas


def _owned_pods(workload: dict, pods: dict) -> list[dict]:
    # A pod being deleted (a drain, a scale-down) is no longer the workload's to judge.
    return [pod for pod in pods.get("items", [])
            if _owned_by(pod, workload) and not (pod.get("metadata") or {}).get("deletionTimestamp")]


def _is_ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (pod.get("status") or {}).get("conditions") or [])


def _owned_by(pod: dict, workload: dict) -> bool:
    """Whether ``pod`` belongs to ``workload``: directly (a StatefulSet) or through a ReplicaSet named after it."""
    name = (workload.get("metadata") or {}).get("name") or ""
    for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
        if owner.get("kind") == workload.get("kind") and owner.get("name") == name:
            return True
        template_hash = ((pod.get("metadata") or {}).get("labels") or {}).get("pod-template-hash")
        if owner.get("kind") == "ReplicaSet" and template_hash and owner.get("name") == f"{name}-{template_hash}":
            return True
    return False


def _healthy_samples(detail: object) -> int:
    """How many healthy readings ``health_detail`` records so far; 0 after a failed read reset it."""
    text = str(detail or "")
    if not text.startswith(SAMPLED):
        return 0
    count = text[len(SAMPLED):].split(",", 1)[0]
    return int(count) if count.isdigit() else 0


#: :func:`assess_health`'s verdicts for one reading.
HEALTHY, UNSETTLED, RED = "healthy", "unsettled", "red"


def _known_pods(baseline: dict[str, int]) -> set[str]:
    """The pods the baseline holds: there at the apply, or seen Ready on a later reading."""
    return {key.split("/", 1)[0] for key in baseline}


def assess_health(workload: dict, pods: dict, baseline: dict[str, int], since: datetime) -> tuple[str, str]:
    """``(verdict, detail)`` for one reading: :data:`RED`, :data:`UNSETTLED` or :data:`HEALTHY`.

    Red comes first, so nothing settling can hide it: a known pod (in the
    baseline) that is not Ready, or any container, sidecars and init
    containers included, that restarted past the baseline or was OOMKilled
    since ``since``. Then unsettled: a rollout or scale under way, or a pod not
    yet known that is still starting (not Ready, nothing restarted). Healthy
    needs every owned pod Ready.
    """
    items = _owned_pods(workload, pods)
    known = _known_pods(baseline)
    for pod in items:
        meta = pod.get("metadata") or {}
        name = meta.get("name") or ""
        if (meta.get("uid") or name) in known and not _is_ready(pod):
            return RED, f"pod {name} is not Ready"
        for container in _container_statuses(pod):
            key = f"{meta.get('uid') or name}/{container.get('name')}"
            if int(container.get("restartCount") or 0) > baseline.get(key, 0):
                return RED, f"container {container.get('name')} in pod {name} restarted"
            for current, state in ((False, container.get("lastState") or {}), (True, container.get("state") or {})):
                ended = state.get("terminated") or {}
                finished = _parse_time(ended.get("finishedAt"))
                # A past termination with no time cannot be placed after the apply; a current one is red now.
                if ended.get("reason") == OOM_KILLED and (current if finished is None else finished >= since):
                    return RED, f"container {container.get('name')} in pod {name} was OOMKilled"
    if not _rolled_out(workload):
        return UNSETTLED, "the rollout is not complete"
    for pod in items:
        if not _is_ready(pod):
            return UNSETTLED, f"pod {(pod.get('metadata') or {}).get('name') or ''} is starting"
    if not items:
        return RED, "no pods belong to the workload"
    return HEALTHY, "rolled out, every pod Ready, no restarts"


def judge_health(workload: dict, pods: dict, baseline: dict[str, int], since: datetime) -> tuple[bool, str]:
    """``(passed, detail)``: :func:`assess_health` as a pass or not; unsettled is not a pass."""
    verdict, detail = assess_health(workload, pods, baseline, since)
    return verdict == HEALTHY, detail


def _join_baseline(baseline: dict[str, int], workload: dict, pods: dict) -> dict[str, int]:
    """``baseline`` plus every owned pod now Ready that it did not hold, at its current restart counts: once
    seen Ready, a pod that came after the apply is judged like one that was there (NotReady is red)."""
    known = _known_pods(baseline)
    joined = dict(baseline)
    for pod in _owned_pods(workload, pods):
        meta = pod.get("metadata") or {}
        if (meta.get("uid") or meta.get("name")) not in known and _is_ready(pod):
            joined.update(restart_baseline({"items": [pod]}))
    return joined


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
        return False, f"a {record['kind']} fix cannot be observed directly"
    labels = (((workload.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
    budgets = kubectl_json(record, ["get", resource, "-n", namespace, "-o", "json"])
    for budget in budgets.get("items", []):
        if selects((budget.get("spec") or {}).get("selector") or {}, labels):
            return True, f"budget {budget['metadata']['name']} selects the workload"
    return False, "no budget selects the workload yet"
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

    def __init__(self, kubeconfigs: dict[tuple[str, str, str], str] | None = None) -> None:
        # Shareable with post_merge_scope.sandbox_recheck, so a cluster's credentials are fetched once per run.
        self.kubeconfigs: dict[tuple[str, str, str], str] = {} if kubeconfigs is None else kubeconfigs

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


def watch_health(
    conn: sqlite3.Connection,
    kubectl_json: KubectlJson | None = None,
    now: datetime | None = None,
    limit: int = READS_PER_TICK,
    contains: Contains = forge_contains,
    paths: Paths = None,
    scope_refusal: Callable[[dict], str | None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> dict[str, list[str]]:
    """Steps 3 and 4 for each merged record: wait for the apply, then hold the health window.

    ``stop()`` true (the cron run's time budget, ``post_merge_outcome.RUN_BUDGET``)
    starts no further record: the rest wait for the next run, least recently read
    first as always, and the count of them goes under ``not_reached``.

    ``scope_refusal(record)`` (``post_merge_scope.scope_refusal_for``) names
    why the install's declared scope leaves the record's cluster out, or None.
    A refused record is not read at all, only stamped, and is returned under
    ``out_of_scope``, or under ``scope_unknown`` when the scope cannot say
    (``post_merge_scope.UNKNOWN_PREFIX``, also used when judging it failed).

    Not yet applied: read the reconcilers on the record's cluster that deliver
    its repository into its namespace (:func:`reconcilers_for`) against the
    merge commit, asking the broker whether a later applied commit contains it
    (``contains``); with none, observe the fix's object directly
    (:func:`observed_directly`). The reading goes in ``sync_state``. Applied:
    ``applied_at`` and the pods' restart counts (``health_baseline``). Each
    later run: read the workload (:func:`judge_health`); not healthy fails it
    at once, a rollout or scale under way waits with the count reset, and :data:`HEALTH_SAMPLES` healthy readings with the last past the
    window (:data:`HEALTH_WINDOW`, at least :data:`HEALTHY_AFTER_SYNC`) pass
    it, into ``health_result`` and ``health_detail``. A workload that is gone or cannot be judged fails
    with the reason; any other read failure is kept in ``health_detail`` (or
    ``sync_state`` before the apply), resets the count and is retried, until
    :data:`HEALTH_SETTLE_LIMIT` past the window with no healthy reading fails it. Every read stamps
    ``health_checked_at``, which orders the next tick, so no record starves the
    rest. Everything lives on the row, so a restart loses nothing. ``paths``
    (default :func:`forge_paths`) reads the files the pull request changed,
    only when :func:`reconcilers_for` needs them; a list is kept in
    ``changed_paths`` and never read again, and an unknown one is said in
    ``sync_state`` and read again next tick. Returns urls under ``applied``,
    ``waiting``, ``passed``, ``failed``, ``unreadable``, ``out_of_scope`` and
    ``scope_unknown``.
    """
    paths = paths or forge_paths
    import reconciler_sync

    kubectl_json = kubectl_json or SandboxKubectl()
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {
        "applied": [], "waiting": [], "passed": [], "failed": [], "unreadable": [], "out_of_scope": [],
        "scope_unknown": [],
    }
    # A cluster's reconciler lists, or the error reading them, so a failed read is not repeated per record.
    lists_by_cluster: dict[tuple, dict | Exception] = {}
    answers: dict[tuple, bool | None] = {}
    reads = 0
    # Every candidate, since a refused record costs no read: only records that are read count
    # toward ``limit``, so refused ones never hold a tick's slots, however many there are.
    for values in conn.execute(HEALTH_SQL, (-1,)).fetchall():
        if reads >= limit:
            break
        if stop is not None and stop():
            seen["not_reached"] = seen.get("not_reached", []) + [values[0]]
            continue
        record = dict(zip(HEALTH_COLUMNS, values))
        url = record["url"]
        updates: dict[str, object] = {"health_checked_at": stamp}
        refused = None
        if scope_refusal is not None and all(cluster_coordinates(record)) and record["object"]:
            try:
                refused = scope_refusal(record)
            except Exception as exc:  # noqa: BLE001 — a record whose scope cannot be judged is not read
                logger.warning("post-merge check: could not judge the scope of %s: %r", url, exc)
                refused = f"{SCOPE_UNKNOWN_PREFIX}the scope could not be judged: {exc!r}"
        try:
            if refused:
                raise ScopeRefused(refused)
            cluster = cluster_coordinates(record)
            if not (all(cluster) and record["object"]):
                raise ClusterReadError("the record names no cluster or workload to read")
            reads += 1
            if record["applied_at"] is None:
                if cluster not in lists_by_cluster:
                    try:
                        lists_by_cluster[cluster] = {
                            resource: kubectl_json(record, ["get", resource, "-A", "-o", "json", "--chunk-size=500"])
                            for resource in RECONCILER_RESOURCES
                        }
                    except Exception as exc:
                        lists_by_cluster[cluster] = exc
                lists = lists_by_cluster[cluster]
                if isinstance(lists, Exception):
                    raise lists

                def asked(ancestor: str, revision: str, repo: str = record["repo"]) -> bool | None:
                    if (repo, ancestor, revision) not in answers:
                        answers[(repo, ancestor, revision)] = contains(repo, ancestor, revision)
                    return answers[(repo, ancestor, revision)]

                unknown: list[bool] = []

                def changed(record: dict = record, updates: dict = updates, unknown: list = unknown) -> list[str] | None:
                    # A merged pull request's files never change: read them once and keep them on the row.
                    if record["changed_paths"]:
                        return json.loads(record["changed_paths"])
                    workload_name = str(record["object"] or "").rpartition("/")[2]
                    found = paths(record["repo"], record["number"], workload_name) if workload_name else None
                    if found:
                        updates["changed_paths"] = json.dumps(found)
                    else:
                        unknown.append(True)
                    return found

                objects = reconcilers_for(record["repo"], record["namespace"] or "default", lists, changed)
                workload, pods = _read_workload(kubectl_json, record)
                if objects:
                    state, detail = combined_sync(objects, record["merge_commit"], asked)
                    applied = state == reconciler_sync.APPLIED
                    if state == reconciler_sync.UNCONFIRMED:
                        # The broker could not say whether the applied commit contains the merge (an
                        # abbreviated or SHA-256 merge commit, a fetch it refused): look for the fix itself.
                        applied, direct = observed_directly(kubectl_json, record, workload)
                        if applied:
                            state, detail = OBSERVED, f"the reconcilers' commit could not be confirmed; {direct}"
                else:
                    applied, direct = observed_directly(kubectl_json, record, workload)
                    state = OBSERVED if applied else reconciler_sync.PENDING
                    detail = f"no reconciler targets {record['namespace'] or 'default'}; {direct}"
                if unknown:
                    detail = f"the workload's files unknown: no RootSync counts and no path narrows the match; {detail}"
                updates["sync_state"] = f"{state}: {detail}"[:500]
                if applied:
                    updates["applied_at"] = stamp
                    updates["health_baseline"] = json.dumps(restart_baseline(pods), sort_keys=True)
                    bucket = "applied"
                else:
                    bucket = "waiting"
            else:
                applied_at = _parse_time(record["applied_at"])
                if applied_at is None:
                    raise WorkloadGone("the apply time is unreadable")
                window = max(HEALTH_WINDOW.get(record["kind"] or "", max(HEALTH_WINDOW.values())), HEALTHY_AFTER_SYNC)
                last = record["health_detail"]
                if last and not _healthy_samples(last) and now - applied_at >= window + HEALTH_SETTLE_LIMIT:
                    raise WorkloadGone(f"no healthy reading by {int((window + HEALTH_SETTLE_LIMIT).total_seconds() // 60)} "
                                       f"minutes after the apply; last: {last}")
                workload, pods = _read_workload(kubectl_json, record)
                try:
                    baseline = json.loads(record["health_baseline"] or "{}")
                except ValueError:
                    baseline = None
                if not isinstance(baseline, dict) or not all(isinstance(v, int) for v in baseline.values()):
                    raise WorkloadGone("the restart baseline is unreadable")
                verdict, detail = assess_health(workload, pods, baseline, applied_at)
                samples = _healthy_samples(last) + 1
                if verdict != RED:
                    joined = _join_baseline(baseline, workload, pods)
                    if joined != baseline:
                        updates["health_baseline"] = json.dumps(joined, sort_keys=True)
                if verdict == UNSETTLED:
                    # A rollout or scale under way (an HPA, someone else's change) is not settled yet, not red;
                    # it resets the count, and HEALTH_SETTLE_LIMIT ends a wait that never settles.
                    updates["health_detail"] = f"{NOT_SETTLED}{detail}"
                    bucket = "waiting"
                elif verdict == RED:
                    # Any red reading after the apply fails the window, even one that would recover.
                    updates.update(health_result=HEALTH_FAILED, health_detail=detail, health_at=stamp)
                    bucket = "failed"
                elif now - applied_at >= window and samples >= HEALTH_SAMPLES:
                    updates.update(health_result=HEALTH_PASSED, health_detail=f"{detail} ({samples} readings)",
                                   health_at=stamp)
                    bucket = "passed"
                else:
                    updates["health_detail"] = f"{SAMPLED}{samples}, last {stamp}"
                    bucket = "waiting"
        except ScopeRefused as exc:
            # Not read; the reason goes on the row, and the stamp moves it to the back of the queue.
            unknown = str(exc).startswith(SCOPE_UNKNOWN_PREFIX)
            bucket = "scope_unknown" if unknown else "out_of_scope"
            reason = str(exc)[len(SCOPE_UNKNOWN_PREFIX):] if unknown else str(exc)
            updates["sync_state"] = f"{'scope unknown' if unknown else 'out of scope'}: {reason}"[:500]
        except WorkloadGone as exc:
            logger.warning("post-merge check: %s cannot be judged: %s", url, exc)
            updates.update(health_result=HEALTH_FAILED, health_detail=f"cannot judge: {exc}"[:500], health_at=stamp)
            bucket = "failed"
        except Exception as exc:  # noqa: BLE001 — one unreadable cluster must not stop the tick
            logger.warning("post-merge check: could not read the cluster for %s: %r", url, exc)
            updates["sync_state" if record["applied_at"] is None else "health_detail"] = f"unreadable: {exc}"[:500]
            bucket = "unreadable"
        try:
            with conn:
                conn.execute(
                    f"UPDATE {RECORDS_TABLE} SET {', '.join(f'{key} = ?' for key in updates)} WHERE url = ?",
                    (*updates.values(), url),
                )
        except sqlite3.Error as exc:
            # Nothing was written, so the tick reports what the row still says: not read.
            logger.warning("post-merge check: could not record the health step for %s: %r", url, exc)
            bucket = "unreadable"
        seen[bucket].append(url)
    return seen


#: Session KV's database, as ``session_kv_server.py`` reads the same variable.
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
SESSION_KV_DB_DEFAULT = "/var/lib/kube-agents/session/session_kv.db"
SWITCH = "POST_MERGE_CHECK"


def reads_per_tick() -> int:
    """:data:`READS_PER_TICK_ENV` as a whole number from 1 to :data:`READS_PER_TICK_MAX`, else :data:`READS_PER_TICK`."""
    raw = os.environ.get(READS_PER_TICK_ENV, "").strip()
    if not raw:
        return READS_PER_TICK
    # At most four digits before int(): Python refuses a digit string past
    # 4300 characters with a ValueError, and no valid value needs more than three.
    value = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 4 else 0
    if not 1 <= value <= READS_PER_TICK_MAX:
        logger.warning("post-merge check: %s=%r is not a whole number from 1 to %d; using %d",
                       READS_PER_TICK_ENV, raw, READS_PER_TICK_MAX, READS_PER_TICK)
        return READS_PER_TICK
    return value


#: The cron run's summary, in the agent's shipped logs (``script_log``).
LOG_FILE = "post_merge_check.log"


def main(argv: list[str] | None = None, view: View = forge_view, kubectl_json: KubectlJson | None = None,
         **tick_overrides) -> int:
    """With no arguments, the ``post-merge-check`` cron job: one ``post_merge_outcome.tick``, printing
    nothing to stdout. The summary goes to ``logs/post_merge_check.log``. A run in which any record or
    step errored exits 1 with one line naming them on stderr, kept in the run's saved output, and the
    scheduler posts its failure line to chat; a failure already reported in the last day
    (``post_merge_outcome.failures_to_report``) does not exit 1 again. A clean run exits 0 and the
    scheduler relays nothing. ``notice``, ``health`` and ``report``
    run one step by hand (``report`` in the switch's mode, through ``post_merge_report``) and print
    what it saw as JSON.

    Reads nothing while the install's ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``.
    """
    import sys
    from contextlib import closing

    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["notice"], ["health"], ["report"]):
        print("usage: post_merge_check.py [notice|health|report]", file=sys.stderr)
        return 2
    import kubeagents_switches

    mode = kubeagents_switches.switch(SWITCH)
    if mode == kubeagents_switches.OFF:
        if argv:
            print(json.dumps({"switch": kubeagents_switches.OFF}))
        return 0
    path = os.environ.get(SESSION_KV_DB_ENV) or SESSION_KV_DB_DEFAULT
    with closing(sqlite3.connect(path, timeout=5.0)) as conn:
        if argv == ["notice"]:
            print(json.dumps(notice_merges(conn, view, limit=reads_per_tick())))
        elif argv == ["health"]:
            import post_merge_scope

            try:
                refusal = post_merge_scope.scope_refusal_for(post_merge_scope.install_scope())
            except Exception as exc:  # noqa: BLE001 — an unknown scope reads no cluster
                print(json.dumps({"scope": "unreadable", "error": str(exc)[:300]}))
                return 1
            print(json.dumps(watch_health(conn, kubectl_json, limit=reads_per_tick(), scope_refusal=refusal)))
        elif argv == ["report"]:
            import post_merge_report

            print(json.dumps(post_merge_report.report_outcomes(conn, mode)))
        else:
            import post_merge_outcome

            seen = post_merge_outcome.tick(conn, mode, view=view, kubectl_json=kubectl_json, **tick_overrides)
            summary = f"post-merge check ({mode}): {json.dumps(seen)}"
            early = post_merge_outcome.stopped_early(seen)
            try:
                import script_log

                script_log.append(LOG_FILE, [summary] + ([early] if early else []))
            except Exception as exc:  # noqa: BLE001 — the log is best effort
                print(f"post-merge check: could not write {LOG_FILE}: {exc!r}", file=sys.stderr)
            failures = post_merge_outcome.failures_to_report(post_merge_outcome.errored(seen),
                                                             datetime.now(timezone.utc))
            if failures:
                print(f"post-merge check ({mode}): {len(failures)} failed: {', '.join(failures)[:1500]}; "
                      f"the run's summary is in logs/{LOG_FILE}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
