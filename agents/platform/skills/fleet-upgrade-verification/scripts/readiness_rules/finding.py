#!/usr/bin/env python3
"""
finding.py — the tiered-finding helpers the per-entry rules for catalogue entries 6, 8, 9
and 10 import, on top of the result shape the package docstring fixes; shipped with the
registry before those rules.

Not a rule. A rule's `evaluate` returns `empty_result()` filled in: three lists of findings,
one per tier, plus `notes`, the strings the report appends to the member's note column for
what the rule read but did not file (a provider's caller, a namespace pinned to a version,
a sampled read). Every finding carries `rule` and `tier`; an `unknown` finding carries
`reason`. `pod_spec` is the package's one pod-template walk, so a kind added for one rule is
read by every rule.
"""

from readiness_rules import RESULT_KEYS, new_result, owner_label
from readiness_rules import pod_template_spec as pod_spec  # noqa: F401 - the name the rules read

TIER_BLOCKING = "blocking"
TIER_RISK = "risk"
TIER_UNKNOWN = "unknown"
# The result keys, in the order the report renders them.
RESULT_BLOCKING = "blocking"
RESULT_RISKS = "risks"
RESULT_UNKNOWN = "unknown"
RESULT_NOTES = "notes"


def empty_result() -> dict:
    return new_result()


def unknown(rule_id: str, reason: str) -> dict:
    return {"rule": rule_id, "tier": TIER_UNKNOWN, "reason": reason}


def add_note(result: dict, note: str) -> None:
    """Appends one clause to the result's notes; empty clauses are dropped."""
    if note:
        result.setdefault(RESULT_NOTES, []).append(note)


def workload_label(item: dict) -> str:
    """`Deployment ns/name`, how every rule names a workload."""
    meta = item.get("metadata") or {}
    return owner_label({"kind": item.get("kind"), "namespace": meta.get("namespace", ""), "name": meta.get("name", "")})


__all__ = ["RESULT_KEYS", "empty_result", "unknown", "add_note", "pod_spec", "workload_label"]
