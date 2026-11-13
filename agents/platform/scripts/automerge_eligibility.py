"""The no-pdb fix's file format, for the credential broker.

The fleet audit writes one fix in code rather than through the model: a
PodDisruptionBudget for a workload with none (`no-pdb`), fully determined by the
workload's selector. Before the broker may mark such a pull request as one a
pipeline could merge without a review, it has to establish what the pull request
adds itself, because nothing the sandbox reports can be trusted for that:

- `render_budget` is byte for byte what `audit_report._pdb_manifest` writes, and
  `parse_budget` accepts a file only when it holds fields the generator can
  write and re-rendering them gives the same bytes, so a file anyone edited or
  wrote by hand is not the generated fix.
- `added_file_from_diff` and `path_fits`: the pull request adds that one file,
  under its own name, in its cluster's own tree, and changes nothing else.

`fleet-audit/scripts/test_broker_budget_parity.py` holds the format to the
audit's. Stdlib only: the credential-proxy image ships this directory and
nothing from the fleet-audit skill. Nothing calls it yet.
"""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple

#: The fix type the rules file names (`automation_rules.KNOWN_FIX_TYPES`).
FIX_TYPE = "no-pdb"
#: The workloads a budget can count through their scale subresource, and so the
#: only ones a generated budget is written for.
BUDGET_TARGET_KINDS = ("Deployment", "StatefulSet")
SELECTOR_KEYS = ("matchLabels", "matchExpressions")

#: What the generator can write in each field (`audit_report.KUBERNETES_NAME_PATTERN`,
#: `audit_report.FINDING_ID_RE`); a file naming anything else is not its output.
KUBERNETES_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
FINDING_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?")
CLUSTER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")

BUDGET_SUFFIX = "-pdb"
# The per-cluster tree of the reference GitOps layout, `clusters/<name>/`.
CLUSTER_TREE_ROOT = "clusters"
_HEADER = re.compile(
    r"# (?P<cluster>[^\n]*): no-pdb on (?P<object>[A-Za-z]+/[^\s()]+) \((?P<finding_id>[^\n()]*)\)\n"
)
_BODY = re.compile(
    r"apiVersion: policy/v1\n"
    r"kind: PodDisruptionBudget\n"
    r"metadata:\n"
    r"  name: (?P<name>[^\n]+)\n"
    r"  namespace: (?P<namespace>[^\n]+)\n"
    r"spec:\n"
    r"  maxUnavailable: 1\n"
    r"  selector: (?P<selector>\{[^\n]*\})\n"
)

