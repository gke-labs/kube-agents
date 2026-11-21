"""The `no-pdb` fix: the PodDisruptionBudget the fleet audit writes for a workload that has none.

`generate` is the obtainability SOP's §3.3 budget: `maxUnavailable: 1` and the
workload's `spec.selector` verbatim, which the collector carries on the
finding's candidate as `pod_selector`. `unbuildable_reason` is the one place
the refusals are decided, so the note `finish` gives the worker names the same
refusal `generate` made.

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
    The text is pinned byte for byte by `test_no_pdb_fix.py`; the broker's
    `automerge_eligibility.render_budget` must match it.
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
