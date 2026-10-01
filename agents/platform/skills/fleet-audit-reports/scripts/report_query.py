#!/usr/bin/env python3
"""report_query.py — bounded answers out of the fleet-audit report store.

Seven subcommands, one small JSON document each, so that a question about a
past run costs a number instead of a findings document. Design of record:
docs/designs/fleet-audit-report-store.md.

One rule holds all seven together: **every output is bounded and the full
document is opt-in.** `latest.json` embeds the whole findings document,
deliberately un-clipped, so it can run past the 60k characters the ledger body
is held to — times every stream and repository, times a fourteen-run ring. An agent that
answers "how many criticals are open on compliance?" by reading that file
spends tens of thousands of tokens on an integer. So `show` omits `document`,
`findings` returns identity columns and no prose, and `finding` is the one path
that returns a finding whole: the expensive read, at the granularity somebody
actually asked for. `checks` is the same bargain over `document.scope`, for the
one part of a run the ledger issue is expected to be missing.

The files are read through `report_status.py`'s helpers rather than parsed a
second time here. Two parsers of one envelope is one more thing to keep in step
with the writer, and the writer is the only party that gets to define the
envelope.

A stream is stored once per repository it publishes to. Every subcommand but
`streams` reads one of them: `--repo owner/name`, or the only one there is. A
stream with several and no `--repo` is refused with the list, rather than
answered from whichever repository happens to sort first. An owner directory
that cannot be listed makes the stream unreadable, as `streams` reports it:
the default then is not known to be the only one, and a repository named under
that owner could not be looked for.

Exit 0 means answered. Exit 2 means the question could not be answered, and
stdout still carries one JSON object whose `error` says why — an absent or
unlistable store, an absent stream, an absent stamp, a file that would not
parse. **A missing
`latest.json` is unknown, never clean.**
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

# The reading helpers live in the sibling writer skill. Both bundles are
# scaffolded into the same profile `skills/` directory, so this is a fixed hop
# rather than a search: .../skills/fleet-audit-reports/scripts/ -> .../skills/.
HELPERS_DIR = Path(__file__).resolve().parents[2] / "fleet-audit" / "scripts"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

try:
    import report_status
except ImportError as exc:  # noqa: BLE001 — reported below, never fallen back from
    # No second parser here as a consolation prize. A store read that silently
    # switched to a private copy of the envelope rules is a divergence nobody
    # would see until it answered a question wrongly; a missing sibling skill
    # is an install to fix, and it says so.
    report_status = None  # type: ignore[assignment]
    IMPORT_ERROR = (
        f"cannot import report_status from {HELPERS_DIR}: {exc}. "
        "The fleet-audit skill must be installed alongside this one — it owns "
        "the report store and the helpers that read it."
    )
else:
    IMPORT_ERROR = None

# Findings sort severity-first, exactly as the ledger body renders them, so a
# `--limit` that truncates only ever drops the least-severe end.
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}

# Enough for a finding-heavy stream (compliance carries 57) and still bounded
# against a stream that grows one. `matched` and `truncated` say when it bit.
DEFAULT_LIMIT = 100

# How many ids a "no such finding" answer offers back. A typo wants candidates,
# not the roster it just failed to match against.
MAX_ID_HINTS = 20


class QueryError(Exception):
    """A question this store cannot answer, plus the keys that help next time.

    Every subcommand raises it rather than tracebacking: the caller is an agent
    parsing stdout, and a stack trace is an unparseable answer to a question
    that has a real one ("the ring holds these stamps, not that one").
    """

    def __init__(self, message: str, **fields: object) -> None:
        super().__init__(message)
        self.fields = fields


def _oneline(exc: Exception) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


def _root_of(args: argparse.Namespace) -> str:
    return report_status.reports_root(getattr(args, "root", None))


def _stream_ids(root: str) -> list[str]:
    try:
        return report_status.stream_ids(root)
    except OSError as exc:
        raise QueryError(f"{root} could not be listed: {_oneline(exc)}") from exc


def _inside_store(value: str, what: str) -> str:
    """A stream id and a run stamp are each one name inside the store.

    Both reach `os.path.join(root, audit_id, "runs", name)` straight from the
    command line, and `join` walks wherever a `../` tells it to: `--run
    ../../../../etc/hosts` read a file the store does not contain and printed
    it as a run envelope. This subcommand set exists to be the constrained way
    to read the store — an agent that can be handed a stream and a stamp
    without being handed the filesystem — so the constraint has to hold against
    the arguments, not just against well-behaved ones.
    """
    if value != os.path.basename(value) or value in ("", os.curdir, os.pardir) or os.sep in value:
        raise QueryError(f"{what} {value!r} is not a name inside the report store")
    return value


def _unreadable_store(root: str, reason: str | None) -> str:
    """One wording for a store that is there but cannot be listed, whichever
    subcommand found it."""
    detail = f" ({reason})" if reason else ""
    return (
        f"report store not readable at {root}{detail}: no stream can be "
        "answered from it. This is unknown, not clean."
    )


def _absent_store(root: str) -> str:
    """One wording for a store root that does not exist yet, whichever
    subcommand found it: nothing stored, not a store that could not be read."""
    return (
        f"report store not found at {root}. Nothing can be answered from it — "
        "this is unknown, not clean."
    )


def _store_error(root: str, reason: str | None) -> str:
    return _unreadable_store(root, reason) if reason else _absent_store(root)


def _require_stream(root: str, audit_id: str) -> None:
    """Absent store and absent stream are different answers, so they are
    different messages — one is "I could not look", the other "nothing to look
    at". The root's listing is tried first, since a root that exists but
    cannot be listed passes `isdir` and would otherwise read as an absent
    stream; so a root that can be searched but not listed refuses every
    per-stream answer rather than guessing. A stream directory that cannot be
    stat'd (a root that can be listed but not searched) is unreadable, as
    `streams` reports it, not absent."""
    _inside_store(audit_id, "stream")
    reason = report_status.store_root_error(root)
    if reason or not os.path.isdir(root):
        raise QueryError(
            _store_error(root, reason),
            root=root,
            root_exists=False,
            root_error=reason,
            # The lease is in scratch, not under the root: a first run in
            # flight before any `finish` has made the root is `running`.
            **_liveness(root, audit_id),
        )
    try:
        is_stream = stat.S_ISDIR(os.stat(os.path.join(root, audit_id)).st_mode)
    except FileNotFoundError:
        is_stream = False
    except OSError as exc:
        raise _unreadable_stream(root, audit_id, report_status.os_reason(exc)) from exc
    if not is_stream:
        raise QueryError(
            f"no reports for stream {audit_id!r} under {root}",
            root=root,
            streams=_stream_ids(root),
            **_liveness(root, audit_id),
        )


def _unreadable_stream(root: str, audit_id: str, reason: str, **extra: object) -> QueryError:
    """The refusal `streams` gives a stream it could not read. The same
    projection `streams` reads, so `stream_error` is that stream's row
    `error`, verbatim; `reason` only if the projection, run a moment later,
    read the stream after all."""
    fields = _liveness(root, audit_id)
    return QueryError(
        f"streams that could not be read: {audit_id} ({reason}). This is "
        "unknown, not clean.",
        root=root,
        liveness="error",
        stream_error=fields.get("stream_error") or reason,
        **extra,
    )


def _repos(root: str, audit_id: str) -> tuple[list[str], list[str]]:
    """The stream's repositories, and a line per owner directory that could
    not be listed: `repo_ids` drops those, and a repository hidden behind one
    is unread, not absent."""
    try:
        dirs, unreadable = report_status.scan_repo_dirs(root, audit_id)
    except OSError as exc:
        raise QueryError(f"{audit_id}/ could not be listed: {_oneline(exc)}") from exc
    return [repo for repo in dirs if repo == repo.lower()], unreadable


def _resolve_repo(root: str, audit_id: str, repo: str | None) -> str:
    """The repository a per-run question is about: the one named, or the only
    one the stream has. Several and none named is refused with the list — a
    default would answer a question about one ledger from another's runs."""
    _require_stream(root, audit_id)
    repos, unreadable = _repos(root, audit_id)
    if repo is not None:
        try:
            path = report_status.store_path(root, audit_id, repo)
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        if repo.lower() not in repos:
            # Its owner may not have listed: whether the directory is there is
            # the stat's to say, and a stat that fails is unread, not absent.
            try:
                is_repo = stat.S_ISDIR(os.stat(path).st_mode)
            except FileNotFoundError:
                is_repo = False
            except OSError as exc:
                raise _unreadable_stream(
                    root, audit_id, report_status.os_reason(exc), repos=repos
                ) from exc
            if is_repo and unreadable:
                return repo.lower()
            raise QueryError(
                f"no reports for {audit_id} in {repo}", repos=repos,
                **_liveness(root, audit_id),
            )
        return repo.lower()
    if unreadable:
        # "The only one there is" is not known while an owner is unlisted,
        # and none listed is not "no record".
        raise _unreadable_stream(root, audit_id, "; ".join(unreadable), repos=repos)
    if len(repos) == 1:
        return repos[0]
    if not repos:
        raise QueryError(
            f"{audit_id} has no repository directory under {root}: the store "
            "holds no record of a run for it. That means unknown, not clean — "
            "say so and read the ledger issue.",
            **_liveness(root, audit_id),
        )
    raise QueryError(
        f"{audit_id} publishes to {len(repos)} repositories; name one with --repo",
        repos=repos,
    )


