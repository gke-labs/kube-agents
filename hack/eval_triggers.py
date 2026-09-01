#!/usr/bin/env python3
"""Map a pull request's changed files to the eval tasks that must run.

The mapping is data in hack/eval_triggers.yaml; this module supplies the
bucket behaviours and the fail-closed frame. hack/ci-eval-pr.sh pipes
`git diff --name-only` in and passes the active task names as arguments;
stdout is a verdict (ALL, NONE, or SUBSET plus one name per line), stderr
says which bucket claimed each file. Every failure mode -- unowned path,
disallowed extension, unreadable config, crash -- widens to ALL.

Stdlib only: this runs before the job installs anything, which is also why
the config is read by the restricted parser below instead of PyYAML.
"""

from __future__ import annotations

import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).with_suffix(".yaml")

# The sentinel a bucket returns to demand the full matrix.
ALL = object()

# The config grammar's three indent levels: list items under a top-level
# key, a bucket's fields, and entries of a bucket's lists.
ITEM_INDENT, FIELD_INDENT, LIST_INDENT = 2, 4, 6


def _compile(pattern: str) -> re.Pattern:
    """Gitignore-flavoured glob: `**` crosses slashes, `*`/`?` do not."""
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
    """One rule: files matching my patterns trigger the tasks I name."""

    def __init__(self, name: str, patterns: list[str], extensions: list[str]):
        self.name = name
        self._rules = [(p, _compile(p)) for p in patterns]
        self._extensions = {e.lstrip(".").lower() for e in extensions}

    def owns(self, path: str) -> bool:
        # The extension gate applies to wildcard patterns only; a literal
        # pattern names one exact file.
        for pattern, rx in self._rules:
            if not rx.match(path):
                continue
            if not any(c in pattern for c in "*?") or not self._extensions:
                return True
            ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
            if ext in self._extensions:
                return True
        return False

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


def _scalar(text: str, where: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    elif " #" in text:
        # Refuse rather than mis-parse: an inline comment kept as data would
        # match nothing, and on `floor` that silently drops coverage.
        raise ValueError(f"{where}: inline comments are not supported")
    if not text:
        raise ValueError(f"{where}: empty value")
    return text


def load_config(path: Path):
    """Read the restricted subset eval_triggers.yaml is written in; raise on
    anything else, which the caller turns into the full matrix."""
    floor: list[str] = []
    buckets: list[dict] = []
    section = None
    listing = None
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        where = f"{path.name}:{lineno}"
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0 and line in ("floor:", "buckets:"):
            section, listing = line[:-1], None
        elif section == "floor" and indent == ITEM_INDENT and line.startswith("- "):
            floor.append(_scalar(line[2:], where))
        elif section == "buckets" and indent == ITEM_INDENT and line.startswith("- name:"):
            buckets.append({"name": _scalar(line[len("- name:"):], where), "patterns": [], "extensions": []})
            listing = None
        elif section == "buckets" and buckets and indent == FIELD_INDENT and line in ("patterns:", "extensions:"):
            listing = line[:-1]
        elif section == "buckets" and buckets and indent == FIELD_INDENT and line.startswith("kind:"):
            buckets[-1]["kind"] = _scalar(line[len("kind:"):], where)
            listing = None
        elif section == "buckets" and buckets and listing and indent == LIST_INDENT and line.startswith("- "):
            buckets[-1][listing].append(_scalar(line[2:], where))
        else:
            raise ValueError(f"{where}: unrecognised line {line!r}")
    built = []
    for b in buckets:
        if b.get("kind") not in KINDS:
            raise ValueError(f"bucket {b['name']!r}: unknown kind {b.get('kind')!r}")
        if not b["patterns"]:
            raise ValueError(f"bucket {b['name']!r}: no patterns")
        built.append(KINDS[b["kind"]](b["name"], b["patterns"], b["extensions"]))
    if not built:
        raise ValueError(f"{path.name}: no buckets")
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
