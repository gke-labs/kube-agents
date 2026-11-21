"""The `no-pdb` fix: the PodDisruptionBudget the fleet audit writes for a workload that has none.

`generate` is the obtainability SOP's §3.3 budget: `maxUnavailable: 1` and the
workload's `spec.selector` verbatim, which the collector carries on the
finding's candidate as `pod_selector`. `unbuildable_reason` is the one place
the refusals are decided, so the note `finish` gives the worker names the same
refusal `generate` made.

`read_inputs` is the broker's half: what a file that claims to be this fix
names, read from the pull request's file and held to what the generator could
have been given. It is untrusted extraction; the broker accepts the file only
when `generate` on those inputs gives the same bytes
(`automerge_eligibility.verify_added_manifest`).

The fleet audit calls this through `audit_report._pdb_manifest`. Load it with
`registry.generator("no-pdb")`: the directory's name is the kind, which has a
hyphen, so it is not an importable package name. Stdlib only, like the registry.
"""

from __future__ import annotations

import json
import re

KIND = "no-pdb"
# The new file's name is the workload's name plus this.
BUDGET_SUFFIX = "-pdb"
# A DNS-1123 subdomain, what Kubernetes requires of a PDB's name and namespace.
KUBERNETES_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
# The selector keys `policy/v1` takes, and nothing else.
LABEL_SELECTOR_KEYS = frozenset({"matchLabels", "matchExpressions"})
# What `read_inputs` accepts in each field, narrower than what `generate`
# writes: the workloads a budget can count through their scale subresource,
# and the shapes the audit's finding ids and cluster names take.
BUDGET_TARGET_KINDS = ("Deployment", "StatefulSet")
FINDING_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?")
CLUSTER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
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


def unbuildable_reason(candidate: dict) -> str:
    """Why `generate` builds nothing for `candidate`, or "" where it builds one."""
    selector = candidate.get("pod_selector")
    if not isinstance(selector, dict) or not selector:
        return "the collector attached no selector to write it from"
    if set(selector) - LABEL_SELECTOR_KEYS:
        return "the collector's selector has keys `policy/v1` does not take"
    if not any(selector.get(key) for key in LABEL_SELECTOR_KEYS):
        return "the collector's selector is empty, and would select every pod in the namespace"
    workload = str(candidate.get("object") or "").partition("/")[2]
    if not KUBERNETES_NAME_PATTERN.match(f"{workload}{BUDGET_SUFFIX}"):
        return "its name would not be a valid Kubernetes name"
    if not KUBERNETES_NAME_PATTERN.match(str(candidate.get("namespace") or "")):
        return "the collector named no valid namespace for it"
    return ""


def generate(candidate: dict, finding_id: str) -> tuple[str, str] | None:
    """`(name, text)` of the PodDisruptionBudget for `candidate`, or None.

    None where it cannot be written safely (`unbuildable_reason`): no selector,
    an empty one (which would select every pod in the namespace), keys
    `policy/v1` does not take, or a name or namespace Kubernetes refuses.
    The text is pinned byte for byte by `test_no_pdb_fix.py`; the broker
    re-runs this function to verify a pull request's file
    (`automerge_eligibility.verify_added_manifest`).
    """
    if unbuildable_reason(candidate):
        return None
    selector = candidate["pod_selector"]
    name = f"{str(candidate.get('object') or '').partition('/')[2]}{BUDGET_SUFFIX}"
    namespace = str(candidate.get("namespace") or "")
    # JSON is YAML, so the selector is written exactly as it was read.
    text = (
        f"# {candidate.get('cluster', '')}: {KIND} on {candidate.get('object')} "
        f"({finding_id})\n"
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


def read_inputs(text: str) -> tuple[dict, str] | None:
    """`(candidate, finding_id)` a generated budget names, or None.

    Only fields the audit's own findings carry: a Deployment or StatefulSet, a
    non-empty LabelSelector of strings, and names, a finding id and a cluster
    of the shapes the audit writes. Nothing here proves the file is the fix;
    the caller re-runs `generate` on the result and compares bytes.
    """
    header = _HEADER.match(text)
    body = _BODY.fullmatch(text, header.end()) if header else None
    if body is None:
        return None
    try:
        selector = json.loads(body["selector"])
    except json.JSONDecodeError:
        return None
    if not _selector_shaped(selector):
        return None
    kind, _, workload = header["object"].partition("/")
    if not (
        kind in BUDGET_TARGET_KINDS
        and KUBERNETES_NAME_PATTERN.match(workload)
        and FINDING_ID.fullmatch(header["finding_id"])
        and CLUSTER_NAME.fullmatch(header["cluster"])
    ):
        return None
    candidate = {
        "cluster": header["cluster"],
        "object": header["object"],
        "namespace": body["namespace"],
        "pod_selector": selector,
    }
    return candidate, header["finding_id"]


def _selector_shaped(selector: object) -> bool:
    """Whether `selector` is a LabelSelector's shape: string labels, and
    expressions with a string key and operator and a list of string values."""
    if not isinstance(selector, dict) or set(selector) - LABEL_SELECTOR_KEYS:
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
