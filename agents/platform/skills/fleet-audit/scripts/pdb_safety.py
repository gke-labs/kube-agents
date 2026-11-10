"""pdb_safety.py - check a disruption-budget fix against its cluster before its pull request opens.

A `no-pdb` fix adds one PodDisruptionBudget beside a workload. Whether that
fix may merge without a person looking at it depends on the cluster as it is
when the pull request opens, not as the audit read it hours earlier, so this
reads the workload's namespace once more and answers three questions:

- `replicas-ready`: the workload asks for at least two replicas and all of
  them are Ready. A budget over one pod, or over pods that are not Ready,
  permits no eviction and blocks every node drain.
- `no-overlapping-budget`: no other PodDisruptionBudget already selects the
  workload's pods. The eviction API refuses a pod two budgets select.
- `selector-matches`: the budget the fix writes selects the workload's pods,
  carries the workload's own live selector, and selects no other workload's.

All three must pass for the fix to be eligible for auto-merge. A failing or
unanswered check makes it ineligible and nothing else: the pull request still
opens, for a person to review. The answer is rendered into the body
(`render_section`) and carried in a hidden marker the auto-merge path reads.

The read is `recheck.read_target`, the on-demand re-check's own, pinned to the
finding's cluster and the audit's scope; the workload is found by
`recheck.in_scope_workload`, so the audit's scope filters (S1-S5) hold here too.

Nothing runs this unless the install's `KUBEAGENTS_AUTOMERGE` switch is not
`off` (`audit_report.open_remediation_pr`).
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect
import recheck

CHECK_ID = "no-pdb"
REPLICAS_READY = "replicas-ready"
NO_OVERLAPPING_BUDGET = "no-overlapping-budget"
SELECTOR_MATCHES = "selector-matches"
CHECK_NAMES = (REPLICAS_READY, NO_OVERLAPPING_BUDGET, SELECTOR_MATCHES)
MIN_REPLICAS = 2
SECTION_HEADING = "## Safety checks"
# Read by the auto-merge path. Written only by this harness into a body it
# renders; a body anyone else wrote proves nothing by carrying it.
ELIGIBLE_MARKER = "<!-- automerge-safety: {verdict} -->"
ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"
ELIGIBLE_MARKER_PATTERN = re.compile(r"<!-- automerge-safety: (eligible|ineligible) -->")


def _result(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def _unanswered(detail: str) -> list[dict]:
    return [_result(name, False, detail) for name in CHECK_NAMES]


def budget_from_text(text: str) -> tuple[str, dict] | None:
    """`(name, selector)` of the one PodDisruptionBudget in a fix's file, or
    None when the file holds none, several, or one without a selector."""
    import yaml

    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return None
    budgets = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    if len(budgets) != 1:
        return None
    name = str((budgets[0].get("metadata") or {}).get("name") or "")
    selector = (budgets[0].get("spec") or {}).get("selector")
    if not name or not isinstance(selector, dict):
        return None
    return name, selector


def _raw_item(dump: dict, workload: dict) -> dict:
    return next(
        (
            item
            for item in dump.get("items") or []
            if item.get("kind") == workload["kind"]
            and (item.get("metadata") or {}).get("name") == workload["name"]
            and (item.get("metadata") or {}).get("namespace", workload["ns"]) == workload["ns"]
        ),
        {},
    )


def judge(target: recheck.Target, selector: dict, dump: dict) -> list[dict]:
    """The three results for the fix whose budget is `target.budget` with
    `selector`, against one namespace's read."""
    subject = recheck.Subject(target, dump)
    missing = recheck.in_scope_workload(subject)
    if missing is not None:
        return _unanswered(missing.reason)
    workload = subject.workload
    status = _raw_item(dump, workload).get("status") or {}
    wanted = workload["spec"].get("replicas", 1) or 1
    ready = int(status.get("readyReplicas") or 0)
    results = [
        _result(
            REPLICAS_READY,
            wanted >= MIN_REPLICAS and ready >= wanted,
            f"{target.object}: replicas={wanted}, ready={ready}",
        )
    ]

    overlapping = sorted(
        recheck._name(pdb)
        for pdb in subject.context["pdbs"].get(target.namespace, [])
        if recheck._name(pdb) != target.budget and recheck._selects(pdb, workload["pod_labels"])
    )
    results.append(
        _result(
            NO_OVERLAPPING_BUDGET,
            not overlapping,
            f"already selected by PodDisruptionBudget {', '.join(overlapping)}"
            if overlapping
            else f"no other PodDisruptionBudget in {target.namespace} selects its pods",
        )
    )

    live = workload["spec"].get("selector")
    others = sorted(
        f"{template['kind']}/{template['name']}"
        for template in subject.context.get("pod_templates", {}).get(target.namespace, [])
        if (template["kind"], template["name"]) != (workload["kind"], workload["name"])
        and collect.selector_matches(selector, template["labels"])
    )
    empty = not any(selector.get(key) for key in ("matchLabels", "matchExpressions"))
    if empty:
        why = "the budget's selector is empty and would select every pod in the namespace"
    elif not collect.selector_matches(selector, workload["pod_labels"]):
        why = "the budget's selector does not select the workload's pods"
    elif selector != live:
        why = f"the budget's selector differs from the workload's live selector {json.dumps(live, sort_keys=True)}"
    elif others:
        why = f"the budget's selector also selects {', '.join(others)}'s pods"
    else:
        why = ""
    results.append(
        _result(SELECTOR_MATCHES, not why, why or "the budget's selector is the workload's own and selects only its pods")
    )
    return results


