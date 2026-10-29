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

"""The seeded clusters a case excludes from the install's scope, for ``hack/ci-eval-pr.sh``.

A case whose precondition is an install that leaves some seeded clusters out of
its scope declares ``scope_exclude: {slots: [b]}``. The runner runs such a case
alone, after every other unit: it adds each slot's cluster to the PlatformAgent's
``spec.scope.exclude.clusters`` for the case's repetitions, then puts the scope
back. Slots, not cluster names, because a case runs unchanged on every pool
project; the cluster behind a slot comes from the ``.fleet-context`` file
``hack/fleet-kubeconfigs.sh`` writes, the one place a seeded cluster is resolved,
read through :mod:`kube_agents_bench.fleet`.

The scope the patch replaced is saved on the CR itself, in ANNOTATION, so the
restore needs nothing from the run that patched it: the EXIT trap and the next
lease's preflight both restore from the CR alone, and a patch over a leftover
starts from the saved scope rather than from the leftover.

Subcommands, each printing to stdout and exiting 1 with the reason on stderr:

``cases <task.yaml>...``
    ``<case> <slot>...`` for each task that declares the key, in the order given.
``patch --fleet-dir <dir> --slots <slot>...`` (the CR as JSON on stdin)
    The JSON patch that excludes the slots' clusters and saves the scope.
``restore [--first-step-only]`` (the CR as JSON on stdin)
    The JSON patch for the next step back to the saved scope, or nothing when
    none is saved. Two steps, with a reconcile between them (restore_ops says
    why); with the flag, for a caller that cannot run a reconcile, only the
    first.
``clusters`` (a CR, or the operator's rendered scope file, as JSON on stdin)
    The excluded clusters, sorted, as one line of JSON, so the runner can tell
    when the pod reads the scope the CR declares.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

KEY = "scope_exclude"
SLOTS_KEY = "slots"
# A catalogue slot (bench/tf/fleet/fixtures.json, `cluster_slots`); the lint
# checks the slot exists, this checks only what the runner splices into a lookup.
SLOT = re.compile(r"\A[a-z]\Z")
# The scope the patch replaced, as JSON (`null` for a CR with no scope block).
ANNOTATION = "bench.kubeagents.x-k8s.io/scope-before-exclude"
# The first restore step for a CR that had no scope block: a block that excludes
# nothing, which the reconcile reads as a declaration (restore_ops says why).
CLEARED_SCOPE: dict[str, Any] = {"exclude": {"clusters": []}}
# The JSON pointers the patches write.
SCOPE_PATH = "/spec/scope"
ANNOTATIONS_PATH = "/metadata/annotations"


class ScopeExcludeError(Exception):
    pass


def declared_slots(task_yaml: Path) -> list[str]:
    """The slots a task excludes; empty when it declares none."""
    # Imported here: `patch`, `restore` and `clusters` run under a bare python3 in
    # the EXIT trap and the lease preflight, where PyYAML may not be installed.
    import yaml

    try:
        doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScopeExcludeError(f"{task_yaml}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScopeExcludeError(f"{task_yaml}: expected a YAML mapping at the top level")
    if KEY not in doc:
        return []
    value = doc[KEY]
    slots = value.get(SLOTS_KEY) if isinstance(value, dict) and set(value) == {SLOTS_KEY} else None
    if (
        not isinstance(slots, list)
        or not slots
        or not all(isinstance(s, str) and SLOT.match(s) for s in slots)
        or len(set(slots)) != len(slots)
    ):
        raise ScopeExcludeError(
            f"{task_yaml}: '{KEY}:' must be {{{SLOTS_KEY}: [<slot>, ...]}} with distinct slot letters, got {value!r}"
        )
    return slots


def excluded_clusters(fleet_dir: Path, slots: list[str]) -> list[dict[str, str]]:
    """Each slot's cluster as the triple `spec.scope.exclude.clusters` takes."""
    # Imported here: scripts/validate_bench_cases.py loads this file by path for
    # declared_slots, with no kube_agents_bench package on its path.
    from kube_agents_bench import fleet

    project = fleet.provisioned_project(fleet_dir)
    if not project:
        raise ScopeExcludeError(f"{fleet_dir}: the runner recorded no project for the seeded fleet")
    try:
        recorded = fleet.recorded_clusters(fleet_dir)
    except fleet.FleetRoleUnresolved as exc:
        raise ScopeExcludeError(str(exc)) from exc
    clusters = []
    for slot in slots:
        name, location = recorded.get(slot, ("", ""))
        if not name or not location:
            raise ScopeExcludeError(f"{fleet_dir}: slot '{slot}' has no resolved cluster and location")
        clusters.append({"projectId": project, "location": location, "clusterName": name})
    return clusters


def _annotation_path() -> str:
    # RFC 6901: `~` and `/` in a key are escaped as `~0` and `~1`.
    return ANNOTATIONS_PATH + "/" + ANNOTATION.replace("~", "~0").replace("/", "~1")