_DIFF_HEADER = re.compile(r"diff --git a/(?P<a>[^\s\"]+) b/(?P<b>[^\s\"]+)")
_HUNK = re.compile(r"@@ -0,0 \+1(?:,(?P<count>\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"


class Budget(NamedTuple):
    """What a generated budget file names."""

    cluster: str
    object: str
    finding_id: str
    name: str
    namespace: str
    selector: dict


def render_budget(cluster: str, obj: str, namespace: str, selector: dict, finding_id: str) -> tuple[str, str]:
    """`(name, text)` of the budget the audit writes for `obj`'s `no-pdb` finding.

    `maxUnavailable: 1` and the workload's `spec.selector` verbatim. The
    caller has already refused a selector or a name this cannot write safely
    (`audit_report._pdb_unbuildable_reason`).
    """
    name = f"{obj.partition('/')[2]}{BUDGET_SUFFIX}"
    # JSON is YAML, so the selector is written exactly as it was read.
    text = (
        f"# {cluster}: {FIX_TYPE} on {obj} ({finding_id})\n"
        "apiVersion: policy/v1\n"
        "kind: PodDisruptionBudget\n"
        "metadata:\n"
        f"  name: {name}\n"
        f"  namespace: {namespace}\n"
        "spec:\n"
        "  maxUnavailable: 1\n"
        f"  selector: {json.dumps(selector, sort_keys=True)}\n"
    )
    return name, text


def parse_budget(text: str) -> Budget | None:
    """What a generated budget file names, or None unless re-rendering it
    gives the same bytes: one budget, `maxUnavailable: 1`, nothing else."""
    header = _HEADER.match(text)
    body = _BODY.fullmatch(text, header.end()) if header else None
    if body is None:
        return None
    try:
        selector = json.loads(body["selector"])
    except json.JSONDecodeError:
        return None
    # An empty selector selects every pod in the namespace; the generator
    # refuses one (`audit_report._pdb_unbuildable_reason`), so it is never its output.
    if not _selector_shaped(selector) or not any(selector.get(key) for key in SELECTOR_KEYS):
        return None
    budget = Budget(
        header["cluster"], header["object"], header["finding_id"], body["name"], body["namespace"], selector
    )
    kind, _, obj_name = budget.object.partition("/")
    # Only what the generator can write: a carriage return, or any byte past
    # printable ASCII, could end a line for a YAML reader and carry a second
    # document through a byte-exact round trip.
    if not all(c == "\n" or " " <= c <= "~" for c in text):
        return None
    if not (
        KUBERNETES_NAME.fullmatch(budget.name)
        and KUBERNETES_NAME.fullmatch(budget.namespace)
        and KUBERNETES_NAME.fullmatch(obj_name)
        and kind in BUDGET_TARGET_KINDS
        and FINDING_ID.fullmatch(budget.finding_id)
        and CLUSTER_NAME.fullmatch(budget.cluster)
    ):
        return None
    name, rendered = render_budget(budget.cluster, budget.object, budget.namespace, selector, budget.finding_id)
    return budget if rendered == text and name == budget.name else None


def added_file_from_diff(diff: Any) -> tuple[str, str] | None:
    """`(path, text)` when a unified diff adds exactly one new file and changes
    nothing else, else None. A quoted path (one git had to escape) is refused
    rather than unquoted."""
    if not isinstance(diff, str):
        return None
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if sum(line.startswith("diff --git ") for line in lines) != 1:
        return None
    header = _DIFF_HEADER.fullmatch(lines[0]) if lines else None
    if header is None or header["a"] != header["b"]:
        return None
    path = header["b"]
    at = 1
    # A diff with no mode line is still read; `--- /dev/null` below is what says
    # the file is new. One that does must be a plain file's.
    if at < len(lines) and lines[at].startswith("new file mode "):
        if lines[at] != "new file mode 100644":
            return None
        at += 1
    if at < len(lines) and lines[at].startswith("index "):
        at += 1
    if lines[at : at + 2] != ["--- /dev/null", f"+++ b/{path}"]:
        return None
    at += 2
    hunk = _HUNK.fullmatch(lines[at]) if at < len(lines) else None
    if hunk is None:
        return None
    body = lines[at + 1 :]
    newline = True
    if body and body[-1] == _NO_NEWLINE:
        body.pop()
        newline = False
    if not body or not all(line.startswith("+") for line in body):
        return None
    if len(body) != int(hunk["count"] or 1):
        return None
    return path, "\n".join(line[1:] for line in body) + ("\n" if newline else "")


def _selector_shaped(selector: Any) -> bool:
    """Whether `selector` is a LabelSelector's shape: string labels, and
    expressions with a string key and operator and a list of string values."""
    if not isinstance(selector, dict) or set(selector) - set(SELECTOR_KEYS):
        return False
    labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        return False
    return isinstance(expressions, list) and all(
        isinstance(e, dict)
        and isinstance(e.get("key"), str)
        and isinstance(e.get("operator"), str)
        and isinstance(e.get("values", []), list)
        and all(isinstance(v, str) for v in e.get("values", []))
        for e in expressions
    )


def path_fits(path: str, budget: Budget) -> bool:
    """Whether the file's path can hold `budget`: named for it, under no
    dot-directory, and inside its cluster's own tree of the reference layout,
    `clusters/<name>/`, by the cluster's short name, the last segment of
    `project/location/name`. A shared directory could be applied to every
    cluster. The tree is matched by short name only, so a repository whose
    layout reuses one short name for two clusters cannot be told apart here."""
    parts = path.split("/")
    if parts[-1] != f"{budget.name}.yaml" or any(part.startswith(".") or not part for part in parts):
        return False
    return len(parts) > 2 and parts[0] == CLUSTER_TREE_ROOT and parts[1] == budget.cluster.rsplit("/", 1)[-1]