def _ring(root: str, audit_id: str, repo: str) -> list[str]:
    try:
        return report_status.list_runs(root, audit_id, repo)
    except OSError as exc:
        raise QueryError(
            f"{audit_id} in {repo}: runs/ could not be listed: {_oneline(exc)}"
        ) from exc


def _stream_state(root: str, audit_id: str) -> tuple[str, str | None]:
    """The stream's liveness and stream-level error, as `streams` reports
    them: the same projection, so `runs` and a refusal cannot disagree with
    it about a lease, and an `error` liveness always comes with its reason."""
    try:
        stream = report_status.project_stream(
            root, report_status.scratch_root(), audit_id, time.time()
        )
    except (OSError, ValueError, OverflowError) as exc:
        return "error", f"the stream could not be projected: {_oneline(exc)}"
    return stream["liveness"], stream.get("error")


def _liveness(root: str, audit_id: str) -> dict:
    """The liveness fields a refusal carries: `liveness`, and `stream_error`
    beside it whenever the stream has one, so an `error` never arrives
    without its reason."""
    liveness, error = _stream_state(root, audit_id)
    return {"liveness": liveness, **({"stream_error": error} if error else {})}


def _run_name(run: str | None) -> str:
    """A stamp as the ring spells it. `--run 20260826T063100.123456Z` and the
    same string with `.json` are the same run; no argument means the newest."""
    if run is None or run in ("latest", report_status.LATEST_NAME):
        return report_status.LATEST_NAME
    return _inside_store(run if run.endswith(".json") else f"{run}.json", "run")


