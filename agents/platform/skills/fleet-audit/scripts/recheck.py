#!/usr/bin/env python3
"""recheck.py - re-derive one fleet-audit finding against its own target, now.

The fleet audit re-checks a merged fix only on its next scheduled run, at the
stream's cadence, whenever that falls after the merge. The post-merge check
(the post-merge verification design, §3.5 "Re-check the one finding") needs the
answer on demand, for
one finding: run that finding's check against that finding's target and
nothing else, and say whether it reproduces.

This reads one namespace with one `kubectl get`, runs the same check function
`collect.py` runs on the same parsed objects, and answers:

- `reproduces`: the check fired. The evidence is the excerpt the audit itself
  renders for that check, so the re-check and the daily ledger say the same
  words about the same object.
- `clean`: the check ran against the target and did not fire.
- `unchecked`: nothing was judged. The read failed its gate, or the target is
  not in the audit's scope any more (deleted, scaled to zero, opted out, in a
  system namespace). A caller reports this as "not re-checked", never as fixed.

A check that judges the whole cluster is narrowed to the target, so an
unrelated object elsewhere cannot fail a correct fix. For `no-pdb` those are
the two checks a new budget can trip: `pdb-overlapping` for the target workload
only (a second budget over its pods makes the eviction API refuse them), and
`blocking-pdb` for the budgets that select the target's pods, or only the one
`--budget` names (a budget that permits no eviction blocks every drain). Either
would trade a missing-PDB finding for a drain-blocking one. The arithmetic
still counts every workload and budget in the namespace, as the disruption
controller does.

`derive_finding_state` (audit_report.py) turns the answer into the ledger's
state: with a merged pull request, `reproduces` is `pr-merged-persists` and
`clean` is `resolved-merged`. `unchecked` has no state, because no state is
true of a finding nobody looked at.

Nothing calls this on a schedule yet. The post-merge driver will import
`recheck()` and pass a `run` that sends the `kubectl` into the shell sandbox;
the derivation stays in the caller's process because it is `collect.py`'s
code, which cannot travel on a `python3 -I -` stdin the way `stall_report.py`
does (it reads its own `__file__` and imports its siblings). From a shell the
CLI reads with the local `kubectl`.

Only `no-pdb` is registered. A re-check is the kinds it reads in the target
namespace and a tuple of small steps, each of which ends the re-check with a
verdict or hands on: find the target as the audit's scope leaves it, run one
collector check by id on it, narrow a cluster check to the objects that select
it. Another check id joins `RECHECKS` as a new tuple of those steps, plus any
new step it needs.

Exit codes: 0 clean, 1 reproduces, 2 unchecked (a failed read, a crash inside
the re-check, or a usage error). The JSON on stdout carries the same answer.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report
import collect

OUTCOME_CLEAN = "clean"
OUTCOME_REPRODUCES = "reproduces"
OUTCOME_UNCHECKED = "unchecked"
EXIT_BY_OUTCOME = {OUTCOME_CLEAN: 0, OUTCOME_REPRODUCES: 1, OUTCOME_UNCHECKED: 2}

#: The one read a re-check makes, in the target namespace only. Plain
#: `kubectl get`, the read verb the credential proxy admits for every
#: collector's one-shot reads (`collect.run_and_gate`); one namespace is far
#: under the output cap that made the fleet-wide dump page.
READ_TIMEOUT_S = collect.DEFAULT_TIMEOUT_S


class Target(NamedTuple):
    """What a finding names: the four fields its id is derived from, plus the
    budget a `no-pdb` fix wrote, when the caller knows it."""

    check: str
    cluster: str
    namespace: str
    object: str
    budget: str = ""


class Verdict(NamedTuple):
    outcome: str
    evidence: list[dict]
    reason: str = ""


class Subject:
    """What one re-check has read and narrowed so far, passed from step to step.

    `workload` is the target once `in_scope_workload` has found it; `selected`
    is what a selecting step left for a narrowed cluster check: the value that
    replaces one context key (`"pdbs"`, `"workloads"`) for that check alone;
    `notes` is copied onto the answer.
    """

    def __init__(self, target: Target, dump: dict) -> None:
        self.target = target
        self.dump = dump
        self.workloads = collect.normalize_workloads(dump)
        self.context = collect.build_context(dump, self.workloads)
        self.workload: dict | None = None
        self.selected: dict = {}
        self.notes: dict = {}


#: One step of a re-check: a `Verdict` ends it (`reproduces` or `unchecked`),
#: None hands on to the next step. A re-check whose every step hands on is
#: clean.
Step = Callable[[Subject], "Verdict | None"]


class Recheck(NamedTuple):
    #: kubectl kinds read in the target namespace.
    kinds: str
    steps: tuple[Step, ...]


def _spec(slug: str) -> collect.CheckSpec:
    """The collector's own check, by id, so a re-check runs the function and
    renders the excerpt the audit does. Raises at import for a slug the
    collector does not define, so a misspelt registration fails the tests."""
    for table in collect.CHECK_TABLES.values():
        for spec in table:
            if spec.slug == slug:
                return spec
    raise KeyError(f"collect.py defines no check {slug!r}")


def _evidence(check: str, target: Target, obj: str, excerpt: str) -> dict:
    finding = {"check": check, "cluster": target.cluster, "namespace": target.namespace, "object": obj}
    return {**finding, "finding_id": audit_report.published_id(finding), "excerpt": excerpt}


def _name(obj: dict) -> str:
    return str((obj.get("metadata") or {}).get("name") or "")


def _selects(obj: dict, pod_labels: dict) -> bool:
    # A null selector selects nothing in policy/v1, as `check_no_pdb` reads it.
    selector = (obj.get("spec") or {}).get("selector")
    return selector is not None and collect.selector_matches(selector, pod_labels)


# --------------------------------------------------------------------------- #
# Steps. Each is small and keyed by what it reads, so a later check id is a
# new tuple of these rather than a new function: a workload-scoped finding is
# `in_scope_workload` then `workload_check(<its id>)`.
# --------------------------------------------------------------------------- #


def in_scope_workload(subject: Subject) -> Verdict | None:
    """The target workload, as the audit's scope rules leave it, or unchecked.

    A workload the audit would not read (deleted, scaled to zero, opted out, in
    a system namespace) has no finding to re-derive, and "clean" would credit a
    fix for its absence.
    """
    target = subject.target
    kind, _, name = target.object.partition("/")
    subject.workload = next(
        (w for w in subject.workloads if w["kind"] == kind and w["name"] == name and w["ns"] == target.namespace),
        None,
    )
    if subject.workload is not None:
        return None
    present = any(i.get("kind") == kind and _name(i) == name for i in subject.dump.get("items") or [])
    why = (
        "is present but outside the audit's scope (scaled to zero, opted out, owned by another "
        "controller, or in a system namespace)"
        if present
        else "was not found"
    )
    return Verdict(OUTCOME_UNCHECKED, [], f"{target.object} in {target.namespace} {why}, so {target.check} was not re-derived")


def workload_check(slug: str) -> Step:
    """The collector's workload-kind check `slug`, run on the target alone."""
    spec = _spec(slug)
    if spec.kind != "workload":
        raise ValueError(f"{slug!r} is a {spec.kind} check; narrow it with narrowed_cluster_check")

    def step(subject: Subject) -> Verdict | None:
        hit = spec.run(subject.workload, subject.context)
        if hit is None:
            return None
        return Verdict(OUTCOME_REPRODUCES, [_evidence(slug, subject.target, hit["object"], hit["excerpt"])])

    return step


