#!/usr/bin/env python3
"""reconciler_sync.py -- has the GitOps reconciler applied a merge commit yet?

A merged fix pull request is not a fixed cluster until the reconciler applies
the merge commit. Checking the cluster before then reads the old state and
reports the fix as broken: on 2026-11-05 a dev install said Argo CD had
deleted a PodDisruptionBudget because it looked before Argo synced the merge.
This module answers the question that comes first: given the reconciler
object that delivers the fix and the proposal's ``mergeCommit``, has that
commit been applied? Nothing calls it yet; the post-merge check will.

It reads one object, as ``kubectl get -o json`` returns it, and decides
nothing about the workload; health is the next check's job.

- Argo CD ``Application`` (``argoproj.io``): ``status.sync.status`` is
  ``Synced`` at ``status.sync.revision`` (``status.sync.revisions`` for a
  multi-source Application, where any Git source may carry the fix).
- Flux ``Kustomization`` (``kustomize.toolkit.fluxcd.io``) built from a
  ``GitRepository``: ``status.lastAppliedRevision``, in the ``main@sha1:<sha>``
  form or the older ``main/<sha>`` form. A health check that fails after the
  merge commit was applied still counts as applied.
- Config Sync ``RootSync`` / ``RepoSync`` (``configsync.gke.io``) with a Git
  source: ``status.lastSyncedCommit``.

Flux ``HelmRelease`` is out of scope: the operator grants no read of it. An
OCI or Helm source, and a revision that is not a Git commit, read ``unknown``.

Reconcilers track a branch, not a commit, so the applied revision can be a
later commit that contains the merge. Whether it does is a question for the
forge, which this module does not call: the caller passes ``is_ancestor``,
which answers True, False, or None when it cannot tell. Without an answer, a
revision that is not the merge commit reads ``unconfirmed`` rather than
``pending``, because the merge may already be in it.

States, in the order they are decided:

``applied``      the reconciler applied the merge commit, or a commit
                 ``is_ancestor`` says contains it.
``error``        the reconciler reports an error that keeps the merge from
                 applying: a failed sync of the merge commit (or of a commit
                 containing it, or of a revision it does not name), an Argo
                 error condition, a stalled reconciler. One reading, not a
                 verdict: the caller decides how long an error must persist.
``pending``      not applied yet and nothing is wrong with the merge: the sync
                 is in flight, the reconciler is suspended or syncs by hand
                 (``detail`` says which), or ``is_ancestor`` said the applied
                 revision is older. An error on an older commit is not the
                 merge's error.
``unconfirmed``  applied at another commit and ``is_ancestor`` could not say
                 whether it contains the merge.
``unknown``      the object has no status this reader understands.

Usage::

    kubectl -n argocd get application mcp-demo -o json \\
      | reconciler_sync.py --merge-commit 3d1a1b0bf1b13e4e40a0301114750aadf81bae38
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

APPLIED = "applied"
ERROR = "error"
PENDING = "pending"
UNCONFIRMED = "unconfirmed"
UNKNOWN = "unknown"

ARGOCD = "argocd"
FLUX = "flux"
CONFIGSYNC = "configsync"

ARGO_GROUP = "argoproj.io"
FLUX_KUSTOMIZE_GROUP = "kustomize.toolkit.fluxcd.io"
CONFIGSYNC_GROUP = "configsync.gke.io"

# Argo names its error conditions with an `Error` suffix (ComparisonError,
# SyncError, InvalidSpecError, UnknownError); the `Warning` ones do not stop a sync.
ARGO_ERROR_CONDITION_SUFFIX = "Error"
ARGO_FAILED_PHASES = frozenset({"Failed", "Error"})
ARGO_RUNNING_PHASES = frozenset({"Running", "Terminating"})
ARGO_SYNCED = "Synced"

# Flux reasons on `Ready=False` that mean "working on it", not "failed".
FLUX_IN_PROGRESS_REASONS = frozenset({"Progressing", "DependencyNotReady", "ProgressingWithRetry"})
# The reason Flux gives when it applied the revision and the workload then
# failed its health check: applied, with health left to the next check.
FLUX_HEALTH_CHECK_FAILED = "HealthCheckFailed"
FLUX_GIT_SOURCE_KIND = "GitRepository"

CONFIGSYNC_GIT_SOURCE = "git"
# Config Sync's transient error code: a render still in progress, retried by
# itself. The same code fleet-audit's `check_configsync_reconciler_stalled`
# skips; that check judges any commit, this one only the merge's.
CONFIGSYNC_TRANSIENT_ERROR_CODES = frozenset({"2016"})

MAX_DETAIL_CHARS = 240
SHORT_SHA_LEN = 8

# A Git object id: SHA-1, or SHA-256 for a repository that uses it.
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# The commit at the end of a Flux Git revision: `main@sha1:<sha>`, `sha1:<sha>`,
# `main/<sha>` (Flux before 2.0), or a bare id. A `sha256:` revision is read as
# a commit only when the source is named a GitRepository: an OCI artifact's
# digest has the same shape.
_FLUX_REVISION_RE = re.compile(r"(?:^|@sha1:|^sha1:|/)([0-9a-f]{40})$")
_FLUX_SHA256_REVISION_RE = re.compile(r"(?:^|@sha256:|^sha256:|/)([0-9a-f]{64})$")

# Bitbucket Cloud's `mergeCommit` is a 12-character abbreviation, compared with
# a full id as a prefix (the proposal shape, test_providers_contract.py).
_ABBREVIATED_MERGE_RE = re.compile(r"^[0-9a-f]{12}$")

IsAncestor = Callable[[str, str], "bool | None"]


@dataclass(frozen=True)
class SyncReading:
    """One reading of one reconciler object against one merge commit."""

    state: str
    reconciler: str
    # The commit the reconciler last applied, or "" when it reports none.
    revision: str
    # One sentence for a log line or the eventual pull request comment.
    detail: str
    # Argo's own `status.health.status`, carried for the health check that
    # follows; "" for the other reconcilers.
    health: str = ""


def _short(sha: str) -> str:
    return sha[:SHORT_SHA_LEN] if sha else "no revision"


def _clip(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[: MAX_DETAIL_CHARS - 1] + "…"


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _condition(status: Mapping, kind: str) -> dict | None:
    for cond in _list(status.get("conditions")):
        if isinstance(cond, dict) and cond.get("type") == kind:
            return cond
    return None


def _commit(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _COMMIT_RE.match(text) else ""


def _flux_commit(value: Any, from_repository: bool) -> str:
    text = str(value or "").strip().lower()
    match = _FLUX_REVISION_RE.search(text) or (_FLUX_SHA256_REVISION_RE.search(text) if from_repository else None)
    return match.group(1) if match else ""


def _is_merge(revision: str, merge: str) -> bool:
    """Is `revision` the merge commit, which may be given abbreviated?"""
    return revision == merge or (len(merge) < len(revision) and revision.startswith(merge))


def _reaches(revision: str, merge: str, is_ancestor: IsAncestor | None) -> bool | None:
    """Does `revision` contain the merge commit? None when nobody can say."""
    if not revision:
        return None
    if _is_merge(revision, merge):
        return True
    return is_ancestor(merge, revision) if is_ancestor is not None else None


def _merges_error(revision: str, merge: str, is_ancestor: IsAncestor | None) -> bool:
    """Is a failure reported at `revision` this merge's failure?

    Yes at the merge commit or a commit containing it, and when the reconciler
    names no revision for it; no at an older or unrelated commit, and no when
    a named revision's ancestry is unanswered, so a failure left over from
    before the merge does not read as the merge's.
    """
    return not revision or _reaches(revision, merge, is_ancestor) is True


def _settled(
    reconciler: str, revisions: list[str], merge: str, is_ancestor: IsAncestor | None, health: str
) -> SyncReading | None:
    """`applied`, `pending` or `unconfirmed` for a settled revision; None when there is none."""
    revisions = [r for r in revisions if r]
    if not revisions:
        return None
    answers = []
    for rev in revisions:
        answer = _reaches(rev, merge, is_ancestor)
        if answer is True:
            what = "the merge commit" if _is_merge(rev, merge) else f"{_short(rev)}, which contains the merge commit"
            return SyncReading(APPLIED, reconciler, rev, f"applied {what} {_short(merge)}", health)
        answers.append(answer)
    shown = ", ".join(_short(r) for r in revisions)
    if all(a is False for a in answers):
        return SyncReading(
            PENDING, reconciler, revisions[0], f"still at {shown}, which does not contain the merge commit {_short(merge)}", health
        )
    return SyncReading(
        UNCONFIRMED,
        reconciler,
        revisions[0],
        f"applied {shown}, not the merge commit {_short(merge)}; whether it contains the merge was not checked",
        health,
    )


def _not_a_commit(reconciler: str, raw: Any, health: str = "") -> SyncReading:
    return SyncReading(UNKNOWN, reconciler, "", f"reports revision {_clip(raw)!r}, which is not a Git commit", health)


def _read_argo(obj: Mapping, merge: str, is_ancestor: IsAncestor | None) -> SyncReading:
    status = _dict(obj.get("status"))
    sync = _dict(status.get("sync"))
    health = str(_dict(status.get("health")).get("status") or "")
    if not sync:
        return SyncReading(UNKNOWN, ARGOCD, "", "the Application reports no sync status", health)
    raw = [str(r) for r in _list(sync.get("revisions")) if r] or ([str(sync["revision"])] if sync.get("revision") else [])
    revisions = [_commit(r) for r in raw]
    if raw and not any(revisions):
        return _not_a_commit(ARGOCD, raw[0], health)
    settled = _settled(ARGOCD, revisions, merge, is_ancestor, health) if sync.get("status") == ARGO_SYNCED else None
    if settled is not None and settled.state == APPLIED:
        return settled
    op = _dict(status.get("operationState"))
    phase = str(op.get("phase") or "")
    result, requested = _dict(op.get("syncResult")), _dict(_dict(op.get("operation")).get("sync"))
    op_revisions = [
        c
        for c in (_commit(r) for r in [result.get("revision"), requested.get("revision"),
                                       *_list(result.get("revisions")), *_list(requested.get("revisions"))])
        if c
    ]
    if phase in ARGO_RUNNING_PHASES:
        target = next(iter(op_revisions), "")
        return SyncReading(PENDING, ARGOCD, "", f"a sync to {_short(target)} is running", health)
    # A failure that names no revision is the merge's, as _merges_error rules
    # for Flux and Config Sync.
    if phase in ARGO_FAILED_PHASES and (not op_revisions or any(_reaches(r, merge, is_ancestor) is True for r in op_revisions)):
        what = f"the sync of {_short(merge)}" if op_revisions else "the last sync, which names no revision,"
        return SyncReading(ERROR, ARGOCD, "", f"{what} {phase.lower()}: {_clip(op.get('message'))}", health)
    # Argo's error conditions name no structured revision, and ComparisonError
    # means it cannot compare any commit, the merge included.
    for cond in _list(status.get("conditions")):
        if isinstance(cond, dict) and str(cond.get("type") or "").endswith(ARGO_ERROR_CONDITION_SUFFIX):
            return SyncReading(ERROR, ARGOCD, "", f"{cond.get('type')}: {_clip(cond.get('message'))}", health)
    if settled is not None and settled.state == UNCONFIRMED:
        return settled
    automated = _dict(_dict(obj.get("spec")).get("syncPolicy")).get("automated")
    if automated is None or _dict(automated).get("enabled") is False:
        why = "automated sync is off, so the merge waits for someone to sync it"
    else:
        why = "waiting for Argo CD to sync it"
    if settled is not None:
        return SyncReading(PENDING, ARGOCD, settled.revision, f"{settled.detail}; {why}", health)
    shown = next((r for r in revisions if r), "")
    return SyncReading(PENDING, ARGOCD, "", f"{sync.get('status') or 'no sync status'} at {_short(shown)}; {why}", health)


def _read_flux(obj: Mapping, merge: str, is_ancestor: IsAncestor | None) -> SyncReading:
    status = _dict(obj.get("status"))
    spec = _dict(obj.get("spec"))
    if not status:
        return SyncReading(UNKNOWN, FLUX, "", "the Kustomization reports no status")
    source_kind = _dict(spec.get("sourceRef")).get("kind")
    if source_kind and source_kind != FLUX_GIT_SOURCE_KIND:
        return SyncReading(UNKNOWN, FLUX, "", f"the Kustomization is built from a {source_kind}, not a Git repository")
    raw_applied = status.get("lastAppliedRevision")
    from_repository = source_kind == FLUX_GIT_SOURCE_KIND
    applied = _flux_commit(raw_applied, from_repository)
    if raw_applied and not applied:
        return _not_a_commit(FLUX, raw_applied)
    settled = _settled(FLUX, [applied], merge, is_ancestor, "")
    if settled is not None and settled.state == APPLIED:
        return settled
    if spec.get("suspend") is True:
        return SyncReading(PENDING, FLUX, applied, "the Kustomization is suspended, so the merge waits until it is resumed")
    attempted = _flux_commit(status.get("lastAttemptedRevision"), from_repository)
    stalled = _condition(status, "Stalled")
    if stalled is not None and stalled.get("status") == "True":
        return SyncReading(ERROR, FLUX, applied, f"Stalled: {_clip(stalled.get('reason'))}: {_clip(stalled.get('message'))}")
    ready = _condition(status, "Ready")
    if (
        ready is not None
        and ready.get("status") == "False"
        and ready.get("reason") not in FLUX_IN_PROGRESS_REASONS
        and _merges_error(attempted, merge, is_ancestor)
    ):
        if ready.get("reason") == FLUX_HEALTH_CHECK_FAILED and attempted:
            return SyncReading(
                APPLIED, FLUX, attempted,
                f"applied {_short(attempted)}; Flux reports its health check failing: {_clip(ready.get('message'))}",
            )
        return SyncReading(ERROR, FLUX, applied, f"Ready=False: {_clip(ready.get('reason'))}: {_clip(ready.get('message'))}")
    if (ready is None or ready.get("status") != "True") and _reaches(attempted, merge, is_ancestor) is True:
        return SyncReading(PENDING, FLUX, applied, f"a reconcile of {_short(attempted)} is running")
    return settled or SyncReading(PENDING, FLUX, "", "the Kustomization has not applied a revision yet")


def _configsync_errors(obj: Mapping, ref: str) -> list[dict]:
    node: Any = obj
    for part in str(ref).split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return [e for e in _list(node) if isinstance(e, dict)]


def _read_configsync(obj: Mapping, merge: str, is_ancestor: IsAncestor | None) -> SyncReading:
    status = _dict(obj.get("status"))
    kind = obj.get("kind")
    if not status:
        return SyncReading(UNKNOWN, CONFIGSYNC, "", f"the {kind} reports no status")
    source_type = str(_dict(obj.get("spec")).get("sourceType") or CONFIGSYNC_GIT_SOURCE)
    if source_type != CONFIGSYNC_GIT_SOURCE:
        return SyncReading(UNKNOWN, CONFIGSYNC, "", f"the {kind} syncs from a {source_type} source, not a Git repository")
    raw_synced = status.get("lastSyncedCommit")
    synced = _commit(raw_synced)
    if raw_synced and not synced:
        return _not_a_commit(CONFIGSYNC, raw_synced)
    settled = _settled(CONFIGSYNC, [synced], merge, is_ancestor, "")
    if settled is not None and settled.state == APPLIED:
        return settled
    stalled = _condition(status, "Stalled")
    if stalled is not None and stalled.get("status") == "True":
        return SyncReading(ERROR, CONFIGSYNC, synced, f"Stalled: {_clip(stalled.get('reason'))}: {_clip(stalled.get('message'))}")
    syncing = _condition(status, "Syncing")
    refs = [str(r) for r in _list((syncing or {}).get("errorSourceRefs")) if r]
    if syncing is not None and refs and _merges_error(_commit(syncing.get("commit")), merge, is_ancestor):
        errors = [e for ref in refs for e in _configsync_errors(obj, ref)]
        real = [e for e in errors if str(e.get("code")) not in CONFIGSYNC_TRANSIENT_ERROR_CODES]
        # Only transient errors is a render in flight. A ref with no error list
        # behind it is still the reconciler saying this commit failed.
        if real or not errors:
            first = next((e.get("errorMessage") for e in real if e.get("errorMessage")), syncing.get("message"))
            return SyncReading(ERROR, CONFIGSYNC, synced, f"errors in {', '.join(refs)}: {_clip(first)}")
    syncing_commit = _commit((syncing or {}).get("commit"))
    if syncing is not None and syncing.get("status") == "True" and _reaches(syncing_commit, merge, is_ancestor) is True:
        return SyncReading(PENDING, CONFIGSYNC, synced, f"a sync of {_short(syncing_commit)} is running")
    return settled or SyncReading(PENDING, CONFIGSYNC, "", f"the {kind} has not synced a commit yet")


_READERS: dict[tuple[str, str], Callable[[Mapping, str, IsAncestor | None], SyncReading]] = {
    (ARGO_GROUP, "Application"): _read_argo,
    (FLUX_KUSTOMIZE_GROUP, "Kustomization"): _read_flux,
    (CONFIGSYNC_GROUP, "RootSync"): _read_configsync,
    (CONFIGSYNC_GROUP, "RepoSync"): _read_configsync,
}


def read_sync(obj: Mapping, merge_commit: str, is_ancestor: IsAncestor | None = None) -> SyncReading:
    """Read one reconciler object against a merge commit.

    ``is_ancestor(merge_commit, revision)`` answers whether ``revision``
    contains ``merge_commit``: True, False, or None when it cannot tell. It is
    called only for a revision that is not the merge commit.

    Raises ValueError for a merge commit that is not a full Git object id (or
    Bitbucket Cloud's 12-character abbreviation, matched as a prefix) or an
    object of a kind this module does not read, and TypeError for an object
    that is not a mapping.
    """
    text = str(merge_commit or "").strip().lower()
    merge = _commit(text) or (text if _ABBREVIATED_MERGE_RE.match(text) else "")
    if not merge:
        raise ValueError(
            "merge_commit must be a full 40- or 64-character Git commit id, or Bitbucket Cloud's 12-character abbreviation"
        )
    if not isinstance(obj, Mapping):
        raise TypeError("the reconciler object must be a JSON object")
    group = str(obj.get("apiVersion") or "").split("/", 1)[0]
    reader = _READERS.get((group, str(obj.get("kind") or "")))
    if reader is None:
        raise ValueError(f"cannot read sync status from {obj.get('apiVersion')} {obj.get('kind')}")
    return reader(obj, merge, is_ancestor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Has the reconciler applied this merge commit?")
    parser.add_argument("--merge-commit", required=True, help="the proposal's mergeCommit")
    args = parser.parse_args(argv)
    try:
        reading = read_sync(json.load(sys.stdin), args.merge_commit)
    except (ValueError, TypeError) as exc:
        print(f"reconciler_sync: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(reading), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