def load_envelope(root: str, audit_id: str, repo: str, run: str | None) -> tuple[str, dict]:
    """One run's envelope, whole, `document` included. Raises QueryError.

    `repo` is already resolved (`_resolve_repo`)."""
    name = _run_name(run)
    try:
        if name == report_status.LATEST_NAME:
            envelope, loaded = report_status.load_last_named(root, audit_id, repo)
            if loaded not in (None, name):
                # `latest.json` is gone or older than the newest ring entry:
                # answer from the entry, and say the ledger may have moved on.
                name = loaded
                envelope = {**envelope, "latest_missing": True} if envelope else None
        else:
            envelope = report_status.load_run(root, audit_id, repo, name)
    except report_status.RingReadError as exc:
        raise QueryError(
            f"{audit_id}/latest.json in {repo} is gone and the ring behind it could not "
            f"be read: {audit_id}/{exc.name}: {_oneline(exc)}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise QueryError(
            f"{audit_id}/{name} in {repo} could not be read: {_oneline(exc)}"
        ) from exc
    if envelope is None:
        if name == report_status.LATEST_NAME:
            raise QueryError(
                f"{audit_id} has no run stored for {repo}: the store holds no "
                "record of it. That means unknown, not clean — say so and read "
                "the ledger issue.",
                **_liveness(root, audit_id),
                runs=_ring(root, audit_id, repo),
            )
        raise QueryError(
            f"{audit_id} has no run {name!r} in the ring for {repo}",
            runs=_ring(root, audit_id, repo),
        )
    return name, envelope


