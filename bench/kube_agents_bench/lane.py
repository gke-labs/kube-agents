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
``hack/ci-eval-pr.sh`` beside the lane's exclusions; its shape is two keys:
``safeguards``, holding entries in the case format's own vocabulary, and
``requesting``, mapping a case the lane treats as requesting a pull request
before its own checks say so to the count it is allowed.
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
script uses to run the requesting cases in a second phase after every other
unit has finished, so a repetition that requests nothing never shares the
repository with a case that writes by design.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "LaneSafeguardsError",
    "append_lane_safeguards",
    "check_repository",
    "copy_task",
    "load_lane_requesting",
    "load_lane_safeguards",
    "main",
    "requested_pull_requests",
]

#: The top-level keys of a lane safeguards file: the entries every case
#: carries, and the cases the lane treats as requesting a pull request
#: beyond what their own checks say, each with the count it is allowed.
SAFEGUARDS_KEY = "safeguards"
REQUESTING_KEY = "requesting"
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
#: An ``owner/name`` repository in the shape ``hack/ci-deploy.sh`` accepts
#: and nothing looser: a trailing slash, a third segment, or a ``?`` or ``#``
#: that cuts the API path short passes an "is there a slash" test and fails
#: every call; ``\Z`` because ``$`` also matches before a trailing newline.
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


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


def check_repository(safeguards: list[dict[str, Any]], repo: str) -> None:
    """Refuse a repository the lane's own entries would refuse at grading.

    A ``github_writes`` leaf that pins ``owner`` errors on every repetition
    when ``BENCH_GITOPS_REPO`` sits elsewhere, and a lane that starts on such
    a repository spends a lease to grade nothing. Known before the fan-out
    from the file and the value, so it is refused here.
    """
    if not REPO_SLUG_RE.match(repo):
        raise LaneSafeguardsError(f"{repo!r} is not an owner/name repository")
    owner = repo.split("/", 1)[0]
    for entry in safeguards:
        for leaf in _leaves(entry.get("check")):
            pinned = str(leaf.get("owner") or "") if leaf.get("type") == WRITES_CHECK_TYPE else ""
            if pinned and pinned.lower() != owner.lower():
                raise LaneSafeguardsError(
                    f"{repo} is not under {pinned}, the organisation the lane entry "
                    f"{entry['name']!r} pins; every repetition would grade an errored "
                    "safeguard, so the lane does not start on it"
                )


def load_lane_requesting(path: str | Path) -> dict[str, int]:
    """The file's ``requesting:`` mapping, case id to the count of pull
    requests the lane allows it, or an empty mapping when the key is absent.

    For a case whose prompt the persona answers with a pull request before
    its checks say so: it runs in the second phase and is allowed that many,
    where its own spec would count zero. An entry is a placeholder for the
    case's own check and goes when that check lands. Or for a case that opens
    more pull requests by design than its checks grade: the count is the
    total, and the entry stays.
    """
    file = Path(path)
    if not file.is_file():
        raise LaneSafeguardsError(f"{file}: no such lane safeguards file")
    try:
        doc = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LaneSafeguardsError(f"{file}: not parseable as YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise LaneSafeguardsError(f"{file}: expected a mapping at the top level")
    listed = doc.get(REQUESTING_KEY) or {}
    if not isinstance(listed, dict):
        raise LaneSafeguardsError(f"{file}: `{REQUESTING_KEY}:` must map case ids to counts")
    out: dict[str, int] = {}
    for case, count in listed.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise LaneSafeguardsError(
                f"{file}: `{REQUESTING_KEY}:` {case!r} must map to a count of at least 1, got {count!r}"
            )
        out[str(case)] = count
    return out


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
    """Write ``<out_dir>/<case>/task.yaml`` with the lane's entries appended
    and return its path; :func:`copy_task` with no listed allowance."""
    return copy_task(task_yaml, safeguards, out_dir)[0]


def copy_task(
    task_yaml: str | Path,
    safeguards: list[dict[str, Any]],
    out_dir: str | Path,
    listed_allowance: int = 0,
) -> tuple[Path, int]:
    """Write ``<out_dir>/<case>/task.yaml``: the task with the lane's entries
    appended. Returns the copy's path and how many pull requests the case
    requests: the larger of what its own checks say and ``listed_allowance``,
    the count the lane file's ``requesting:`` gives it.

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
    requested = max(requested_pull_requests(existing), listed_allowance)
    appended = []
    for entry in safeguards:
        if entry["name"] in taken:
            raise LaneSafeguardsError(
                f"{source}: declares an entry named {entry['name']!r}, which is a lane "
                "safeguard's name; devops-bench would refuse the duplicate as a parse error "
                "on every repetition of this case, so rename the case's entry"
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
    return target, requested


def main(argv: list[str] | None = None) -> int:
    """Materialise every task given with the lane's safeguards appended.

    Prints ``<requested> <case> <path>`` per task -- how many pull requests
    the case's own checks request, which the script uses to run those cases
    in the fan-out's second phase, then the copy's path, last because it may
    hold spaces; exits
    non-zero, naming the file and the fault, when the lane file or a task
    refuses the append.
    """
    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("--safeguards", required=True, help="the lane safeguards YAML file")
    parser.add_argument("--out-dir", required=True, help="where <case>/task.yaml copies go")
    parser.add_argument(
        "--gitops-repo",
        default="",
        help="the owner/name the safeguards will read; refused when a lane entry pins another owner",
    )
    parser.add_argument("tasks", nargs="+", help="task.yaml paths to copy")
    args = parser.parse_args(argv)
    try:
        safeguards = load_lane_safeguards(args.safeguards)
        if args.gitops_repo:
            check_repository(safeguards, args.gitops_repo)
        listed = load_lane_requesting(args.safeguards)
        for task in args.tasks:
            case = Path(task).parent.name
            written, requested = copy_task(task, safeguards, args.out_dir, listed.get(case, 0))
            # The count first and the path last: the path may hold spaces
            # (a TMPDIR with one), and the consumer splits on whitespace.
            print(f"{requested} {written.parent.name} {written}")
    except LaneSafeguardsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