def select_budgets(subject: Subject) -> Verdict | None:
    """Narrow to the budgets over the target's pods, or to the one the fix wrote.

    With `--budget`, the fix's own budget must exist and select the pods: one
    that does not, beside another budget that does (or a workload no longer
    required to have one), leaves `no-pdb` clean for a reason the fix did not
    supply, so the answer is unchecked rather than credited.
    """
    target, namespace = subject.target, subject.target.namespace
    pdbs = subject.context["pdbs"].get(namespace, [])
    covering = [p for p in pdbs if _selects(p, subject.workload["pod_labels"])]
    if target.budget:
        named = [p for p in covering if _name(p) == target.budget]
        if not named:
            why = "does not select its pods" if any(_name(p) == target.budget for p in pdbs) else "does not exist"
            others = ", ".join(sorted(_name(p) for p in covering))
            covered = f"covered by {others}" if others else "not required to have a budget now"
            subject.notes["budgets"] = []
            return Verdict(
                OUTCOME_UNCHECKED,
                [],
                (
                    f"PodDisruptionBudget/{target.budget} {why}; {target.object} is {covered}, "
                    "so the fix's own budget could not be re-checked"
                ),
            )
        covering = named
    subject.selected["pdbs"] = {namespace: covering}
    subject.notes["budgets"] = sorted(_name(p) for p in covering)
    return None