def _open(args: argparse.Namespace) -> tuple[str, str, str, dict]:
    """Root, repository, run name and envelope for a per-run subcommand."""
    root = _root_of(args)
    repo = _resolve_repo(root, args.stream, args.repo)
    name, envelope = load_envelope(root, args.stream, repo, args.run)
    return root, repo, name, envelope


def _findings_of(audit_id: str, name: str, envelope: dict) -> list[dict]:
    document = envelope.get("document")
    if not isinstance(document, dict):
        raise QueryError(f"{audit_id}/{name} carries no findings document")
    findings = document.get("findings")
    if not isinstance(findings, list):
        raise QueryError(f"{audit_id}/{name}: document.findings is not a list")
    if not all(isinstance(finding, dict) for finding in findings):
        raise QueryError(f"{audit_id}/{name}: document.findings holds a non-object entry")
    return findings


def _scope_clusters(audit_id: str, name: str, envelope: dict) -> list[dict]:
    document = envelope.get("document")
    if not isinstance(document, dict):
        raise QueryError(f"{audit_id}/{name} carries no findings document")
    scope = document.get("scope")
    if not isinstance(scope, dict):
        raise QueryError(f"{audit_id}/{name}: document.scope is not an object")
    clusters = scope.get("clusters")
    if not isinstance(clusters, list):
        raise QueryError(f"{audit_id}/{name}: document.scope.clusters is not a list")
    if not all(isinstance(entry, dict) for entry in clusters):
        raise QueryError(f"{audit_id}/{name}: document.scope.clusters holds a non-object entry")
    return clusters


def _severity_key(finding: dict) -> tuple[int, str]:
    severity = str(finding.get("severity", "")).strip().lower()
    return (SEVERITY_ORDER.get(severity, len(SEVERITY_ORDER)), str(finding.get("id", "")))


def _identity(finding: dict) -> dict:
    """The columns that name a finding, and nothing that carries prose.

    Evidence excerpts, impact and the three `recommendation` fields are what
    make a document megabyte-scale, and none of them are needed to answer
    "which criticals are open" — `finding` returns them for the one id the
    answer landed on.
    """
    return {
        "id": finding.get("id"),
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "cluster": finding.get("cluster"),
        "check": finding.get("check"),
    }


def _matches(finding: dict, severity: str | None, cluster: str | None, check: str | None) -> bool:
    for wanted, key in ((severity, "severity"), (cluster, "cluster"), (check, "check")):
        if wanted is None:
            continue
        if str(finding.get(key, "")).strip().lower() != wanted.strip().lower():
            return False
    return True


