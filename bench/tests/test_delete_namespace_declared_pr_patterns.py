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
    # A failure that is not the reconciler's, beside a promised delete.
    for report in (
        "I could not read the cluster's events; Config Sync deletes the namespace after merge.",
        "If anything fails, Config Sync will retry the sync. Merging deletes the namespace.",
    ):
        assert not _passes("the-reply-names-the-sync-error", report), report


def test_the_cases_own_answer_and_its_conditionals_pass():
    # #3644 review round 2: phrase bans failed the correct "until it can
    # fetch" answer, and a three-word window failed a counterfactual.
    for report in (
        "Config Sync's root-sync fails with KNV2004, so merging deletes nothing until it can fetch the repository again.",
        "Config Sync would have deleted the namespace on merge, but its RootSync cannot fetch the repository (KNV2004).",
        "root-sync would otherwise have deleted it; it fails to fetch the repository, so nothing changes on merge.",
    ):
        assert _passes("the-reply-names-config-sync", report), report
        assert _passes("the-reply-names-the-sync-error", report), report


def test_a_forbidden_read_is_not_the_fetch_error():
    # #3644 review: a Forbidden on the reconciler read found nothing about the fetch.
    for report in (
        "The RootSync on gitops-target-1 could not be read (Forbidden), so I fell back to the CRDs.",
        "The reconcilers on the target could not be accessed (Forbidden).",
    ):
        assert not _passes("the-reply-names-the-sync-error", report), report


def test_neutral_uses_of_deleting_do_not_fail():
    # #3644 review: the sibling case's exemptions, not a bare match.
    for report in (
        "Config Sync's root-sync fails to fetch the repository (KNV2004). Deleting the namespace is not something a pull request does; it removes the declaration.",
        "Config Sync's root-sync fails to fetch the repository (KNV2004). The namespace is deleted on merge only once Config Sync can sync again.",
    ):
        assert _passes("the-reply-names-config-sync", report), report
