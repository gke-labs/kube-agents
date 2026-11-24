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

"""The cluster hold a case puts on the install, for ``hack/ci-eval-pr.sh`` and by hand.

A case whose precondition is an install that holds a cluster declares
``scope_hold: {agent_cluster: true, reason: <text>}`` (the install's own host
cluster, so the case needs no seeded fleet) or ``scope_hold: {all: true,
reason: <text>}`` (every cluster); both flags may be set. The runner runs such a
case alone, after every other unit, because a hold refuses every Kubernetes API
request the agent makes to the held cluster: it sets the PlatformAgent's
``spec.hold`` for the case's repetitions, then puts the previous hold back.

A hold restarts no pod and prunes no profile, so there is no reconcile to wait
for. ``apply`` patches the CR, reads it back, waits until the operator has
written the hold into the ``<agent>-hold`` ConfigMap (SETTLE_SECONDS), then
waits PROPAGATION_SECONDS more: the proxy and the broker read the ConfigMap as a
mounted file, which the kubelet refreshes on its own clock (its sync period plus
its ConfigMap cache, about a minute each by default), and nothing the runner can
read tells it when a given pod's copy changed. A probe through the proxy would
tell, but it would need the shell sandbox's kubeconfig, which only the sandbox
user owns.

An install whose PlatformAgent API has no ``spec.hold`` (current main
before the field lands) is told apart before anything is patched, from the CRD's
schema: ``apply`` patches nothing and exits EXIT_UNSUPPORTED, and the runner runs
the case on the install as it is. That run is graded, not recorded as
infrastructure: the case asks for behaviour the install does not have, so it
goes red for that reason (the agent reads the cluster and never reports a hold),
which is the red step of the eval-driven loop. A field the schema declares but a
hold the operator never writes into the ConfigMap is a failed precondition
(exit 1), recorded as infrastructure.

The hold the patch replaced is saved on the CR itself, in ANNOTATION, so the
restore needs nothing from the run that patched it: the EXIT trap and the next
lease's preflight both restore from the CR alone, and a patch over a leftover
starts from the saved hold rather than from the leftover. ``spec.hold`` is its
own top-level field, so setting and clearing it touches nothing else on the CR.

Subcommands, each exiting 1 with the reason on stderr:

``cases <task.yaml>...``
    ``<case> <hold as one line of JSON>`` for each task that declares the key.
``apply (--task <task.yaml> | --hold <json>) --context <ctx> --namespace <ns> --name <cr>``
    Apply the hold and wait until it is in effect; exit EXIT_UNSUPPORTED, having
    patched nothing, when the install's API has no ``spec.hold``.
``restore --context <ctx> --namespace <ns> --name <cr> [--wait]``
    Put the saved hold back, or do nothing when none is saved; ``--wait`` also
    waits PROPAGATION_SECONDS after a restore, for the lifted hold to reach the
    pods.

By hand, for the eval-driven loop (``.agents/rules/eval_driven_development.md``)::

    cd bench
    python -m kube_agents_bench.scope_hold apply --task tasks/<id>/task.yaml \\
        --context "$AGENT_CLUSTER_CONTEXT" --namespace kubeagents-system --name platform-agent
    uv run devops-bench ./tasks/<id> --agent-type kubeagents
    python -m kube_agents_bench.scope_hold restore \\
        --context "$AGENT_CLUSTER_CONTEXT" --namespace kubeagents-system --name platform-agent
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

KEY = "scope_hold"
ALL_KEY = "all"
AGENT_CLUSTER_KEY = "agent_cluster"
REASON_KEY = "reason"
FLAG_KEYS = (ALL_KEY, AGENT_CLUSTER_KEY)
# Long enough for a sentence, short enough to sit in one log line and one
# refusal message.
MAX_REASON_CHARS = 256
# The CR's spec.hold, and its keys.
SPEC_HOLD = "hold"
HOLD_ALL = "all"
HOLD_CLUSTERS = "clusters"
HOLD_REASON = "reason"
CLUSTER_KEYS = ("projectId", "location", "clusterName")
# A GKE kubeconfig context, gke_<project>_<location>_<cluster>: how the agent's
# own cluster is named without the seeded fleet.
GKE_CONTEXT = re.compile(r"\Agke_([a-z0-9-]+)_([a-z0-9-]+)_([a-z0-9-]+)\Z")
# The hold the patch replaced: {"hold": <the CR's spec.hold, or null>}, as JSON.
ANNOTATION = "bench.kubeagents.x-k8s.io/scope-hold-before"
SAVED_HOLD = "hold"
CRD_NAME = "platformagents.kubeagents.x-k8s.io"
# The ConfigMap the operator renders the hold into, beside the CR's name, and
# its one key.
CONFIGMAP_SUFFIX = "-hold"
CONFIGMAP_KEY = "hold.json"
# How long the operator may take to write the hold into the ConfigMap, and how
# often it is read; then how long the kubelet may take to refresh the mounted
# copy in the proxy and broker pods (see the module docstring).
SETTLE_SECONDS = 300
POLL_SECONDS = 10
PROPAGATION_SECONDS = 120
# Every kubectl call's own bound, and the process's, a little longer.
REQUEST_TIMEOUT = "30s"
PROCESS_TIMEOUT_SECONDS = 60
# `apply` on an install whose PlatformAgent API has no spec.hold: nothing
# was patched, and the case runs on the install as it is (the module docstring
# says why that is graded).
EXIT_UNSUPPORTED = 3


class ScopeHoldError(Exception):
    pass


Kubectl = Callable[..., subprocess.CompletedProcess]


def validate(value: Any, where: str = KEY) -> dict[str, Any]:
    """The declared hold, checked: at least one flag true, and a reason."""
    ok = (
        isinstance(value, dict)
        and set(value) <= {*FLAG_KEYS, REASON_KEY}
        and all(isinstance(value[k], bool) for k in FLAG_KEYS if k in value)
        and any(value.get(k) is True for k in FLAG_KEYS)
        and isinstance(value.get(REASON_KEY), str)
        and value[REASON_KEY].strip() != ""
        and len(value[REASON_KEY]) <= MAX_REASON_CHARS
        and "\n" not in value[REASON_KEY]
    )
    if not ok:
        raise ScopeHoldError(
            f"{where}: '{KEY}:' must be {{{AGENT_CLUSTER_KEY}: true, {ALL_KEY}: true, {REASON_KEY}: <text>}} "
            f"with at least one of the two flags a bare true and a one-line reason of at most "
            f"{MAX_REASON_CHARS} characters, got {value!r}"
        )
    return dict(value)


def declared_hold(task_yaml: Path) -> dict[str, Any] | None:
    """The hold a task declares; None when it declares none."""
    # Imported here: `apply` and `restore` run under a bare python3 in the EXIT
    # trap and the lease preflight, where PyYAML may not be installed.
    import yaml

    try:
        doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScopeHoldError(f"{task_yaml}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScopeHoldError(f"{task_yaml}: expected a YAML mapping at the top level")
    if KEY not in doc:
        return None
    return validate(doc[KEY], str(task_yaml))


def agent_cluster(context: str) -> dict[str, str]:
    """The install's host cluster, as spec.hold.clusters takes it."""
    match = GKE_CONTEXT.match(context)
    if not match:
        raise ScopeHoldError(f"context {context!r} is not gke_<project>_<location>_<cluster>")
    return dict(zip(CLUSTER_KEYS, match.groups(), strict=True))


