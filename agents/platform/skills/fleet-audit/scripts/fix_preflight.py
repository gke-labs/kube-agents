"""fix_preflight.py - check a fix against its cluster before its pull request opens.

Whether a generated fix may merge without a person looking at it depends on
the cluster as it is when the pull request opens, not as the audit read it
hours earlier. So this reads the finding's namespace once more and runs the
fix's recipe's own safety checks on that read: `fix_recipes/<kind>/checks.py`,
loaded through `fix_recipes.registry.checks_module`, which is the same code the
credential broker runs on its own read before it labels the pull request.
`read_kinds` names the one read; `checks(change, live)` answers one named
result per check, and the fix is eligible for auto-merge only when every one
passes. A failing or unanswered check makes it ineligible and nothing else:
the pull request still opens, for a person to review. The answer is rendered
into the body (`render_section`, one section and marker for every recipe) and
carried in a hidden marker the auto-merge path reads.

What the fix is (`change`) is per kind (`CHANGES`). For `no-pdb` only the
budget `finish` generates is considered
(`budget_from_text`, then byte for byte against `audit_report._pdb_manifest`):
one `policy/v1` document in the finding's namespace with `maxUnavailable: 1`
and a selector, and nothing else in the file.

The read is `recheck.read_target`, the on-demand re-check's own, with the
recipe's `read_kinds`, pinned to the finding's cluster. The install's declared
scope is not re-applied there: the caller has no scope flags, so a cluster is
read only when this run's collector manifest shows it read (`check_fix`'s
`in_manifest`).

Nothing runs this unless the install's `KUBEAGENTS_AUTOMERGE` switch is not
`off` (`audit_report.open_remediation_pr`).
"""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import recheck  # first: through audit_report, it puts the shared scripts directory on the path
from fix_recipes import registry

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

BUDGET_API_VERSION = "policy/v1"
BUDGET_SPEC_KEYS = {"maxUnavailable", "selector"}
BUDGET_MAX_UNAVAILABLE = 1


def recipe_checks(kind: str):
    """The kind's `checks.py`, or None when the kind is unknown or ships none."""
    try:
        return registry.checks_module(kind)
    except registry.RecipeError:
        return None


def unanswered(kind: str, detail: str) -> list[dict]:
    """Every one of the kind's checks unanswered for one reason; one unnamed result for a kind with no checks."""
    checks = recipe_checks(kind)
    if checks is None:
        return [{"name": "safety-checks", "passed": False, "detail": detail, "status": "unknown"}]
    return checks.unanswered(detail)


def read_spec(kind: str, checks) -> recheck.Recheck:
    """The re-check's read for the kind, with the recipe's own `read_kinds`."""
    return recheck.RECHECKS[kind]._replace(kinds=checks.read_kinds)


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


def _no_pdb_change(finding: dict, file_text: str | None) -> tuple[SimpleNamespace | None, str]:
    """`(change, "")` for a no-pdb fix file that is, byte for byte, the budget
    `finish` generates for the finding; `(None, why)` otherwise."""
    budget = budget_from_text(file_text, str(finding.get("namespace") or "")) if file_text else None
    if budget is None:
        return None, (
            "the fix's file is not the budget the audit generates: one policy/v1 PodDisruptionBudget "
            "in the finding's namespace, maxUnavailable: 1 and a selector, and nothing else"
        )
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
        return None, "the fix's file is not the budget the audit generates for this finding, byte for byte"
    change = SimpleNamespace(
        object=str(finding.get("object") or ""),
        namespace=str(finding.get("namespace") or ""),
        name=name,
        selector=selector,
    )
    return change, ""


#: What the fix is, per kind: `(change, "")` or `(None, why)`. The change is
#: what the kind's `checks(change, live)` takes, and carries `name`, the object
#: the fix writes.
CHANGES: dict[str, Callable[[dict, str | None], tuple[SimpleNamespace | None, str]]] = {
    "no-pdb": _no_pdb_change,
}


def check_fix(
    finding: dict,
    file_text: str | None,
    *,
    in_manifest: bool,
    run: Callable | None = None,
    kubeconfig: str = "",
    kube_context: str = "",
) -> dict:
    """`{finding_id, command, eligible, checks}` for one finding whose fix file holds `file_text`.

    `in_manifest` says the audit's collector read the finding's cluster on
    this run, which is what puts it inside the install's declared scope: the
    caller has no scope flags of its own to hand `recheck`, and without that
    proof nothing is read and the fix is ineligible.
    """
    kind = str(finding.get("check") or "")
    answer = {"finding_id": str(finding.get("id", "")), "command": "", "eligible": False}
    checks = recipe_checks(kind)
    describe = CHANGES.get(kind)
    if checks is None or describe is None or kind not in recheck.RECHECKS:
        answer["checks"] = unanswered(kind, f"no recipe checks a {kind or 'unnamed'} fix before it opens")
        return answer
    change, why = describe(finding, file_text)
    if change is None:
        answer["checks"] = checks.unanswered(why)
        return answer
    target = recheck.Target(kind, str(finding.get("cluster") or ""), change.namespace, change.object, change.name)
    if not in_manifest:
        answer["checks"] = checks.unanswered(
            "this run's collector manifest does not show the finding's cluster as read, so it was not read again"
        )
        return answer
    # The collector's read of this cluster already applied the install's scope.
    scope = recheck.collect.fleet_scope_args.DeclaredScope()
    dump, command, reason = recheck.read_target(
        target, read_spec(kind, checks), run=run, kubeconfig=kubeconfig, kube_context=kube_context, scope=scope
    )
    answer["command"] = command
    answer["checks"] = checks.unanswered(reason) if dump is None else checks.checks(change, dump)
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