def _saved_scope(cr: dict[str, Any]) -> tuple[bool, Any]:
    annotations = (cr.get("metadata") or {}).get("annotations") or {}
    if ANNOTATION not in annotations:
        return False, None
    try:
        saved = json.loads(annotations[ANNOTATION])
    except ValueError as exc:
        raise ScopeExcludeError(f"annotation {ANNOTATION} is not JSON: {exc}") from exc
    # Only the runner writes it, as an object or null; anything else would make a
    # patch the API server refuses, on every restore, so name it instead.
    if saved is not None and not isinstance(saved, dict):
        raise ScopeExcludeError(
            f"annotation {ANNOTATION} holds {saved!r}, not a scope object or null; delete it by hand"
        )
    return True, saved


def patch_ops(cr: dict[str, Any], clusters: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Exclude `clusters` on top of the scope the CR had before any exclusion."""
    saved, base = _saved_scope(cr)
    if not saved:
        base = (cr.get("spec") or {}).get("scope")
    scope = copy.deepcopy(base) if isinstance(base, dict) else {}
    exclude = scope.get("exclude") if isinstance(scope.get("exclude"), dict) else {}
    merged = list(exclude.get("clusters") or [])
    merged += [c for c in clusters if c not in merged]
    scope["exclude"] = {**exclude, "clusters": merged}
    ops: list[dict[str, Any]] = []
    if not (cr.get("metadata") or {}).get("annotations"):
        ops.append({"op": "add", "path": ANNOTATIONS_PATH, "value": {}})
    ops.append({"op": "add", "path": _annotation_path(), "value": json.dumps(base)})
    ops.append({"op": "add", "path": SCOPE_PATH, "value": scope})
    return ops


def restore_ops(cr: dict[str, Any], first_step_only: bool = False) -> list[dict[str, Any]]:
    """The next step back to the saved scope; empty when none is saved.

    Always two steps, with a reconcile between them, and the annotation goes only
    in the second: it is the one marker the next lease repairs from, so it must
    outlast a settle that fails after the first. When the CR had a scope block,
    the first puts it back, and the CR's exclusions matching the saved ones mark
    it done (exclusions, not the whole block, which the API server defaults).
    When it had none, the first empties the exclusions rather than removing the
    block, because the Cluster Agent reconcile reads an absent block as "keep the
    last declaration's exclusions"; the second then removes the block too. A
    caller that cannot run the reconcile passes `first_step_only` and never gets
    the second step.
    """
    saved, base = _saved_scope(cr)
    if not saved:
        return []
    remove_annotation = {"op": "remove", "path": _annotation_path()}
    scope = (cr.get("spec") or {}).get("scope")
    if base is not None:
        if scope is None or scope_clusters({"spec": {"scope": scope}}) != scope_clusters(base):
            return [{"op": "add", "path": SCOPE_PATH, "value": base}]
        return [] if first_step_only else [remove_annotation]
    if scope is None:
        return [remove_annotation]
    if scope_clusters({"spec": {"scope": scope}}):
        return [{"op": "add", "path": SCOPE_PATH, "value": copy.deepcopy(CLEARED_SCOPE)}]
    if first_step_only:
        return []
    return [{"op": "remove", "path": SCOPE_PATH}, remove_annotation]


def scope_clusters(doc: dict[str, Any]) -> list[dict[str, str]]:
    """The excluded clusters of a CR or of the rendered scope file, sorted."""
    scope = (doc.get("spec") or {}).get("scope") if "spec" in doc else doc
    exclude = (scope or {}).get("exclude") or {}
    return sorted(exclude.get("clusters") or [], key=lambda c: json.dumps(c, sort_keys=True))


def _stdin_json() -> dict[str, Any]:
    try:
        doc = json.load(sys.stdin)
    except ValueError as exc:
        raise ScopeExcludeError(f"stdin is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScopeExcludeError("stdin is not a JSON object")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kube_agents_bench.scope_exclude")
    sub = parser.add_subparsers(dest="command", required=True)
    cases = sub.add_parser("cases")
    cases.add_argument("tasks", nargs="*")
    patch = sub.add_parser("patch")
    patch.add_argument("--fleet-dir", required=True, type=Path)
    patch.add_argument("--slots", required=True, nargs="+")
    restore = sub.add_parser("restore")
    restore.add_argument("--first-step-only", action="store_true")
    sub.add_parser("clusters")
    args = parser.parse_args(argv)
    try:
        if args.command == "cases":
            for task in args.tasks:
                slots = declared_slots(Path(task))
                if slots:
                    print(" ".join([Path(task).parent.name, *slots]))
        elif args.command == "patch":
            print(json.dumps(patch_ops(_stdin_json(), excluded_clusters(args.fleet_dir, args.slots))))
        elif args.command == "restore":
            ops = restore_ops(_stdin_json(), first_step_only=args.first_step_only)
            if ops:
                print(json.dumps(ops))
        else:
            print(json.dumps(scope_clusters(_stdin_json()), sort_keys=True))
    except ScopeExcludeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