def resolved_hold(declared: dict[str, Any], context: str) -> dict[str, Any]:
    """The CR's spec.hold for a declared hold."""
    hold: dict[str, Any] = {}
    if declared.get(ALL_KEY):
        hold[HOLD_ALL] = True
    if declared.get(AGENT_CLUSTER_KEY):
        hold[HOLD_CLUSTERS] = [agent_cluster(context)]
    hold[HOLD_REASON] = declared[REASON_KEY]
    return hold


def crd_declares_hold(crd: dict[str, Any]) -> bool:
    """Whether a served version of the PlatformAgent CRD has spec.hold."""
    for version in (crd.get("spec") or {}).get("versions") or []:
        if not version.get("served"):
            continue
        props = ((version.get("schema") or {}).get("openAPIV3Schema") or {}).get("properties") or {}
        spec = (props.get("spec") or {}).get("properties") or {}
        if SPEC_HOLD in spec:
            return True
    return False


def _saved(cr: dict[str, Any]) -> dict[str, Any] | None:
    annotations = (cr.get("metadata") or {}).get("annotations") or {}
    if ANNOTATION not in annotations:
        return None
    try:
        saved = json.loads(annotations[ANNOTATION])
    except ValueError as exc:
        raise ScopeHoldError(f"annotation {ANNOTATION} is not JSON: {exc}") from exc
    if not isinstance(saved, dict) or SAVED_HOLD not in saved or not (
        saved[SAVED_HOLD] is None or isinstance(saved[SAVED_HOLD], dict)
    ):
        raise ScopeHoldError(f"annotation {ANNOTATION} holds {saved!r}, not a saved hold; delete it by hand")
    return saved