def cmd_streams(args: argparse.Namespace) -> dict:
    """One row per stream — the fleet at a glance, no document read into the
    answer. `report_status.project` already computes this; the rows here are
    its projection with the per-stream `latest` flattened and the ring reduced
    to a count."""
    projection = report_status.project(_root_of(args))
    rows = [
        _stream_row(audit_id, stream, repo, entry)
        for audit_id, stream in sorted(projection["streams"].items())
        for repo, entry in (sorted((stream.get("repos") or {}).items()) or [(None, {})])
    ]
    unreadable = sorted({row["audit_id"] for row in rows if row["error"]})
    error = None
    if not projection["root_exists"]:
        error = _store_error(projection["root"], projection.get("root_error"))
    # Before `unreadable`: `project` stamps the lease failure onto every
    # stream's error, so naming those streams would blame stores that read fine.
    elif projection.get("lease_error"):
        error = (
            f"in-flight leases not readable ({projection['lease_error']}): a run "
            "in progress would not be listed. This is unknown, not clean."
        )
    elif unreadable:
        error = "streams that could not be read: " + ", ".join(unreadable)
    return {
        "root": projection["root"],
        "root_exists": projection["root_exists"],
        "root_error": projection.get("root_error"),
        "generated_at": projection["generated_at"],
        "ttl_s": projection["ttl_s"],
        "lease_error": projection.get("lease_error"),
        "streams": rows,
        "error": error,
    }


def _stream_row(audit_id: str, stream: dict, repo: str | None, entry: dict) -> dict:
    """One row per stream and repository; a stream with no store yet (never
    run, or a first run in flight) is one row with `repo` null."""
    latest = entry.get("latest") or {}
    started = stream.get("started") or {}
    gaps = latest.get("coverage_gaps")
    return {
        "audit_id": audit_id,
        "repo": repo,
        "liveness": stream.get("liveness"),
        "finished_at": latest.get("finished_at"),
        "status": latest.get("status"),
        "findings": latest.get("findings"),
        "critical": latest.get("critical"),
        "new": latest.get("new"),
        "resolved": latest.get("resolved"),
        # False when `finish` withheld the delta over a lost memory: `new`
        # and `resolved` are then 0 because nothing was claimed, not because
        # nothing changed.
        "delta_known": latest.get("delta_known"),
        "current": latest.get("current"),
        "clusters": latest.get("clusters"),
        "skipped": latest.get("skipped"),
        "partial": latest.get("partial"),
        # True when the issue still lists the previous run's findings, so
        # this run's zero `findings` and `critical` do not say it is clear.
        "ledger_held_open": latest.get("ledger_held_open"),
        # True when the row is the newest ring entry because a later run
        # failed before storing: the issue may be newer than this row.
        "latest_missing": entry.get("latest_missing"),
        # A count, not the gap strings: every stream's worth of prose is the
        # unbounded shape this command exists to avoid. `show` names them.
        "gaps": len(gaps) if isinstance(gaps, list) else None,
        "issue_number": latest.get("issue_number"),
        "issue_url": latest.get("issue_url"),
        "runs": len(entry.get("runs") or []),
        "running_since": started.get("started_at"),
        "age_s": started.get("age_s"),
        # A stream-level error -- the lease, a stray directory, a sibling
        # repository -- goes on every row of the stream, whatever its liveness:
        # a lease makes it `running` or `died` without clearing the error.
        "error": entry.get("error") or stream.get("error"),
    }


def cmd_show(args: argparse.Namespace) -> dict:
    """One run's envelope without `document` — status, delta counts, coverage
    gaps, issue link."""
    root, repo, name, envelope = _open(args)
    return {
        "root": root,
        "audit_id": args.stream,
        "repo": repo,
        "run": name,
        # The projection the status view renders off-pod, reused rather than
        # re-derived. The leading underscore marks it module-private to
        # report_status' own callers; this script is one of them by design,
        # and a second copy of "every key except these, plus these counts" is
        # exactly the drift that sharing the helpers prevents.
        "envelope": report_status._project_latest(
            {key: value for key, value in envelope.items() if key != "latest_missing"}
        ),
        # Always a boolean, as on the other per-run subcommands.
        "latest_missing": bool(envelope.get("latest_missing")),
        "error": None,
    }


