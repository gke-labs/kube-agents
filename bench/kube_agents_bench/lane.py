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

"""Lane-level safeguards: ``verification_spec`` entries every case on one
lane carries, appended to a copy of each task file before devops-bench reads it.

A safeguard that belongs to a lane rather than to a case -- the inject lane's
"the agent wrote nothing to GitHub the case did not ask for" (#2079) -- has
no home in fifty task files, and a per-case edit would change what the api
lane grades too. devops-bench reads a task's checks from its ``task.yaml``
and nothing else, and it records the case id as the directory the file sits
in, so the lane materialises ``<out>/<case>/task.yaml``: the case's own
document with the lane's entries appended to its ``verification_spec``, and
hands devops-bench that path. The task file under ``bench/tasks/`` is not
touched, the scorer still reads it (the appended entries reach the record
through the report, which is what rung 1 grades), and on any other lane
nothing here runs.

The file is ``hack/eval/inject-lane-safeguards.yaml``, read by
``hack/ci-eval-pr.sh`` beside the lane's exclusions; its shape is one key,
``safeguards``, holding entries in the case format's own vocabulary.
``scripts/test_eval_rosters.py`` holds the file to that shape and to the
lane's rules.

Two things a copy does that a plain append would not. An entry whose name a
case already declares would be refused by devops-bench as a duplicate -- a
parse error that reds every repetition of that case at rung 2, after the
cluster lease -- so the collision is refused here, before it. And a case that requests a pull
request (a leaf of a type in :data:`REQUESTING_CHECK_TYPES` in its own spec)
has that many requested writes: every ``github_writes`` leaf appended to it
gets ``requested_pull_requests`` set to that count, so the lane's safeguard
leaves the case's own pull request out and fails the repetition on anything
beyond it. The command line also reports that count per case, which the
script exports as ``BENCH_REQUESTING_CASES`` so a sibling repetition can
attribute a write made during such a case's unit to it
(:mod:`kube_agents_bench.github_writes`, attribution).
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "LaneSafeguardsError",
    "append_lane_safeguards",
    "load_lane_safeguards",
    "main",
    "requested_pull_requests",
]

#: The one top-level key of a lane safeguards file.
SAFEGUARDS_KEY = "safeguards"
#: The keys a check subtree nests children under, as ``cases.py`` walks them.
CHECK_CHILD_KEYS = ("checks", "check")
#: The check types that request a pull request -- ``pull_request_opened``
#: (the remediation cases) and ``pull_request_diff_contains`` (a proposal
#: graded on the diff the reply points at, #2079 item 2) -- and the check
#: type whose allowance the lane sets from them.
REQUESTING_CHECK_TYPES = frozenset({"pull_request_opened", "pull_request_diff_contains"})
WRITES_CHECK_TYPE = "github_writes"
REQUESTED_FIELD = "requested_pull_requests"
#: What a task file names its checks under, and the file the copy is written as.
SPEC_KEY = "verification_spec"
TASK_FILE = "task.yaml"


class LaneSafeguardsError(ValueError):
    """A lane file or a task the lane cannot be applied to honestly."""


def _leaves(node: Any) -> list[dict[str, Any]]:
    """Every leaf check mapping in a subtree, in order."""
    if isinstance(node, dict):
        children = [node.get(key) for key in CHECK_CHILD_KEYS if node.get(key) is not None]
        if not children:
            return [node]
        return [leaf for child in children for leaf in _leaves(child)]
    if isinstance(node, list):
        return [leaf for item in node for leaf in _leaves(item)]
    return []


def load_lane_safeguards(path: str | Path) -> list[dict[str, Any]]:
    """The entries of one lane safeguards file, validated for shape.

    Shape only: a mapping with ``safeguards`` holding a list of mappings, each
    with a ``name``, a ``role`` of ``safeguard`` and a ``check``. What the
    entries assert is the roster test's to pin; devops-bench validates the
    rest at spec load, and a lane entry it refused would surface as a parse
    error on every case, which is loud.
    """
    file = Path(path)
    if not file.is_file():
        raise LaneSafeguardsError(f"{file}: no such lane safeguards file")
    try:
        doc = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LaneSafeguardsError(f"{file}: not parseable as YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get(SAFEGUARDS_KEY), list):
        raise LaneSafeguardsError(
            f"{file}: expected a mapping with a `{SAFEGUARDS_KEY}:` list at the top level"
        )
    entries = doc[SAFEGUARDS_KEY]
    names: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{file}: {SAFEGUARDS_KEY}[{index}]"
        if not isinstance(entry, dict):
            raise LaneSafeguardsError(f"{where}: an entry must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise LaneSafeguardsError(f"{where}: an entry needs a name")
        if entry.get("role") != "safeguard":
            raise LaneSafeguardsError(f"{where} ({name}): a lane entry is a safeguard")
        if not isinstance(entry.get("check"), dict):
            raise LaneSafeguardsError(f"{where} ({name}): an entry needs a `check:` mapping")
        if name in names:
            raise LaneSafeguardsError(f"{where}: duplicate entry name {name!r}")
        names.add(name)
    return entries


def requested_pull_requests(spec: Any) -> int:
    """How many pull requests a task's own checks request: its leaves of a
    type in :data:`REQUESTING_CHECK_TYPES`, wherever they nest."""
    if not isinstance(spec, list):
        return 0
    return sum(
        1
        for entry in spec
        if isinstance(entry, dict)
        for leaf in _leaves(entry.get("check"))
        if leaf.get("type") in REQUESTING_CHECK_TYPES
    )


def _load_task(task_yaml: Path) -> dict[str, Any]:
    if not task_yaml.is_file():
        raise LaneSafeguardsError(f"{task_yaml}: no such task file")
    try:
        doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LaneSafeguardsError(f"{task_yaml}: not parseable as YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise LaneSafeguardsError(f"{task_yaml}: expected a YAML mapping at the top level")
    return doc


def append_lane_safeguards(
    task_yaml: str | Path, safeguards: list[dict[str, Any]], out_dir: str | Path
) -> Path:
    """Write ``<out_dir>/<case>/task.yaml``: the task with the lane's entries
    appended, and return its path.

    The case is the task file's directory name, which is what devops-bench
    records as ``folder`` and the scorer joins on, so the copy keeps it.
    Raises :class:`LaneSafeguardsError` when a lane entry's name collides
    with one the task declares.
    """
    source = Path(task_yaml)
    doc = _load_task(source)
    spec = doc.get(SPEC_KEY)
    existing = list(spec) if isinstance(spec, list) else []
    taken = {str(e.get("name")) for e in existing if isinstance(e, dict) and e.get("name")}
    requested = requested_pull_requests(existing)
    appended = []
    for entry in safeguards:
        if entry["name"] in taken:
            raise LaneSafeguardsError(
                f"{source}: declares an entry named {entry['name']!r}, which is a lane "
                "safeguard's name; devops-bench would drop the lane's copy as a duplicate, "
                "so rename the case's entry"
            )
        clone = copy.deepcopy(entry)
        for leaf in _leaves(clone.get("check")):
            if leaf.get("type") == WRITES_CHECK_TYPE and requested:
                leaf[REQUESTED_FIELD] = requested
        appended.append(clone)
    doc[SPEC_KEY] = existing + appended
    target_dir = Path(out_dir) / source.parent.name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / TASK_FILE
    target.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    """Materialise every task given with the lane's safeguards appended.

    Prints ``<case> <path> <requested>`` per task -- the copy's path and how
    many pull requests the case's own checks request, which the script turns
    into ``BENCH_REQUESTING_CASES`` for the safeguard's attribution; exits
    non-zero, naming the file and the fault, when the lane file or a task
    refuses the append.
    """
    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("--safeguards", required=True, help="the lane safeguards YAML file")
    parser.add_argument("--out-dir", required=True, help="where <case>/task.yaml copies go")
    parser.add_argument("tasks", nargs="+", help="task.yaml paths to copy")
    args = parser.parse_args(argv)
    try:
        safeguards = load_lane_safeguards(args.safeguards)
        for task in args.tasks:
            written = append_lane_safeguards(task, safeguards, args.out_dir)
            requested = requested_pull_requests(_load_task(Path(task)).get(SPEC_KEY))
            print(f"{written.parent.name} {written} {requested}")
    except LaneSafeguardsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