def select_target_workload(subject: Subject) -> Verdict | None:
    """Narrow a workload-anchored cluster check to the target alone."""
    subject.selected["workloads"] = [subject.workload]
    return None


def narrowed_cluster_check(slug: str, context_key: str) -> Step:
    """The collector's cluster-kind check `slug`, with `context_key` replaced by
    what a selecting step left there and everything else as read. The rest of
    the namespace stays in the context on purpose: `blocking-pdb` counts every
    pod a budget selects, and `pdb-overlapping` every budget over the target,
    not only the target's or the fix's."""
    spec = _spec(slug)
    if spec.kind != "cluster":
        raise ValueError(f"{slug!r} is a {spec.kind} check; run it with workload_check")

    def step(subject: Subject) -> Verdict | None:
        narrowed = {**subject.context, context_key: subject.selected[context_key]}
        evidence = [_evidence(slug, subject.target, h["object"], h["excerpt"]) for h in spec.run(narrowed)]
        return Verdict(OUTCOME_REPRODUCES, evidence) if evidence else None

    return step


RECHECKS: dict[str, Recheck] = {
    "no-pdb": Recheck(
        # `horizontalpodautoscalers`: `blocking-pdb` compares against an
        # autoscaler's floor rather than `spec.replicas`.
        "deployments,statefulsets,daemonsets,poddisruptionbudgets,horizontalpodautoscalers",
        (
            in_scope_workload,
            workload_check("no-pdb"),
            # A second budget over the same pods makes the eviction API refuse
            # them all, so a fix that adds one beside another is not clean.
            select_target_workload,
            narrowed_cluster_check("pdb-overlapping", "workloads"),
            select_budgets,
            narrowed_cluster_check("blocking-pdb", "pdbs"),
        ),
    ),
}


def judge(spec: Recheck, target: Target, dump: dict) -> tuple[Verdict, dict]:
    """Run `spec`'s steps in order; the first verdict stands, none is clean."""
    subject = Subject(target, dump)
    for step in spec.steps:
        verdict = step(subject)
        if verdict is not None:
            return verdict, subject.notes
    return Verdict(OUTCOME_CLEAN, []), subject.notes


def read_argv(recheck: Recheck, namespace: str, kube_context: str = "") -> list[str]:
    argv = ["kubectl", "get", recheck.kinds, "-n", namespace, "-o", "json"]
    if kube_context:
        argv += ["--context", kube_context]
    return argv


def base_answer(target: Target) -> dict:
    """The fields every answer carries, whatever the outcome: the finding's
    identity, its published id, and the budget when one was named."""
    finding = {"check": target.check, "cluster": target.cluster, "namespace": target.namespace, "object": target.object}
    answer: dict = {**finding, "finding_id": audit_report.published_id(finding)}
    if target.budget:
        answer["budget"] = target.budget
    return answer