def cmd_findings(args: argparse.Namespace) -> dict:
    """Identity columns for the findings of one run, filterable. Never a body,
    never an excerpt, never recommendation prose."""
    root, repo, name, envelope = _open(args)
    findings = _findings_of(args.stream, name, envelope)
    matched = sorted(
        (f for f in findings if _matches(f, args.severity, args.cluster, args.check)),
        key=_severity_key,
    )
    shown = matched[: args.limit]
    return {
        "root": root,
        "audit_id": args.stream,
        "repo": repo,
        "run": name,
        "finished_at": envelope.get("finished_at"),
        "status": envelope.get("status"),
        "ledger_held_open": bool(envelope.get("ledger_held_open")),
        "latest_missing": bool(envelope.get("latest_missing")),
        "filters": {
            "severity": args.severity,
            "cluster": args.cluster,
            "check": args.check,
        },
        "total": len(findings),
        "matched": len(matched),
        "returned": len(shown),
        "truncated": len(shown) < len(matched),
        "findings": [_identity(finding) for finding in shown],
        "error": None,
    }


def cmd_finding(args: argparse.Namespace) -> dict:
    """One finding, whole. The only subcommand that returns prose."""
    root, repo, name, envelope = _open(args)
    findings = _findings_of(args.stream, name, envelope)
    for finding in findings:
        if str(finding.get("id", "")) == args.id:
            return {
                "root": root,
                "audit_id": args.stream,
                "repo": repo,
                "run": name,
                "finished_at": envelope.get("finished_at"),
                "latest_missing": bool(envelope.get("latest_missing")),
                "finding": finding,
                "error": None,
            }
    ordered = sorted(findings, key=_severity_key)
    raise QueryError(
        f"{args.stream}/{name} has no finding with id {args.id!r}",
        available=len(findings),
        ids=[f.get("id") for f in ordered[:MAX_ID_HINTS]],
    )


def cmd_checks(args: argparse.Namespace) -> dict:
    """The command behind every check one run says it performed.

    The ledger publishes these itself, in a collapsed table that is last in line
    for the body budget — so on a finding-heavy run it is dropped whole and
    replaced by a notice saying the commands are kept in the stored report and
    to ask the agent for that report to re-run any of them. This subcommand is
    what makes that sentence true: the alternative on offer was opening a
    138 KB envelope with a file tool, which the skill forbids for good reason.

    Rows come back in the document's own order, so a truncated answer lines up
    with the published table rather than a re-sort of it. `--cluster` and
    `--check` narrow before `--limit` bites.

    `checks_not_applicable` comes back alongside, because the notice counts
    those too and they are the claims most worth a second reader: an excluded
    check leaves the coverage denominator, which is the one way a partial run
    can read as complete.
    """
    root, repo, name, envelope = _open(args)
    clusters = _scope_clusters(args.stream, name, envelope)
    ran: list[dict] = []
    excluded: list[dict] = []
    for cluster in clusters:
        where = cluster.get("name")
        for entry in cluster.get("checks_run") or []:
            if isinstance(entry, dict):
                ran.append(
                    {
                        "cluster": where,
                        "check": entry.get("check"),
                        "command": entry.get("command"),
                    }
                )
        for entry in cluster.get("checks_not_applicable") or []:
            if isinstance(entry, dict):
                excluded.append(
                    {
                        "cluster": where,
                        "check": entry.get("check"),
                        "reason": entry.get("reason"),
                    }
                )
    matched = [row for row in ran if _matches(row, None, args.cluster, args.check)]
    na_matched = [row for row in excluded if _matches(row, None, args.cluster, args.check)]
    shown = matched[: args.limit]
    na_shown = na_matched[: args.limit]
    return {
        "root": root,
        "audit_id": args.stream,
        "repo": repo,
        "run": name,
        "finished_at": envelope.get("finished_at"),
        "status": envelope.get("status"),
        "latest_missing": bool(envelope.get("latest_missing")),
        "filters": {"cluster": args.cluster, "check": args.check},
        "scope_entries": len(clusters),
        "total": len(ran),
        "matched": len(matched),
        "returned": len(shown),
        "truncated": len(shown) < len(matched),
        "checks": shown,
        "not_applicable_total": len(excluded),
        "not_applicable_matched": len(na_matched),
        "not_applicable_returned": len(na_shown),
        "not_applicable_truncated": len(na_shown) < len(na_matched),
        "not_applicable": na_shown,
        "error": None,
    }


