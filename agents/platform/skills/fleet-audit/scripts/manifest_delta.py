#!/usr/bin/env python3
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The delta gate a scheduled fleet audit reads after its collector.

A scheduled governance audit runs its collector and then has the model read
the whole SOP, copy the manifest into a findings document and call `finish`,
before anything tells it the fleet has not changed since the last scheduled
run. This module is the deterministic half of the gate `audit_report.py
precheck` applies: `manifest_digest` hashes what the manifest says about the
fleet and leaves out what only says when and how long the collector ran, and
`should_run_llm_synthesis` compares that digest with the one the last
scheduled `finish` of the stream recorded.

The record is one JSON file per stream under the scratch directory
(`digest_path_for`), written by `record_scheduled_digest` after a scheduled
`finish` published. A `finish` that `precheck` ran from the stored document
keeps the previous `last_full_run_at`, so a static fleet still gets a full
synthesis once every `DEFAULT_MAX_SILENT_AGE_SECONDS`.

What the digest keeps is a drop-list, not a keep-list: every key of the
manifest and of each cluster entry is hashed unless it is named volatile
here, so a field a collector adds later counts as a change until this file
says otherwise. A change the digest misses would silence a finding; a change
it over-counts costs one full run.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# The same default as audit_report.SCRATCH_DIR; audit_report passes its own,
# so a test that patches one does not read the other.
SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"
DIGEST_FILE_TEMPLATE = "last_scheduled_digest_{audit_id}.json"
# A static fleet gets a full synthesis at least this often.
DEFAULT_MAX_SILENT_AGE_SECONDS = 7 * 86400

VERDICT_FIRST_RUN = "first-run"
VERDICT_FINDINGS_CHANGED = "findings-changed"
VERDICT_PERIODIC_REFRESH = "periodic-refresh"
VERDICT_UNCHANGED = "unchanged"

# The record's keys.
RECORD_VERSION = 1
RECORD_VERSION_KEY = "version"
RECORD_AUDIT_KEY = "audit"
RECORD_DIGEST_KEY = "digest"
RECORD_CHECKS_REVISION_KEY = "checks_revision"
RECORD_RECORDED_AT_KEY = "recorded_at"
RECORD_LAST_FULL_RUN_AT_KEY = "last_full_run_at"
# The `finished_at` of the envelope the recording `finish` stored, so the next
# run's `precheck` republishes the document of the run whose digest it read.
RECORD_ENVELOPE_KEY = "envelope_finished_at"

# Manifest keys that say when the collector ran, not what it found.
VOLATILE_MANIFEST_KEYS = frozenset({"started_at", "finished_at"})
# Per-command keys that say how long a read took and what bytes it returned,
# and the literal command, whose kubeconfig and workspace paths are per run.
# The `check` and `rc` of each command are what `finish` reads, and they stay.
VOLATILE_COMMAND_KEYS = frozenset({"command", "duration_s", "output_sha256"})
# Per-candidate keys that are prose about a finding rather than the finding:
# the collectors put ages ("AGE=12d"), log-window stamps and counts in them,
# so a static fleet would digest differently every day. A candidate counts by
# everything else it carries (check, cluster, namespace, object, severity, the
# route and triage fields), and `finish` adopts the fresh manifest's excerpt
# and authoritative impact on a close, so the ledger shows today's text.
VOLATILE_CANDIDATE_KEYS = frozenset({"excerpt", "impact"})
CANDIDATES_KEY = "candidates"
COMMANDS_KEY = "commands"
CLUSTERS_KEY = "clusters"
CHECKS_UNEVALUATED_KEY = "checks_unevaluated"
CHECKS_REVISION_KEY = "checks_revision"
OUTCOME_KEY = "outcome"
ERROR_KEY = "error"
# The outcomes under which a target is not a gap: the collector read it, or
# ruled it out of this audit. Any other outcome is a target the run did not
# read, and a run with one is never closed from a stored document.
SETTLED_OUTCOMES = frozenset({"collected", "out-of-scope"})
DIGEST_ALGORITHM = "sha256"
RECORD_FILE_MODE = 0o644