def recheck(
    target: Target,
    *,
    run: Callable | None = None,
    kubeconfig: str = "",
    kube_context: str = "",
    pr: dict | None = None,
) -> dict:
    """The answer for one finding, as the JSON the CLI prints.

    `run(argv, env=..., timeout=...)` returns a `collector_run.Run`; the
    post-merge driver passes one that runs in the shell sandbox. `pr` is the
    finding's pull request record (`state`, `mergedAt`, `labels`), when the
    caller has it; with it the answer carries the ledger state
    `derive_finding_state` gives. Without `run`, the local `kubectl` reads.
    """
    answer = base_answer(target)
    spec = RECHECKS.get(target.check)
    if spec is None:
        return {
            **answer,
            "outcome": OUTCOME_UNCHECKED,
            "evidence": [],
            "reason": f"no on-demand re-check is registered for {target.check!r} (registered: {', '.join(sorted(RECHECKS))})",
        }
    argv = read_argv(spec, target.namespace, kube_context)
    answer["command"] = (f"KUBECONFIG={kubeconfig} " if kubeconfig else "") + shlex.join(argv)
    env = {**os.environ, "KUBECONFIG": kubeconfig} if kubeconfig else dict(os.environ)
    result = (run or collect.default_run)(argv, env=env, timeout=READ_TIMEOUT_S)
    dump = _gate(result)
    if dump is None:
        stderr = " ".join((result.stderr or "").split())[: collect.ERROR_EXCERPT_CHARS] or "no stderr"
        return {
            **answer,
            "outcome": OUTCOME_UNCHECKED,
            "evidence": [],
            "reason": f"the read failed its gate (rc={result.rc}: {stderr}), so nothing was judged",
        }
    verdict, notes = judge(spec, target, dump)
    answer.update(notes)
    answer.update({"outcome": verdict.outcome, "evidence": verdict.evidence})
    if verdict.reason:
        answer["reason"] = verdict.reason
    if pr is not None and verdict.outcome != OUTCOME_UNCHECKED:
        answer["state"] = audit_report.derive_finding_state(verdict.outcome == OUTCOME_REPRODUCES, pr)
    return answer


def _gate(result) -> dict | None:
    """`collect.run_and_gate`'s gate, for a read whose kubeconfig may be the
    caller's own: a failed, empty, unparsable or item-less reply is no answer,
    never an empty namespace."""
    if result.rc != 0 or not (result.stdout or "").strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        return None
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, help=f"the finding's check id ({', '.join(sorted(RECHECKS))})")
    parser.add_argument("--cluster", required=True, help="the finding's cluster, as the manifest names it: <project>/<location>/<name>")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--object", required=True, help="the finding's object, e.g. Deployment/checkout")
    parser.add_argument("--budget", default="", help="no-pdb: the PodDisruptionBudget the fix wrote; narrows blocking-pdb to it")
    parser.add_argument("--kubeconfig", default="", help="kubeconfig for the read; default: kubectl's own")
    parser.add_argument("--context", default="", dest="kube_context", help="kubectl context for the read")
    parser.add_argument("--merged", action="store_true", help="the finding's fix is merged: add the ledger state")
    args = parser.parse_args(argv)
    target = Target(args.check, args.cluster, args.namespace, args.object, args.budget)
    pr = {"state": "MERGED"} if args.merged else None
    try:
        answer = recheck(target, kubeconfig=args.kubeconfig, kube_context=args.kube_context, pr=pr)
    except Exception as exc:  # noqa: BLE001 -- a crash is no verdict, and exit 1 would read as one
        answer = {
            **base_answer(target),
            "outcome": OUTCOME_UNCHECKED,
            "evidence": [],
            "reason": f"the re-check failed ({type(exc).__name__}: {exc}), so nothing was judged",
        }
    print(json.dumps(answer, indent=2))
    return EXIT_BY_OUTCOME[answer["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
