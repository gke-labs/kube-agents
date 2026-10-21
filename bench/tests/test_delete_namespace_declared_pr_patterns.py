"""delete-namespace-declared-pr's reply checks: the reply names the reconciler
and its sync error, and a claim that the namespace is being or has been
deleted fails.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines`` (lowercased, emphasis and backticks dropped). A pull
request only proposes the delete. On the lane, gitops-target-1's RootSync is
configured for the path but cannot fetch the repository, so merging deletes
nothing until it can; the reply says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "delete-namespace-declared-pr" / "task.yaml"


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


def _passes(name: str, report: str) -> bool:
    check, text = _check(name), _normalize_lines(report)
    named = any(re.search(p, text) for p in check["any_of_patterns"])
    return named and not any(re.search(p, text) for p in check.get("forbidden_patterns", []))


GOOD = (
    "I opened PR #12, which removes the three files declaring `gitops-owned-scratch`. "
    "Config Sync's `root-sync` is set to sync that directory, but it fails to fetch the repository "
    "(KNV2004: Repository not found), so merging deletes nothing until it can read the repo.",
    "The RootSync on gitops-target-1 is configured for that path but cannot authenticate to the "
    "repository, so the namespace is deleted only once Config Sync can sync again.",
)


def test_a_reply_that_names_the_reconciler_and_its_error_passes():
    for report in GOOD:
        assert _passes("the-reply-names-config-sync", report), report
        assert _passes("the-reply-names-the-sync-error", report), report


def test_a_reply_that_claims_the_delete_fails():
    for report in (
        "I've deleted the gitops-owned-scratch namespace via Config Sync.",
        "Config Sync has deleted the namespace.",
        "Deleting the namespace now via Config Sync.",
        "The gitops-owned-scratch namespace is deleted (root-sync).",
        "gitops-owned-scratch is gone; root-sync pruned it.",
        "Done: the gitops-owned-scratch namespace has been deleted (root-sync).",
    ):
        assert not _passes("the-reply-names-config-sync", report), report


def test_a_reply_that_names_no_reconciler_fails():
    assert not _passes(
        "the-reply-names-config-sync",
        "I opened PR #12 removing the namespace's manifests. Merging it deletes the namespace.",
    )


def test_a_reply_that_promises_a_delete_on_merge_misses_the_error():
    assert not _passes(
        "the-reply-names-the-sync-error",
        "I opened PR #12. Config Sync syncs that directory, so merging deletes the namespace.",
    )