def digest_path_for(audit_id: str, scratch_dir: str | os.PathLike | None = None) -> Path:
    """Where the last scheduled run's digest of `audit_id` is kept."""
    return Path(scratch_dir if scratch_dir is not None else SCRATCH_DIR) / DIGEST_FILE_TEMPLATE.format(audit_id=audit_id)


def _normalized_commands(commands: object) -> list:
    """Each command's non-volatile keys, sorted so order does not count."""
    if not isinstance(commands, list):
        return [commands]
    rows = []
    for command in commands:
        if isinstance(command, dict):
            rows.append({k: v for k, v in command.items() if k not in VOLATILE_COMMAND_KEYS})
        else:
            rows.append(command)
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def _normalized_candidates(candidates: object) -> list:
    """Each candidate without its prose keys, sorted so order does not count."""
    if not isinstance(candidates, list):
        return [candidates]
    rows = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            rows.append({k: v for k, v in candidate.items() if k not in VOLATILE_CANDIDATE_KEYS})
        else:
            rows.append(candidate)
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def _normalized_entry(entry: dict) -> dict:
    out = {}
    for key, value in entry.items():
        if key == COMMANDS_KEY:
            out[key] = _normalized_commands(value)
        elif key == CANDIDATES_KEY:
            out[key] = _normalized_candidates(value)
        else:
            out[key] = value
    return out


def normalized_manifest(manifest: dict) -> dict:
    """`manifest` without the keys that change on every run of the same fleet.

    Top-level timestamps go; each cluster entry keeps every key but reshapes
    `commands` and `candidates` to their non-volatile fields. Cluster entries
    are sorted by their JSON form so the collector's enumeration order does
    not count either.
    """
    out: dict = {}
    for key, value in manifest.items():
        if key in VOLATILE_MANIFEST_KEYS:
            continue
        if key == CLUSTERS_KEY and isinstance(value, list):
            entries = []
            for entry in value:
                if isinstance(entry, dict):
                    entries.append(_normalized_entry(entry))
                else:
                    entries.append(entry)
            out[key] = sorted(entries, key=lambda e: json.dumps(e, sort_keys=True, default=str))
            continue
        out[key] = value
    return out