def cmd_diff(args: argparse.Namespace) -> dict:
    """What two ring entries disagree about: ids and titles added and resolved.

    Computed from each run's whole `document.findings`, not from the envelope's
    `new_ids`/`resolved_ids` — those are one run's delta against the run before
    it, which answers a different question than "what changed between Monday
    and Friday", and `current_ids` is the body's hidden block rather than the document.
    """
    root = _root_of(args)
    repo = _resolve_repo(root, args.stream, args.repo)
    ring = _ring(root, args.stream, repo)
    if not ring:
        raise QueryError(f"{args.stream}: the run ring is empty, so there is nothing to diff")
    later = _run_name(args.to) if args.to else ring[-1]
    if later == report_status.LATEST_NAME:
        later = ring[-1]
    if later not in ring:
        raise QueryError(f"{args.stream} has no run {later!r} in the ring", runs=ring)
    if args.frm:
        earlier = _run_name(args.frm)
    else:
        index = ring.index(later)
        if index == 0:
            raise QueryError(
                f"{args.stream}: {later} is the oldest entry in the ring, so there "
                "is no earlier run to diff it against",
                runs=ring,
            )
        earlier = ring[index - 1]
    if earlier not in ring:
        raise QueryError(f"{args.stream} has no run {earlier!r} in the ring", runs=ring)

    if ring.index(earlier) >= ring.index(later):
        # Reversed, `added` and `resolved` would swap silently and read as a
        # true answer to the opposite question.
        raise QueryError(
            f"{args.stream}: --from {earlier} is not older than --to {later}", runs=ring
        )
    _, before_envelope = load_envelope(root, args.stream, repo, earlier)
    _, after_envelope = load_envelope(root, args.stream, repo, later)
    before = {str(f.get("id")): f for f in _findings_of(args.stream, earlier, before_envelope)}
    after = {str(f.get("id")): f for f in _findings_of(args.stream, later, after_envelope)}
    added = sorted((f for fid, f in after.items() if fid not in before), key=_severity_key)
    resolved = sorted((f for fid, f in before.items() if fid not in after), key=_severity_key)
    return {
        "root": root,
        "audit_id": args.stream,
        "repo": repo,
        "from": earlier,
        "to": later,
        "from_finished_at": before_envelope.get("finished_at"),
        "to_finished_at": after_envelope.get("finished_at"),
        # A partial run's document is silent about what it could not see, so
        # a finding absent from it is unseen rather than fixed; the harness
        # never announces one resolved over a partial run, and nor may this.
        "from_partial": bool(before_envelope.get("partial")),
        "to_partial": bool(after_envelope.get("partial")),
        # A run that held the ledger open refused to call the previous
        # findings resolved; its empty document says nothing more than that.
        "from_held_open": bool(before_envelope.get("ledger_held_open")),
        "to_held_open": bool(after_envelope.get("ledger_held_open")),
        "added": [_identity(f) for f in added[: args.limit]],
        "resolved": [_identity(f) for f in resolved[: args.limit]],
        "added_total": len(added),
        "resolved_total": len(resolved),
        "unchanged": len(set(before) & set(after)),
        "truncated": len(added) > args.limit or len(resolved) > args.limit,
        "error": None,
    }


