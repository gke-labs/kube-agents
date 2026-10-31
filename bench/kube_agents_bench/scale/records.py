"""The measurement records and the data file they merge into.

The file's schema is documented in ``bench/scale/README.md``. A record's key
names one run of one operation at one level; merging replaces a record with the
same key and keeps every other, so the file accumulates history across commits.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
KEY_SEPARATOR = "|"
COMMIT_KEY_LENGTH = 8
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
JSON_INDENT = 2


def record_key(commit: str, mode: str, factor: str, level: float, operation: str, run: int) -> str:
    """The identity of one run: replaced on re-run, distinct across builds.

    A commit SHA is shortened; any other build name (a release tag such as
    ``rc_2610220514_eeb8a95``) is kept whole, because its prefix is shared by
    other builds.
    """
    build = commit[:COMMIT_KEY_LENGTH] if FULL_SHA_RE.match(commit) else commit
    return KEY_SEPARATOR.join([build, mode, factor, _level_text(level), operation, str(run)])


def _level_text(level: float) -> str:
    return str(int(level)) if float(level).is_integer() else str(level)


@dataclass
class Metric:
    name: str
    value: float
    container: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "value": self.value}
        if self.container:
            data["container"] = self.container
        return data


@dataclass
class Record:
    commit: str
    mode: str
    phase: str
    factor: str
    level: float
    operation: str
    run: int
    started: str
    ended: str
    image: str = ""
    # The measurement runner's own commit, so a change in how a metric is read is visible.
    runner: str = ""
    # Every factor's planned value at this point; totals hold what the clusters really had.
    values: dict[str, float] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    limits: dict[str, dict[str, float]] = field(default_factory=dict)
    # Per container name, how many running pods carried it.
    replicas: dict[str, int] = field(default_factory=dict)
    metrics: list[Metric] = field(default_factory=list)
    provider_429s: int = 0
    # The start of each prompt's answer, so a surprising response time can be checked against what was said.
    answers: list[str] = field(default_factory=list)
    excluded_from: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return record_key(self.commit, self.mode, self.factor, self.level, self.operation, self.run)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Record:
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "metrics"}
        record = cls(**fields)
        record.metrics = [Metric(m["name"], m["value"], m.get("container")) for m in data.get("metrics", [])]
        return record

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["metrics"] = [metric.to_dict() for metric in self.metrics]
        return {"key": self.key, **data}


def load(path: Path) -> dict[str, Any]:
    """The data file, or an empty one when it does not exist yet."""
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "records": []}
    data = json.loads(path.read_text())
    if data.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"{path}: schema {data.get('schema')!r}, expected {SCHEMA_VERSION}")
    return data


def existing_keys(path: Path) -> set[str]:
    return {record["key"] for record in load(path)["records"]}


def merge(path: Path, new: list[Record]) -> int:
    """Merge ``new`` into the file at ``path``; returns how many records replaced old ones.

    The write is atomic (a temporary file renamed over the old one), so an
    interrupted run never leaves a truncated file behind.
    """
    data = load(path)
    by_key = {record["key"]: record for record in data["records"]}
    replaced = 0
    for record in new:
        if record.key in by_key:
            replaced += 1
        by_key[record.key] = record.to_dict()
    data["records"] = sorted(by_key.values(), key=lambda record: (record["started"], record["key"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w") as stream:
        json.dump(data, stream, indent=JSON_INDENT, sort_keys=False)
        stream.write("\n")
    os.replace(temporary, path)
    return replaced