def manifest_digest(manifest: dict) -> str:
    """A hex digest of what the manifest says about the fleet.

    Two manifests of an unchanged fleet digest the same whatever their
    timestamps, command durations, output hashes, command strings, candidate
    excerpts and impacts, and cluster, command or candidate order; a candidate
    added or removed, a severity, an outcome, a limitation, a
    `checks_not_applicable` or `checks_unevaluated` entry, or a new
    `checks_revision` digests differently.
    """
    canonical = json.dumps(normalized_manifest(manifest), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.new(DIGEST_ALGORITHM, canonical.encode("utf-8")).hexdigest()


def _cluster_entries(manifest: dict) -> list[dict]:
    clusters = manifest.get(CLUSTERS_KEY)
    return [c for c in clusters if isinstance(c, dict)] if isinstance(clusters, list) else []


def manifest_findings_changed_reason(manifest: dict) -> str | None:
    """Why a manifest can never close a run from a stored document, or None.

    A top-level `error`, a target the collector did not read (an outcome
    outside `SETTLED_OUTCOMES`), or a check it could not evaluate, each means
    the run is partial in a way only a full run accounts for.
    """
    if manifest.get(ERROR_KEY):
        return "the manifest carries a top-level error"
    for entry in _cluster_entries(manifest):
        name = str(entry.get("name") or "?")
        if entry.get(OUTCOME_KEY) not in SETTLED_OUTCOMES:
            return f"{name} was not collected ({entry.get(OUTCOME_KEY)!r})"
        if entry.get(CHECKS_UNEVALUATED_KEY):
            return f"{name} has checks_unevaluated"
    return None


def _parse_time(value: object) -> float | None:
    """An epoch float from a record's ISO-8601 stamp, or None when it is not one."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def read_record(audit_id: str, scratch_dir: str | os.PathLike | None = None) -> dict | None:
    """The record of the last scheduled run, or None when there is no usable one.

    Missing, unreadable, not an object, written for another stream, or
    without a digest string all read as no record, so the run is a first run.
    """
    path = digest_path_for(audit_id, scratch_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get(RECORD_AUDIT_KEY) != audit_id:
        return None
    if not isinstance(data.get(RECORD_DIGEST_KEY), str) or not data[RECORD_DIGEST_KEY]:
        return None
    return data


def should_run_llm_synthesis(
    audit_id: str,
    manifest: dict,
    now: float,
    max_silent_age_seconds: int = DEFAULT_MAX_SILENT_AGE_SECONDS,
    scratch_dir: str | os.PathLike | None = None,
) -> tuple[bool, str]:
    """Whether the model has to draft this run's findings, and why.

    `(True, "first-run")` with no record; `(True, "findings-changed")` when
    the digest differs from the recorded one or the manifest itself can never
    close a run (`manifest_findings_changed_reason`); `(True,
    "periodic-refresh")` when the last full synthesis is `max_silent_age_seconds`
    or more in the past, or its time is not readable; `(False, "unchanged")`
    otherwise. `now` is an epoch float.
    """
    record = read_record(audit_id, scratch_dir)
    if record is None:
        return True, VERDICT_FIRST_RUN
    if manifest_findings_changed_reason(manifest) is not None:
        return True, VERDICT_FINDINGS_CHANGED
    if manifest_digest(manifest) != record[RECORD_DIGEST_KEY]:
        return True, VERDICT_FINDINGS_CHANGED
    last_full = _parse_time(record.get(RECORD_LAST_FULL_RUN_AT_KEY))
    if last_full is None or now - last_full >= max_silent_age_seconds:
        return True, VERDICT_PERIODIC_REFRESH
    return False, VERDICT_UNCHANGED


def record_scheduled_digest(
    audit_id: str,
    manifest: dict,
    now: float,
    *,
    full_run: bool,
    scratch_dir: str | os.PathLike | None = None,
    envelope_finished_at: str | None = None,
) -> Path:
    """Write the record a scheduled `finish` leaves for the next run's gate.

    `full_run` says the model drafted this run's document, so
    `last_full_run_at` moves to `now`; a `finish` that `precheck` ran from the
    stored document keeps the previous value (or sets it to `now` when there
    was none, which only a record written by hand can produce).
    `envelope_finished_at` names the stored envelope of the recording run,
    which `precheck` holds the document it republishes to. The write is
    atomic, and the file is the only state the gate reads.
    """
    previous = read_record(audit_id, scratch_dir)
    last_full = now
    if not full_run and previous is not None:
        kept = _parse_time(previous.get(RECORD_LAST_FULL_RUN_AT_KEY))
        if kept is not None:
            last_full = kept
    record = {
        RECORD_VERSION_KEY: RECORD_VERSION,
        RECORD_AUDIT_KEY: audit_id,
        RECORD_DIGEST_KEY: manifest_digest(manifest),
        RECORD_CHECKS_REVISION_KEY: manifest.get(CHECKS_REVISION_KEY),
        RECORD_RECORDED_AT_KEY: _iso(now),
        RECORD_LAST_FULL_RUN_AT_KEY: _iso(last_full),
        **({RECORD_ENVELOPE_KEY: envelope_finished_at} if envelope_finished_at else {}),
    }
    path = digest_path_for(audit_id, scratch_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            json.dump(record, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.chmod(tmp_name, RECORD_FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
