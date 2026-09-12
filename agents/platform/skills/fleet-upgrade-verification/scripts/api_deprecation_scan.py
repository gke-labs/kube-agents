#!/usr/bin/env python3
"""
api_deprecation_scan.py — Kubernetes API removals between the fleet and a target version,
read from the manifests the linked GitOps repositories declare.

An upgrade to a version that removes an API breaks every workload still declaring it.
This script reads what Git declares before the target is chosen: every `*.yaml`, `*.yml`
and `*.json` in each managed repository, the top-level `apiVersion`/`kind`/`metadata.name`
of every document in it, matched against `removed_apis.json` beside this file. A hit is an
apiVersion removed after the fleet's current version and no later than the target.

The fleet's current version comes from Phase 1's table (`fleet_upgrade_report.py --output`)
or from `--current-version`; the floor is the lowest control-plane minor, because the API
server is what stops serving a removed version — node pools do not enter into it.

Read-only against every repository. It opens each one through the credential broker's
content workspaces (a shallow read-only clone the broker owns) and, on an install whose
broker is not armed for content-passing, through a leased checkout on the shared volume,
the way `inspect_repository.py clone` does — under a lease of its own, so the reset that
positions that checkout on the base branch never touches the session's working tree, the
one `submit_suggestion.py prepare` hands the agent to edit. It runs no `gcloud`. Live client usage of deprecated
APIs is a different question, answered by GKE Deprecation Insights; the footer says where.

A file that does not parse (a Helm template, a Kustomize patch with anchors the loader
refuses) is listed as skipped with the reason, never a crash, and a section reads clean
only alongside the count of what it did not read. A repository the broker or git cannot
serve is listed under errors and sets exit code 1: no repo, no false clean.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

# The shared helpers, in the pod (`/opt/defaults/scripts`, the operator's copy;
# `/opt/data/scripts`, the profile's) and in a source checkout, where nothing is
# staged into /opt. Same three entries as inspect_repository.py.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402
import gitops_workspace  # noqa: E402

# The lease owner name written beside a directory-mode checkout, so a stale
# lease names the skill that took it, and the prefix that makes the scan's lease
# its own. `ensure_workspace(reset=True)` runs `git reset --hard` and `git clean`
# in the lease's clone; under the session lease that clone is the tree
# submit-suggestion's `prepare` opened for the agent to edit, and a scan asked
# for mid-edit would delete the edits. A prefix rather than a suffix, because
# `sanitize_lease` keeps the first 64 characters.
OWNER = "fleet-upgrade-verification"
SCAN_LEASE_PREFIX = "api-deprecation-scan-"

# The removal table, beside this script so a skill copy carries its data.
TABLE_PATH = Path(__file__).resolve().parent / "removed_apis.json"
TABLE_NAME = TABLE_PATH.name

# What a manifest looks like from the outside. Kustomize and Helm chart
# metadata files share these suffixes and are read too; they simply carry no
# apiVersion the table names.
MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")
JSON_SUFFIX = ".json"

# Directories never walked in a checkout or a local tree.
SKIPPED_DIRS = frozenset({".git"})

# Under the broker's own per-request ceilings (256 paths, 8 MiB), with room to
# spare, the same two numbers inspect_repository.py batches with.
BATCH_PATHS = 100
BATCH_BYTES = 6 << 20

# Bounds on one repository, so a repository nobody sized cannot fill the agent's
# volume or its context. Both are reported when they bite; a capped scan is a
# partial scan and the section says so.
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 64 << 20
STOPPED_MAX_FILES = "maxFiles"
STOPPED_MAX_BYTES = "maxBytes"

# `[v]MAJOR.MINOR[.PATCH][-gke.BUILD]`: the GKE form Phase 1 prints, and the bare
# minor the removal table and a human's `--target-version 1.27` use. Only the
# major and minor matter here; a removal happens at a minor.
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.\d+)?(?:-gke\.\d+)?$")

# One `git` runs per directory-mode repository, to name the sha the section
# reports; the same budget the Phase 1 script gives gcloud.
GIT_TIMEOUT_SECONDS = 60
GIT_HEAD_CMD = ("git", "rev-parse", "HEAD")

# Manifest document keys, and the kind whose `items` are manifests of their own.
API_VERSION_KEY = "apiVersion"
KIND_KEY = "kind"
METADATA_KEY = "metadata"
NAME_KEY = "name"
ITEMS_KEY = "items"
LIST_KIND = "List"

# The broker's `read` answers the tail of a batch that crossed its per-request
# byte budget with this reason, which `Workspace.read_many` documents as "ask
# again for the rest"; the other reasons (`tooLarge`, `symlink`) are final.
REASON_REQUEST_BUDGET = "requestBudget"

# Skip reasons. A Go-template marker in a file that fails to parse is the
# commonest cause, and the reason says what to do about it.
TEMPLATE_MARKER = "{{"
REASON_TEMPLATE = "Go template (Helm chart?); render it and scan the output"
REASON_YAML = "YAML did not parse: {error}"
REASON_JSON = "JSON did not parse: {error}"
REASON_ENCODING = "not UTF-8 text"

# Source lines. The clean section states its source; the issue's acceptance
# criterion quotes the first form verbatim.
SOURCE_REPO = "repo manifests as of {sha}"
SOURCE_LOCAL = "local directory {path}"
SHA_UNKNOWN = "unknown sha"

# Where live usage of deprecated APIs is read, which this script does not do:
# GKE builds Deprecation Insights from audit logs of what clients actually call.
# The command is for a human or a later change; `recommender insights list` is
# not in the agent's gcloud read allowlist and the read-only permission set does
# not carry the role it needs.
DEPRECATION_INSIGHTS_URL = (
    "https://cloud.google.com/kubernetes-engine/docs/deprecations/"
    "viewing-deprecation-insights-and-recommendations"
)
DEPRECATION_INSIGHTS_COMMAND = (
    "gcloud recommender insights list --insight-type=google.container.DiagnosisInsight "
    "--project=<project> --location=<location>"
)

# The table lists what is known through `as_of`; a target beyond it needs the
# agent to confirm newer removals elsewhere, and the report says so.
TABLE_STALE_NOTE = (
    "target {target} is newer than {table} (as of Kubernetes {as_of}); confirm removals "
    "after {as_of} through mcp-developer_knowledge before trusting a clean section"
)

# Rendering.
EMPTY_CELL = "-"
TABLE_SEPARATOR_CELL = "---"
MEMBER_SEPARATOR = ", "
HIT_COLUMNS = (
    "path",
    "kind",
    "name",
    "apiVersion used",
    "removed in",
    "replacement",
    "members affected",
)
JSON_INDENT = 2
NO_REPOS_HEADING = "## No managed repositories configured"

# Exit codes: the Phase 1 contract. A repository that could not be read is
# reported and does not abort the others; the exit code says whether every
# requested read succeeded.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


Version = tuple[int, int]


def parse_version(text) -> Version | None:
    """`1.30.5-gke.1355000` -> (1, 30); `1.27` -> (1, 27); None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = VERSION_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def format_version(version: Version) -> str:
    return f"{version[0]}.{version[1]}"