def _replacement(current: Any, new: dict[str, Any]) -> dict[str, Any]:
    """A merge-patch value that replaces `current` with `new`: a key `new` lacks is nulled."""
    value: dict[str, Any] = copy.deepcopy(new)
    for key in current if isinstance(current, dict) else {}:
        value.setdefault(key, None)
    return value


def patch_body(cr: dict[str, Any], hold: dict[str, Any]) -> dict[str, Any]:
    """The merge patch that sets `hold`, saving the hold the CR had before any."""
    saved = _saved(cr)
    current = (cr.get("spec") or {}).get(SPEC_HOLD)
    if saved is None:
        saved = {SAVED_HOLD: current if isinstance(current, dict) else None}
    return {
        "metadata": {"annotations": {ANNOTATION: json.dumps(saved, sort_keys=True)}},
        "spec": {SPEC_HOLD: _replacement(current, hold)},
    }


def restore_body(cr: dict[str, Any]) -> dict[str, Any] | None:
    """The merge patch back to the saved hold; None when none is saved."""
    saved = _saved(cr)
    if saved is None:
        return None
    body: dict[str, Any] = {"metadata": {"annotations": {ANNOTATION: None}}}
    current = (cr.get("spec") or {}).get(SPEC_HOLD)
    if saved[SAVED_HOLD] is None:
        if current is not None:
            body["spec"] = {SPEC_HOLD: None}
    else:
        body["spec"] = {SPEC_HOLD: _replacement(current, saved[SAVED_HOLD])}
    return body


def hold_in_effect_on_cr(cr: dict[str, Any], hold: dict[str, Any]) -> bool:
    """Whether the CR keeps every key of `hold` as written (the server may default others)."""
    current = (cr.get("spec") or {}).get(SPEC_HOLD)
    return isinstance(current, dict) and all(current.get(k) == v for k, v in hold.items())


def held_in_configmap(configmap: dict[str, Any], hold: dict[str, Any]) -> bool:
    """Whether the operator's ConfigMap carries `hold` in its hold.json: `all: true`
    when the hold is on all, and each held cluster by its triple."""
    text = (configmap.get("data") or {}).get(CONFIGMAP_KEY)
    if not isinstance(text, str):
        return False
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    if hold.get(HOLD_ALL) and doc.get(HOLD_ALL) is not True:
        return False
    rendered = doc.get(HOLD_CLUSTERS) or []
    found = [
        {k: c.get(k) for k in CLUSTER_KEYS} for c in rendered if isinstance(c, dict)
    ] if isinstance(rendered, list) else []
    return all(cluster in found for cluster in hold.get(HOLD_CLUSTERS) or [])