def check_fix(
    finding: dict,
    file_text: str | None,
    *,
    in_manifest: bool,
    run: Callable | None = None,
    kubeconfig: str = "",
    kube_context: str = "",
) -> dict:
    """`{finding_id, command, eligible, checks}` for one `no-pdb` finding whose
    fix file holds `file_text`.

    `in_manifest` says the audit's collector read the finding's cluster on
    this run, which is what puts it inside the install's declared scope: the
    caller has no scope flags of its own to hand `recheck`, and without that
    proof nothing is read and the fix is ineligible.
    """
    answer = {"finding_id": str(finding.get("id", "")), "command": "", "eligible": False}
    budget = budget_from_text(file_text) if file_text else None
    if budget is None:
        answer["checks"] = _unanswered("the fix's file does not hold exactly one PodDisruptionBudget with a selector")
        return answer
    name, selector = budget
    target = recheck.Target(
        CHECK_ID,
        str(finding.get("cluster") or ""),
        str(finding.get("namespace") or ""),
        str(finding.get("object") or ""),
        name,
    )
    if not in_manifest:
        answer["checks"] = _unanswered(
            "this run's collector manifest does not show the finding's cluster as read, so it was not read again"
        )
        return answer
    # The collector's read of this cluster already applied the install's scope.
    scope = collect.fleet_scope_args.DeclaredScope()
    dump, command, reason = recheck.read_target(
        target, recheck.RECHECKS[CHECK_ID], run=run, kubeconfig=kubeconfig, kube_context=kube_context, scope=scope
    )
    answer["command"] = command
    answer["checks"] = _unanswered(reason) if dump is None else judge(target, selector, dump)
    answer["eligible"] = all(c["passed"] for c in answer["checks"])
    return answer


def render_section(answers: list[dict], *, eligible: bool, noun: str) -> str:
    """The body section for a group's answers, closed by the hidden marker."""
    out = [
        SECTION_HEADING,
        "",
        (
            f"Read from the cluster just before this {noun} opened, to decide whether it may merge "
            "without a review."
        ),
    ]
    for answer in answers:
        out += ["", f"**`{answer['finding_id']}`**", ""]
        for check in answer["checks"]:
            out.append(f"- {'pass' if check['passed'] else 'FAIL'} `{check['name']}`: {check['detail']}")
        if answer["command"]:
            out += ["", "Reproduce with (read-only):", "", "```bash", _shown_command(answer["command"]), "```"]
    out += [
        "",
        "**Auto-merge:** eligible."
        if eligible
        else f"**Auto-merge:** not eligible; this {noun} waits for a person to review it.",
        "",
        ELIGIBLE_MARKER.format(verdict=ELIGIBLE if eligible else INELIGIBLE),
    ]
    return "\n".join(out)


def _shown_command(command: str) -> str:
    """The read without the sandbox-local `KUBECONFIG=` prefix, which names a
    temporary file a reader does not have."""
    words = shlex.split(command)
    return shlex.join(words[1:] if words and words[0].startswith("KUBECONFIG=") else words)