# --- the table ---------------------------------------------------------------


@dataclass(frozen=True)
class Removal:
    api_version: str
    kind: str
    removed_in: Version
    replacement: str


@dataclass
class Table:
    source_url: str
    as_of: Version
    removals: list[Removal]

    def by_key(self) -> dict[tuple[str, str], Removal]:
        return {(r.api_version, r.kind): r for r in self.removals}


def load_table(path: Path = TABLE_PATH) -> Table:
    """Reads and validates the removal table; a malformed entry is a usage error."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    as_of = parse_version(raw.get("as_of"))
    if as_of is None:
        raise ValueError(f"{path}: as_of {raw.get('as_of')!r} is not MAJOR.MINOR")
    removals: list[Removal] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw.get("removed", []):
        removed_in = parse_version(entry.get("removed_in"))
        api_version = entry.get("api_version")
        kind = entry.get("kind")
        replacement = entry.get("replacement")
        if not api_version or not kind or removed_in is None or not replacement:
            raise ValueError(f"{path}: malformed entry {entry!r}")
        key = (api_version, kind)
        if key in seen:
            raise ValueError(f"{path}: duplicate entry for {api_version} {kind}")
        seen.add(key)
        removals.append(Removal(api_version, kind, removed_in, replacement))
    return Table(source_url=raw.get("source_url", ""), as_of=as_of, removals=removals)


def removals_in_range(table: Table, floor: Version, target: Version) -> list[Removal]:
    """Removals the upgrade crosses: floor < removed_in <= target.

    A removal at or below the floor has already happened on every member; one
    above the target is not this upgrade's concern.
    """
    return [r for r in table.removals if floor < r.removed_in <= target]


# --- the fleet's current versions -------------------------------------------


@dataclass
class Member:
    label: str
    version: Version | None
    note: str = ""


def members_from_fleet_json(report: dict) -> list[Member]:
    """Phase 1's `members[]`, reduced to a label and the control-plane minor.

    A member whose control-plane version did not parse (Phase 1's `unknown`) is
    kept, with the note, so the report can say it was not measured rather than
    silently narrowing the fleet.
    """
    members: list[Member] = []
    for m in report.get("members", []) or []:
        if not isinstance(m, dict):
            continue
        label = "/".join(str(m.get(k) or "") for k in ("project", "cluster") if m.get(k)) or "?"
        raw = m.get("control_plane_version")
        version = parse_version(raw)
        note = "" if version is not None else f"control plane version unparsable: {raw!r}"
        members.append(Member(label=label, version=version, note=note))
    return members


def fleet_floor(members: list[Member]) -> Version | None:
    """The lowest control-plane minor among the members that parsed."""
    versions = [m.version for m in members if m.version is not None]
    return min(versions) if versions else None


def affected_members(members: list[Member], removed_in: Version) -> list[Member]:
    """Members whose API server still serves the version: current < removed_in."""
    return [m for m in members if m.version is not None and m.version < removed_in]


# --- reading manifests -------------------------------------------------------


@dataclass
class Snapshot:
    """What a reader got from one source: the files, and what it did not get."""

    source: str
    sha: str | None
    files: dict[str, bytes] = field(default_factory=dict)
    skipped: list[dict] = field(default_factory=list)
    stopped: str | None = None


def is_manifest_path(path: str) -> bool:
    return path.lower().endswith(MANIFEST_SUFFIXES)


def proxy_endpoint() -> str:
    return os.environ.get("CREDENTIAL_PROXY_URL", "").strip()


def content_mode_available(endpoint: str | None = None) -> bool:
    """Asked of the broker, as inspect_repository.py asks: the switch is not here."""
    endpoint = proxy_endpoint() if endpoint is None else endpoint
    if not endpoint:
        return False
    return credential_proxy_client.workspaces_available(endpoint)


def read_repo_content(
    repo: str,
    endpoint: str,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    open_workspace: Callable | None = None,
) -> Snapshot:
    """Content mode: page the listing, keep the manifest paths, batch the reads."""
    open_workspace = open_workspace or credential_proxy_client.Workspace.open
    total_bytes = 0
    snapshot = Snapshot(source="", sha=None)
    with open_workspace(endpoint, repo, depth=1) as workspace:
        snapshot.sha = workspace.base_sha or None
        cursor: str | None = None
        batch: list[str] = []
        batch_bytes = 0

        def flush() -> None:
            nonlocal batch, batch_bytes
            pending = batch
            batch = []
            batch_bytes = 0
            while pending:
                files, missed = workspace.read_many(pending)
                snapshot.files.update(files)
                deferred = [m for m in missed if m.get("reason") == REASON_REQUEST_BUDGET]
                snapshot.skipped.extend(m for m in missed if m.get("reason") != REASON_REQUEST_BUDGET)
                if deferred and not files:
                    # Nothing fit, so asking again would loop: the deferral is final.
                    snapshot.skipped.extend(deferred)
                    break
                pending = [m["path"] for m in deferred]

        while snapshot.stopped is None:
            listing = workspace.list(after=cursor)
            if not listing:
                break
            for entry in listing:
                path = entry.get("path", "")
                if not is_manifest_path(path):
                    continue
                size = int(entry.get("size", 0) or 0)
                if len(snapshot.files) + len(batch) >= max_files:
                    snapshot.stopped = STOPPED_MAX_FILES
                    break
                if total_bytes + size > max_bytes:
                    snapshot.stopped = STOPPED_MAX_BYTES
                    break
                total_bytes += size
                batch.append(path)
                batch_bytes += size
                if len(batch) >= BATCH_PATHS or batch_bytes >= BATCH_BYTES:
                    flush()
            flush()
            if not listing.truncated:
                break
            cursor = listing[-1]["path"]
    snapshot.source = SOURCE_REPO.format(sha=snapshot.sha or SHA_UNKNOWN)
    return snapshot


def _runner(cmd: list, *, cwd=None, check: bool = True):
    return subprocess.run(
        cmd, cwd=cwd, check=check, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS
    )


def read_tree(
    root: Path,
    source: str,
    sha: str | None,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Snapshot:
    """Every manifest-shaped file under `root`, in sorted order, within the caps."""
    snapshot = Snapshot(source=source, sha=sha)
    total_bytes = 0
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for name in filenames:
            if is_manifest_path(name):
                paths.append(Path(dirpath) / name)
    for path in sorted(paths):
        if len(snapshot.files) >= max_files:
            snapshot.stopped = STOPPED_MAX_FILES
            break
        relative = str(path.relative_to(root))
        try:
            size = path.stat().st_size
        except OSError as e:
            snapshot.skipped.append({"path": relative, "reason": str(e)})
            continue
        if total_bytes + size > max_bytes:
            snapshot.stopped = STOPPED_MAX_BYTES
            break
        try:
            snapshot.files[relative] = path.read_bytes()
        except OSError as e:
            snapshot.skipped.append({"path": relative, "reason": str(e)})
            continue
        total_bytes += size
    return snapshot


def read_repo_directory(
    repo: str,
    lease: str,
    runner=_runner,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Snapshot:
    """Directory mode: a leased checkout on the shared volume, then `git rev-parse HEAD`."""
    workspace = gitops_workspace.ensure_workspace(repo, runner, lease=lease, reset=True, owner=OWNER)
    head = runner(list(GIT_HEAD_CMD), cwd=str(workspace), check=False)
    sha = (getattr(head, "stdout", "") or "").strip() or None
    return read_tree(Path(workspace), SOURCE_REPO.format(sha=sha or SHA_UNKNOWN), sha, max_files, max_bytes)


def read_local_directory(
    path: str, max_files: int = DEFAULT_MAX_FILES, max_bytes: int = DEFAULT_MAX_BYTES
) -> Snapshot:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    return read_tree(root, SOURCE_LOCAL.format(path=root), None, max_files, max_bytes)


# --- parsing manifests -------------------------------------------------------


@dataclass
class Document:
    path: str
    api_version: str
    kind: str
    name: str


def _documents_of(node, path: str, out: list[Document]) -> None:
    """Collects the manifests in one parsed value, expanding `kind: List` items."""
    if isinstance(node, list):
        for item in node:
            _documents_of(item, path, out)
        return
    if not isinstance(node, dict):
        return
    api_version = node.get(API_VERSION_KEY)
    kind = node.get(KIND_KEY)
    if not isinstance(api_version, str) or not isinstance(kind, str):
        return
    if kind == LIST_KIND and isinstance(node.get(ITEMS_KEY), list):
        for item in node[ITEMS_KEY]:
            _documents_of(item, path, out)
        return
    metadata = node.get(METADATA_KEY)
    name = metadata.get(NAME_KEY) if isinstance(metadata, dict) else None
    out.append(Document(path=path, api_version=api_version, kind=kind, name=str(name) if name else ""))


def parse_manifests(path: str, data: bytes) -> tuple[list[Document], str | None]:
    """(documents, skip_reason). A file that does not parse yields no documents and a reason."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], REASON_ENCODING
    docs: list[Document] = []
    is_json = path.lower().endswith(JSON_SUFFIX)
    # Anything the loader raises is this file's problem, not the repository's.
    # PyYAML's YAMLError is not the whole set: its timestamp constructor raises a
    # bare ValueError for a date that does not exist, and a pathologically nested
    # document raises RecursionError.
    try:
        if is_json:
            parsed = [json.loads(text)] if text.strip() else []
        else:
            parsed = list(yaml.safe_load_all(text))
    except Exception as e:  # noqa: BLE001 - a file that does not parse is listed, never fatal
        if is_json:
            return [], REASON_JSON.format(error=e)
        if TEMPLATE_MARKER in text:
            return [], REASON_TEMPLATE
        first_line = str(e).splitlines()[0] if str(e) else ""
        return [], REASON_YAML.format(error=f"{type(e).__name__}: {first_line}" if first_line else type(e).__name__)
    for node in parsed:
        _documents_of(node, path, docs)
    return docs, None


# --- scanning ----------------------------------------------------------------


def scan_snapshot(snapshot: Snapshot, in_range: list[Removal], members: list[Member]) -> dict:
    """Every hit in one source, plus the counts a clean verdict has to be read with."""
    lookup = {(r.api_version, r.kind): r for r in in_range}
    hits: list[dict] = []
    skipped = list(snapshot.skipped)
    documents = 0
    files_read = 0
    for path in sorted(snapshot.files):
        docs, reason = parse_manifests(path, snapshot.files[path])
        if reason is not None:
            skipped.append({"path": path, "reason": reason})
            continue
        files_read += 1
        documents += len(docs)
        for doc in docs:
            removal = lookup.get((doc.api_version, doc.kind))
            if removal is None:
                continue
            hits.append(
                {
                    "path": doc.path,
                    "kind": doc.kind,
                    "name": doc.name,
                    "api_version": doc.api_version,
                    "removed_in": format_version(removal.removed_in),
                    "replacement": removal.replacement,
                    "members_affected": [m.label for m in affected_members(members, removal.removed_in)],
                }
            )
    return {
        "source": snapshot.source,
        "sha": snapshot.sha,
        "files_read": files_read,
        "files_listed": len(snapshot.files) + len(snapshot.skipped),
        "documents": documents,
        "hits": hits,
        "skipped": skipped,
        "stopped": snapshot.stopped,
        "error": None,
    }


def error_section(message: str) -> dict:
    return {
        "source": None,
        "sha": None,
        "files_read": 0,
        "files_listed": 0,
        "documents": 0,
        "hits": [],
        "skipped": [],
        "stopped": None,
        "error": message,
    }


# --- rendering ---------------------------------------------------------------


def _cell(value) -> str:
    if value is None or value == "" or value == []:
        return EMPTY_CELL
    if isinstance(value, list):
        value = MEMBER_SEPARATOR.join(str(v) for v in value)
    return str(value).replace("|", "\\|")


def render_section(name: str, section: dict, floor: Version, target: Version) -> list[str]:
    if section["error"]:
        return [f"## {name} — not scanned", "", f"- read failed: {section['error']}", ""]
    lines = [f"## {name} — {section['source']}", ""]
    if section["hits"]:
        lines.append("| " + " | ".join(HIT_COLUMNS) + " |")
        lines.append("| " + " | ".join(TABLE_SEPARATOR_CELL for _ in HIT_COLUMNS) + " |")
        for hit in section["hits"]:
            row = (
                hit["path"],
                hit["kind"],
                hit["name"],
                hit["api_version"],
                hit["removed_in"],
                hit["replacement"],
                hit["members_affected"],
            )
            lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
        lines.append("")
        lines.append(
            f"{len(section['hits'])} manifest(s) declare an apiVersion removed after "
            f"{format_version(floor)} up to {format_version(target)}; "
            f"{section['files_read']} file(s), {section['documents']} document(s) read."
        )
    else:
        lines.append(
            f"Clean: no manifest declares an apiVersion removed after {format_version(floor)} "
            f"up to {format_version(target)}; {section['files_read']} file(s), "
            f"{section['documents']} document(s) read."
        )
    if section["stopped"]:
        lines.append(
            f"- partial: the scan stopped at the {section['stopped']} cap; "
            "files past it were not read"
        )
    for skip in section["skipped"]:
        lines.append(f"- skipped {skip['path']}: {skip.get('reason', '')}")
    lines.append("")
    return lines


def render_report(report: dict) -> str:
    floor = tuple(report["floor_version"])
    target = tuple(report["target_version"])
    table = report["table"]
    lines = [
        f"# API deprecation scan: {format_version(floor)} -> {format_version(target)}",
        "",
        f"{len(report['removals_in_range'])} API version(s) removed after {format_version(floor)} "
        f"up to {format_version(target)}, from {TABLE_NAME} (as of Kubernetes {table['as_of']}).",
    ]
    measured = [m for m in report["members"] if m["version"]]
    unmeasured = [m for m in report["members"] if not m["version"]]
    if measured:
        lines.append(
            "Members: "
            + MEMBER_SEPARATOR.join(f"{m['label']} ({format_version(tuple(m['version']))})" for m in measured)
        )
    if unmeasured:
        lines.append(
            "Not measured: " + MEMBER_SEPARATOR.join(f"{m['label']} ({m['note']})" for m in unmeasured)
        )
    for note in report["notes"]:
        lines.append(f"Note: {note}")
    lines.append("")
    if not report["sources"]:
        lines.extend(
            [
                NO_REPOS_HEADING,
                "",
                "No GitHub repository is registered under managed_repos, so there are no "
                "manifests to scan. Name one with --repo, or a local tree with --manifests-dir.",
                "",
            ]
        )
    for name, section in report["sources"].items():
        lines.extend(render_section(name, section, floor, target))
    lines.extend(
        [
            "---",
            "",
            f"Scope: apiVersions declared in Git, matched against {TABLE_NAME} "
            f"(as of Kubernetes {table['as_of']}, source {table['source_url']}). "
            "Files that did not parse are listed as skipped above and were not scanned. "
            "Live client usage of deprecated APIs is GKE Deprecation Insights' job, not this "
            f"script's: {DEPRECATION_INSIGHTS_URL} in the console, or "
            f"`{DEPRECATION_INSIGHTS_COMMAND}` run by a human; this script does not run it.",
        ]
    )
    return "\n".join(lines)


# --- assembly ----------------------------------------------------------------


def build_report(
    table: Table,
    floor: Version,
    target: Version,
    members: list[Member],
    readers: list[tuple[str, Callable[[], Snapshot]]],
) -> dict:
    """Runs every reader and grades what it returned; one failure never aborts the rest."""
    in_range = removals_in_range(table, floor, target)
    sources: dict[str, dict] = {}
    for name, reader in readers:
        try:
            sources[name] = scan_snapshot(reader(), in_range, members)
        except Exception as e:  # noqa: BLE001 - a broker or git failure is a report, not a crash
            sources[name] = error_section(f"{type(e).__name__}: {e}")
    notes: list[str] = []
    if target > table.as_of:
        notes.append(
            TABLE_STALE_NOTE.format(target=format_version(target), table=TABLE_NAME, as_of=format_version(table.as_of))
        )
    if target <= floor:
        notes.append(
            f"target {format_version(target)} is not above the fleet's current floor "
            f"{format_version(floor)}; no removal lies in range"
        )
    return {
        "floor_version": list(floor),
        "target_version": list(target),
        "table": {"as_of": format_version(table.as_of), "source_url": table.source_url, "path": str(TABLE_PATH)},
        "removals_in_range": [
            {
                "api_version": r.api_version,
                "kind": r.kind,
                "removed_in": format_version(r.removed_in),
                "replacement": r.replacement,
            }
            for r in in_range
        ],
        "members": [{"label": m.label, "version": list(m.version) if m.version else None, "note": m.note} for m in members],
        "notes": notes,
        "sources": sources,
        "summary": {
            "sources": len(sources),
            "hits": sum(len(s["hits"]) for s in sources.values()),
            "skipped": sum(len(s["skipped"]) for s in sources.values()),
            "errors": sum(1 for s in sources.values() if s["error"]),
        },
    }


def scan_lease(explicit: str | None) -> str:
    """The directory-mode lease: `--lease` as given, else the session's with the scan prefix."""
    if explicit and str(explicit).strip():
        return gitops_workspace.lease_id(explicit)
    return gitops_workspace.sanitize_lease(SCAN_LEASE_PREFIX + gitops_workspace.lease_id(None))


def build_readers(args, endpoint: str, content_mode: bool) -> list[tuple[str, Callable[[], Snapshot]]]:
    """One (name, reader) per source: local trees, then repositories in the mode the broker allows."""
    readers: list[tuple[str, Callable[[], Snapshot]]] = []
    for path in args.manifests_dir or []:
        readers.append((path, lambda p=path: read_local_directory(p, args.max_files, args.max_bytes)))
    repos = list(args.repo or [])
    if not repos and not args.manifests_dir:
        repos = gitops_workspace.get_managed_github_repos()
    lease = scan_lease(args.lease) if repos and not content_mode else None
    for repo in repos:
        if content_mode:
            readers.append((repo, lambda r=repo: read_repo_content(r, endpoint, args.max_files, args.max_bytes)))
        else:
            readers.append((repo, lambda r=repo: read_repo_directory(r, lease, _runner, args.max_files, args.max_bytes)))
    return readers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kubernetes API removals between the fleet's current version and a target, read from GitOps manifests."
    )
    parser.add_argument("--target-version", help="Target GKE version, e.g. 1.27.3-gke.100 or 1.27. Defaults to the --versions file's target_version.")
    parser.add_argument("--versions", help="fleet_upgrade_report.py --output JSON; the floor is its lowest control-plane minor.")
    parser.add_argument("--current-version", help="The fleet's current version when there is no --versions file.")
    parser.add_argument("--repo", action="append", help="owner/name to scan; repeatable. Default: every managed_repos GitHub entry.")
    parser.add_argument("--manifests-dir", action="append", help="Local directory to scan instead of, or as well as, repositories; repeatable.")
    parser.add_argument("--lease", help="Directory mode only: the workspace lease to check the repository out under. Default: a scan-private lease derived from the session's.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES, help="Manifest files read per source before the scan stops and says so.")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Bytes read per source before the scan stops and says so.")
    parser.add_argument("--output", help="Path to write the report as JSON.")
    args = parser.parse_args(argv)

    try:
        table = load_table()
    except (OSError, ValueError) as e:
        sys.stderr.write(f"removal table unusable: {e}\n")
        return EXIT_USAGE

    members: list[Member] = []
    fleet_target = None
    if args.versions and args.current_version:
        sys.stderr.write("pass --versions or --current-version, not both\n")
        return EXIT_USAGE
    if args.versions:
        try:
            with open(args.versions, encoding="utf-8") as f:
                fleet = json.load(f)
        except (OSError, ValueError) as e:
            sys.stderr.write(f"--versions {args.versions}: {e}\n")
            return EXIT_USAGE
        members = members_from_fleet_json(fleet if isinstance(fleet, dict) else {})
        fleet_target = fleet.get("target_version") if isinstance(fleet, dict) else None
        floor = fleet_floor(members)
        if floor is None:
            sys.stderr.write(f"--versions {args.versions}: no member's control-plane version parsed; pass --current-version\n")
            return EXIT_USAGE
    elif args.current_version:
        floor = parse_version(args.current_version)
        if floor is None:
            sys.stderr.write(f"--current-version {args.current_version!r} is not MAJOR.MINOR[.PATCH][-gke.BUILD]\n")
            return EXIT_USAGE
    else:
        sys.stderr.write("pass --versions <fleet_upgrade_report.py --output JSON> or --current-version\n")
        return EXIT_USAGE

    target_text = args.target_version or fleet_target
    if not target_text:
        sys.stderr.write("pass --target-version (the --versions file carries none)\n")
        return EXIT_USAGE
    target = parse_version(target_text)
    if target is None:
        sys.stderr.write(f"--target-version {target_text!r} is not MAJOR.MINOR[.PATCH][-gke.BUILD]\n")
        return EXIT_USAGE

    endpoint = proxy_endpoint()
    # The broker is asked only when a repository will be read: a --manifests-dir
    # run on a workstation has no broker to ask.
    reads_repos = bool(args.repo) or not args.manifests_dir
    content_mode = reads_repos and content_mode_available(endpoint)
    readers = build_readers(args, endpoint, content_mode)
    report = build_report(table, floor, target, members, readers)
    print(render_report(report))

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=JSON_INDENT)
            print(f"\nWrote {report['summary']['sources']} source(s) to {args.output}")
        except OSError as e:
            sys.stderr.write(f"failed to write {args.output}: {e}\n")
            return EXIT_PARTIAL

    return EXIT_PARTIAL if report["summary"]["errors"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