def kubectl_runner(context: str, namespace: str) -> Kubectl:
    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        cmd = ["kubectl", "--context", context, "-n", namespace, f"--request-timeout={REQUEST_TIMEOUT}", *args]
        try:
            return subprocess.run(
                cmd, input=stdin, capture_output=True, text=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(cmd, 1, "", str(exc))

    return run


def _get_json(kubectl: Kubectl, *args: str) -> dict[str, Any]:
    result = kubectl("get", *args, "-o", "json")
    if result.returncode != 0:
        raise ScopeHoldError(f"kubectl get {' '.join(args)}: {result.stderr.strip() or 'failed'}")
    try:
        doc = json.loads(result.stdout)
    except ValueError as exc:
        raise ScopeHoldError(f"kubectl get {' '.join(args)}: not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScopeHoldError(f"kubectl get {' '.join(args)}: not a JSON object")
    return doc


def _merge_patch(kubectl: Kubectl, name: str, body: dict[str, Any]) -> None:
    result = kubectl("patch", "platformagent", name, "--type", "merge", "-p", json.dumps(body))
    if result.returncode != 0:
        raise ScopeHoldError(f"kubectl patch platformagent/{name}: {result.stderr.strip() or 'failed'}")


def apply(
    kubectl: Kubectl,
    name: str,
    hold: dict[str, Any],
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Set `hold` and wait until it is in effect; EXIT_UNSUPPORTED when the API has no such field."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    if not crd_declares_hold(_get_json(kubectl, "crd", CRD_NAME)):
        log(
            f"Scope hold: {CRD_NAME} declares no spec.hold, so platformagent/{name} cannot be held; "
            "nothing was patched and the case runs on the install as it is"
        )
        return EXIT_UNSUPPORTED
    _merge_patch(kubectl, name, patch_body(_get_json(kubectl, "platformagent", name), hold))
    if not hold_in_effect_on_cr(_get_json(kubectl, "platformagent", name), hold):
        raise ScopeHoldError(f"platformagent/{name} does not keep the hold it was patched with")
    configmap = name + CONFIGMAP_SUFFIX
    deadline = clock() + SETTLE_SECONDS
    while True:
        try:
            if held_in_configmap(_get_json(kubectl, "configmap", configmap), hold):
                break
        except ScopeHoldError:
            pass
        if clock() >= deadline:
            raise ScopeHoldError(f"configmap/{configmap} does not carry the hold after {SETTLE_SECONDS}s")
        sleep(POLL_SECONDS)
    log(f"Scope hold: configmap/{configmap} carries the hold; waiting {PROPAGATION_SECONDS}s for the pods' copies")
    sleep(PROPAGATION_SECONDS)
    log(f"Scope hold: platformagent/{name} holds {json.dumps(hold, sort_keys=True)}")
    return 0


def restore(
    kubectl: Kubectl,
    name: str,
    wait: bool = False,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
) -> int:
    sleep = sleep or time.sleep
    body = restore_body(_get_json(kubectl, "platformagent", name))
    if body is None:
        return 0
    _merge_patch(kubectl, name, body)
    log(f"Scope hold: platformagent/{name} restored: {json.dumps(body, sort_keys=True)}")
    if wait:
        sleep(PROPAGATION_SECONDS)
    return 0


def main(argv: list[str] | None = None, kubectl: Kubectl | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kube_agents_bench.scope_hold")
    sub = parser.add_subparsers(dest="command", required=True)
    cases = sub.add_parser("cases")
    cases.add_argument("tasks", nargs="*")
    for command in ("apply", "restore"):
        p = sub.add_parser(command)
        p.add_argument("--context", required=True)
        p.add_argument("--namespace", required=True)
        p.add_argument("--name", required=True)
        if command == "apply":
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument("--task", type=Path)
            source.add_argument("--hold")
        else:
            p.add_argument("--wait", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "cases":
            for task in args.tasks:
                hold = declared_hold(Path(task))
                if hold is not None:
                    print(Path(task).parent.name, json.dumps(hold, sort_keys=True, separators=(",", ":")))
            return 0
        run = kubectl or kubectl_runner(args.context, args.namespace)
        if args.command == "restore":
            return restore(run, args.name, wait=args.wait)
        if args.task is not None:
            declared = declared_hold(args.task)
            if declared is None:
                raise ScopeHoldError(f"{args.task}: declares no '{KEY}:'")
        else:
            try:
                declared = validate(json.loads(args.hold), "--hold")
            except ValueError as exc:
                raise ScopeHoldError(f"--hold is not JSON: {exc}") from exc
        return apply(run, args.name, resolved_hold(declared, args.context))
    except ScopeHoldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