def cmd_runs(args: argparse.Namespace) -> dict:
    """What the ring holds, so a `diff` can name real stamps.

    The ring by filename. Reading fourteen envelopes to decorate a listing
    would spend the whole store to answer "which runs are there". Liveness and
    the error come from the stream's projection, which reads each repository's
    newest envelope as `streams` does; no older ring entry is parsed.
    """
    root = _root_of(args)
    repo = _resolve_repo(root, args.stream, args.repo)
    ring = _ring(root, args.stream, repo)
    # The stream's error rides along as it does on every `streams` row -- a
    # sibling repository's corrupt store, a stray directory, an unreadable
    # lease -- and a lease that makes liveness `running` does not clear it.
    # The ring still lists; the error makes the exit 2, as it does there.
    liveness, error = _stream_state(root, args.stream)
    return {
        "root": root,
        "audit_id": args.stream,
        "repo": repo,
        "count": len(ring),
        "runs": ring,
        "newest": ring[-1] if ring else None,
        "liveness": liveness,
        "error": error,
    }


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded reads of the fleet-audit report store.",
        epilog="Exit 0 answered the question; exit 2 could not, and the JSON says why.",
    )
    # No literal fallback: a third copy of the root is one more to drift, and
    # without report_status every subcommand refuses anyway.
    default_root = os.environ.get("FLEET_AUDIT_REPORTS_DIR") or getattr(
        report_status, "REPORTS_DIR", "unknown: the fleet-audit skill is not installed"
    )
    parser.add_argument("--root", help=f"store root to read (default: {default_root})")
    # Repeated on every subparser so `… findings compliance-audit --root X`
    # works too, and suppressed so the subparser's absent value never
    # overwrites one given before the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    repo_flag = argparse.ArgumentParser(add_help=False)
    repo_flag.add_argument(
        "--repo",
        help="owner/name of the ledger's repository; required when the stream has several",
    )
    run_flag = argparse.ArgumentParser(add_help=False)
    run_flag.add_argument(
        "--run",
        help="a stamp from `runs` (with or without .json); default is the newest run",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    streams = subcommands.add_parser(
        "streams", parents=[shared], help="one row per stream: last run, status, counts, liveness"
    )
    streams.set_defaults(handler=cmd_streams)

    show = subcommands.add_parser(
        "show", parents=[shared, repo_flag, run_flag], help="one run's envelope without its document"
    )
    show.add_argument("stream")
    show.set_defaults(handler=cmd_show)

    findings = subcommands.add_parser(
        "findings", parents=[shared, repo_flag, run_flag], help="finding id/severity/title/cluster/check"
    )
    findings.add_argument("stream")
    findings.add_argument("--severity", help="critical, major or minor")
    findings.add_argument("--cluster")
    findings.add_argument("--check")
    findings.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    findings.set_defaults(handler=cmd_findings)

    finding = subcommands.add_parser(
        "finding", parents=[shared, repo_flag, run_flag], help="one finding in full, prose included"
    )
    finding.add_argument("stream")
    finding.add_argument("id")
    finding.set_defaults(handler=cmd_finding)

    checks = subcommands.add_parser(
        "checks",
        parents=[shared, repo_flag, run_flag],
        help="the command behind each check a run ran, and the checks it excluded",
    )
    checks.add_argument("stream")
    checks.add_argument("--cluster")
    checks.add_argument("--check")
    checks.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    checks.set_defaults(handler=cmd_checks)

    diff = subcommands.add_parser(
        "diff", parents=[shared, repo_flag], help="what changed between two runs in the ring"
    )
    diff.add_argument("stream")
    diff.add_argument("--from", dest="frm", help="older stamp (default: the one before --to)")
    diff.add_argument("--to", dest="to", help="newer stamp (default: the newest run)")
    diff.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    diff.set_defaults(handler=cmd_diff)

    runs = subcommands.add_parser("runs", parents=[shared, repo_flag], help="the stamps the ring holds")
    runs.add_argument("stream")
    runs.set_defaults(handler=cmd_runs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if report_status is None:
        print(json.dumps({"error": IMPORT_ERROR, "looked_in": str(HELPERS_DIR)}, sort_keys=True))
        return 2
    try:
        payload = args.handler(args)
    except QueryError as exc:
        payload = {"error": str(exc), **exc.fields}
    print(json.dumps(payload, sort_keys=True))
    return 2 if payload.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
