#!/usr/bin/env python3
"""Map a pull request's changed files to the eval tasks that must run.

The mapping is data in hack/eval_triggers.yaml, written in the shape of a
GitHub Actions path filter: named buckets, each with an ordered `paths`
list where `!` negates and the last matching pattern wins. This module
supplies the bucket behaviours and the fail-closed frame.

hack/ci-eval-pr.sh pipes `git diff --name-only` in and passes the active
task names as arguments; stdout is a verdict (ALL, NONE, or SUBSET plus
one name per line), stderr says which bucket claimed each file. Every
failure mode -- unowned path, disallowed extension, unreadable config,
missing PyYAML, crash -- widens to ALL: the shell side keeps the full
matrix on any non-zero exit.
"""

from __future__ import annotations

import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).with_suffix(".yaml")

# The sentinel a bucket returns to demand the full matrix.
ALL = object()


def _compile(pattern: str) -> re.Pattern:
    """GitHub-Actions-flavoured glob: `**` crosses slashes, `*`/`?` do not.

    Hand-translated because the runner image's Python (3.11) predates
    glob.translate(), and fnmatch lets `*` cross `/`.
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Bucket(ABC):
    """One rule: files matching my paths trigger the tasks I name."""

    def __init__(self, name: str, paths: list[str], extensions: list[str]):
        self.name = name
        self._rules = [
            (p.lstrip("!"), _compile(p.lstrip("!")), p.startswith("!")) for p in paths
        ]
        self._extensions = {e.lstrip(".").lower() for e in extensions}

    def owns(self, path: str) -> bool:
        """Last matching pattern wins, as in a workflow's `paths:` filter.

        The extension gate applies to positive wildcard patterns only: a
        literal pattern names one exact file, and a negation only takes
        ownership away."""
        owned = False
        for pattern, rx, negated in self._rules:
            if not rx.match(path):
                continue
            if negated:
                owned = False
            elif not any(c in pattern for c in "*?") or not self._extensions:
                owned = True
            else:
                name = path.rsplit("/", 1)[-1]
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext in self._extensions:
                    owned = True
        return owned

    @abstractmethod
    def tasks_for(self, path: str, active: list[str]):
        """Task names `path` triggers, or ALL. Only called when owns(path)."""


class NoTasks(Bucket):
    """Files that cannot change agent behaviour trigger nothing."""

    def tasks_for(self, path: str, active: list[str]):
        return set()


class TaskDir(Bucket):
    """bench/tasks/<name>/... triggers <name>; inactive tasks trigger nothing."""

    def tasks_for(self, path: str, active: list[str]):
        task = path.split("/")[2]
        return {task} if task in active else set()


class StackDir(Bucket):
    """bench/tf/prebuilt/<stack>/... triggers the active tasks declaring that
    stack in task.yaml (same regex as ci-eval-pr.sh's task_stack()); a stack
    no active task declares gets the full matrix."""

    _STACK_RE = re.compile(r"^\s*stack:\s*(.+?)\s*$", re.M)

    def _stack_of(self, task: str) -> str:
        try:
            text = (REPO_ROOT / "bench" / "tasks" / task / "task.yaml").read_text()
        except OSError:
            return ""
        m = self._STACK_RE.search(text)
        return m.group(1).strip("'\"") if m else ""

    def tasks_for(self, path: str, active: list[str]):
        stack = "prebuilt/" + path.split("/")[3]
        users = {t for t in active if self._stack_of(t) == stack}
        return users if users else ALL


KINDS = {"no-tasks": NoTasks, "task-dir": TaskDir, "stack-dir": StackDir}

BUCKET_KEYS = {"kind", "paths", "extensions"}


def _str_list(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{where}: expected a list of non-empty strings, got {value!r}")
    return value


def load_config(path: Path):
    """yaml.safe_load plus a strict schema check; raise on anything off,
    which the caller turns into the full matrix."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or set(data) != {"floor", "buckets"}:
        raise ValueError(f"{path.name}: top level must be exactly floor + buckets")
    floor = _str_list(data["floor"], "floor")
    if not isinstance(data["buckets"], dict) or not data["buckets"]:
        raise ValueError(f"{path.name}: buckets must be a non-empty mapping")
    built = []
    for name, spec in data["buckets"].items():
        if not isinstance(spec, dict) or not BUCKET_KEYS >= set(spec):
            raise ValueError(f"bucket {name!r}: keys must be within {sorted(BUCKET_KEYS)}")
        if spec.get("kind") not in KINDS:
            raise ValueError(f"bucket {name!r}: unknown kind {spec.get('kind')!r}")
        paths = _str_list(spec.get("paths"), f"bucket {name!r} paths")
        if not any(not p.startswith("!") for p in paths):
            raise ValueError(f"bucket {name!r}: needs at least one positive path")
        extensions = _str_list(spec.get("extensions", []), f"bucket {name!r} extensions")
        built.append(KINDS[spec["kind"]](name, paths, extensions))
    return floor, built


class Selector:
    """First bucket to own a path decides it; unowned paths and ALL answers
    widen the whole selection to the full matrix."""

    def __init__(self, buckets: list[Bucket], floor: list[str]):
        self.buckets = buckets
        self.floor = floor

    def select(self, changed: list[str], active: list[str]):
        picked: set[str] = set()
        for path in changed:
            bucket = next((b for b in self.buckets if b.owns(path)), None)
            if bucket is None:
                print(f"  {path} -> no bucket: full matrix", file=sys.stderr)
                return ALL
            tasks = bucket.tasks_for(path, active)
            if tasks is ALL:
                print(f"  {path} -> {bucket.name}: full matrix", file=sys.stderr)
                return ALL
            print(f"  {path} -> {bucket.name}: {sorted(tasks) or 'nothing'}", file=sys.stderr)
            picked |= tasks
        if picked:
            # No selected run skips the delegation chain entirely.
            picked |= set(self.floor) & set(active)
        return picked


def main() -> int:
    active = sys.argv[1:]
    if not active:
        print("usage: git diff --name-only ... | eval_triggers.py <active task names>", file=sys.stderr)
        return 2
    changed = [line.strip() for line in sys.stdin if line.strip()]
    if not changed:
        # An empty diff is an input problem, not a docs-only PR.
        print("  empty diff -> full matrix", file=sys.stderr)
        print("ALL")
        return 0
    floor, buckets = load_config(CONFIG)
    result = Selector(buckets, floor).select(changed, active)
    # The gate's blocking rungs arm on admitted cases alone (EVAL_ADMITTED_CASES,
    # exported from BOOTSTRAP_ADMITTED by ci-eval-pr.sh), so a subset without
    # one could never red the job. Guarantee ONE, not the whole roster -- since
    # #1096 that is most of the matrix, and unioning it would defeat selection.
    # Read at runtime rather than mirrored in the yaml so admission keeps a
    # single home.
    admitted = [c for c in re.split(r"[,\s]+", os.environ.get("EVAL_ADMITTED_CASES", "")) if c]
    if result is not ALL and result and not result & set(admitted):
        keep = next((c for c in admitted if c in active), None)
        if keep:
            result.add(keep)
    if result is ALL:
        print("ALL")
    elif not result:
        print("NONE")
    else:
        print("SUBSET")
        # Keep the matrix's reporting order.
        for task in active:
            if task in result:
                print(task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
