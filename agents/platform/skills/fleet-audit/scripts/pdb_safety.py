"""pdb_safety.py - check a disruption-budget fix against its cluster before its pull request opens.

A `no-pdb` fix adds one PodDisruptionBudget beside a workload. Only the
budget `finish` generates is considered (`budget_from_text`): one `policy/v1`
document in the finding's namespace with `maxUnavailable: 1` and a selector,
and nothing else in the file. Whether that fix may merge without a person
looking at it depends on the cluster as it is when the pull request opens, not
as the audit read it hours earlier, so this reads the workload's namespace
once more and answers three questions:

- `replicas-ready`: the workload asks for at least two replicas (and an
  autoscaler that owns it cannot go below two), all of them Ready, in a
  status that has caught up with its spec. With one replica the
  budget leaves a drain taking the only pod, and pods that are not Ready count
  as already disrupted, so the budget permits no eviction and blocks drains.
- `no-overlapping-budget`: no live PodDisruptionBudget already selects the
  workload's pods (the eviction API refuses a pod two budgets select), and
  none already takes the fix's name (applying the fix would replace it).
- `selector-matches`: the budget the fix writes selects the workload's pods,
  carries the workload's own live selector, and selects no other workload's.

All three must pass for the fix to be eligible for auto-merge. A failing or
unanswered check makes it ineligible and nothing else: the pull request still
opens, for a person to review. The answer is rendered into the body
(`render_section`) and carried in a hidden marker the auto-merge path reads.

The read is `recheck.read_target`, the on-demand re-check's own (plus Jobs
and CronJobs, see `READ`), pinned to the finding's cluster. The install's
declared scope is not re-applied there: the caller has no scope flags, so a
cluster is read only when this run's collector manifest shows it read
(`check_fix`'s `in_manifest`). The workload is found by
`recheck.in_scope_workload`, so the audit's workload filters (S1-S5) hold
here too.

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
# The re-check's own read plus Jobs and CronJobs: a budget that also selects a
# Job's pods permits no evictions (`collect.check_no_pdb` withholds the
# selector for that reason), so `selector-matches` has to see them.
READ = recheck.RECHECKS[CHECK_ID]._replace(kinds=recheck.RECHECKS[CHECK_ID].kinds + ",cronjobs,jobs")
SECTION_HEADING = "## Safety checks"
# For the auto-merge path, which nothing reads yet. Written only by this
# harness, last in its section, into a body it renders; the body's other
# free text passes `publishable_text`, which cannot spell `<!--`. A body
# anyone else wrote or edited proves nothing by carrying it, so a reader
# trusts it only on a pull request this harness opened, or re-checks.
ELIGIBLE_MARKER = "<!-- automerge-safety: {verdict} -->"
ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"
ELIGIBLE_MARKER_PATTERN = re.compile(r"<!-- automerge-safety: (eligible|ineligible) -->")


def _result(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def _unanswered(detail: str) -> list[dict]:
    return [_result(name, False, detail) for name in CHECK_NAMES]


BUDGET_API_VERSION = "policy/v1"
BUDGET_SPEC_KEYS = {"maxUnavailable", "selector"}
BUDGET_MAX_UNAVAILABLE = 1


def budget_from_text(text: str, namespace: str) -> tuple[str, dict] | None:
    """`(name, selector)` of the budget in a fix's file, or None unless the
    file holds exactly one document, a `policy/v1` PodDisruptionBudget in
    `namespace` whose spec is `maxUnavailable: 1` and a selector -- the form
    `finish` generates. Anything else in the file, another namespace, or a
    `minAvailable` that could block every drain is a fix for a person to read."""
    import yaml

    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return None
    if len(docs) != 1 or not isinstance(docs[0], dict) or docs[0].get("kind") != "PodDisruptionBudget" or docs[0].get("apiVersion") != BUDGET_API_VERSION:
        return None
    metadata = docs[0].get("metadata") or {}
    spec = docs[0].get("spec") or {}
    name = str(metadata.get("name") or "")
    selector = spec.get("selector")
    if (
        not name
        or metadata.get("namespace") != namespace
        or set(spec) != BUDGET_SPEC_KEYS
        or spec.get("maxUnavailable") != BUDGET_MAX_UNAVAILABLE
        or not isinstance(selector, dict)
    ):
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
    raw = _raw_item(dump, workload)
    status = raw.get("status") or {}
    wanted = workload["spec"].get("replicas", 1) or 1
    # An autoscaler can take the workload down to its floor after this read,
    # so the floor is what has to reach MIN_REPLICAS (as `blocking-pdb` does).
    hpa = collect._hpa_targeting(workload, subject.context)
    floor = ((hpa.get("spec") or {}).get("minReplicas") or collect.DEFAULT_HPA_MIN_REPLICAS) if hpa else wanted
    ready = int(status.get("readyReplicas") or 0)
    generation = (raw.get("metadata") or {}).get("generation")
    observed = status.get("observedGeneration")
    # A status the controller has not caught up with describes the old spec.
    stale = generation is not None and (observed is None or observed < generation)
    results = [
        _result(
            REPLICAS_READY,
            min(wanted, floor) >= MIN_REPLICAS and ready >= wanted and not stale,
            f"{target.object}: replicas={wanted}, ready={ready}"
            + (f", autoscaler floor={floor}" if hpa else "")
            + (f", status from generation {observed} of {generation}" if stale else ""),
        )
    ]

    live = subject.context["pdbs"].get(target.namespace, [])
    overlapping = sorted(recheck._name(pdb) for pdb in live if recheck._selects(pdb, workload["pod_labels"]))
    named = any(recheck._name(pdb) == target.budget for pdb in live)
    if overlapping:
        why = f"already selected by PodDisruptionBudget {', '.join(overlapping)}"
    elif named:
        why = f"PodDisruptionBudget {target.budget} already exists in {target.namespace}, and the fix would replace it"
    else:
        why = ""
    results.append(
        _result(NO_OVERLAPPING_BUDGET, not why, why or f"no PodDisruptionBudget in {target.namespace} selects its pods")
    )

    own = workload["spec"].get("selector")
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
    elif selector != own:
        why = f"the budget's selector differs from the workload's live selector {json.dumps(own, sort_keys=True)}"
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
    budget = budget_from_text(file_text, str(finding.get("namespace") or "")) if file_text else None
    if budget is None:
        answer["checks"] = _unanswered(
            "the fix's file is not the budget the audit generates: one policy/v1 PodDisruptionBudget "
            "in the finding's namespace, maxUnavailable: 1 and a selector, and nothing else"
        )
        return answer
    name, selector = budget
    # Byte for byte what `finish` writes for this finding, or nothing is read:
    # a worker-written budget, another namespace, another min or max, or a
    # second document is a fix for a person to read.
    generated = recheck.audit_report._pdb_manifest(
        {
            "cluster": str(finding.get("cluster") or ""),
            "object": str(finding.get("object") or ""),
            "namespace": str(finding.get("namespace") or ""),
            "pod_selector": selector,
        },
        str(finding.get("id", "")),
    )
    if generated is None or generated[1] != file_text:
        answer["checks"] = _unanswered("the fix's file is not the budget the audit generates for this finding, byte for byte")
        return answer
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
        target, READ, run=run, kubeconfig=kubeconfig, kube_context=kube_context, scope=scope
    )
    answer["command"] = command
    answer["checks"] = _unanswered(reason) if dump is None else judge(target, selector, dump)
    answer["eligible"] = all(c["passed"] for c in answer["checks"])
    return answer


def render_section(
    answers: list[dict], *, eligible: bool, noun: str, clean: Callable[[str], str] = str, practice: bool = False
) -> str:
    """The body section for a group's answers, closed by the hidden marker.

    `clean` is applied to every detail and command, which carry cluster
    names, stderr and exception text: the caller passes the redaction its
    published bodies use."""
    out = [
        SECTION_HEADING,
        "",
        (
            f"Read from the cluster just before this {noun} opened, to decide whether it may merge "
            "without a review."
        ),
    ]
    for answer in answers:
        out += ["", f"**`{clean(answer['finding_id'])}`**", ""]
        for check in answer["checks"]:
            out.append(f"- {'pass' if check['passed'] else 'FAIL'} `{check['name']}`: {clean(check['detail'])}")
        if answer["command"]:
            out += ["", "Reproduce with (read-only):", "", "```bash", clean(_shown_command(answer["command"])), "```"]
    out += [
        "",
        ("**Would auto-merge (practice).**" if practice else "**Auto-merge:** eligible.")
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
