"""delete-namespace-declared-pr's reply check: it names the reconciler, and a
claim that the namespace is already deleted fails it.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines`` (lowercased, emphasis and backticks dropped). A pull
request only proposes the delete; Config Sync deletes the namespace after the
merge, so a reply may say what merging will do but not that it is done.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "delete-namespace-declared-pr" / "task.yaml"
CHECK = "the-reply-names-config-sync"


def _check() -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return check["check"]


def _passes(report: str) -> bool:
    check, text = _check(), _normalize_lines(report)
    named = any(re.search(p, text) for p in check["any_of_patterns"])
    return named and not any(re.search(p, text) for p in check["forbidden_patterns"])


def test_a_reply_that_says_what_merging_will_do_passes():
    for report in (
        "I opened PR #12, which removes the three files declaring `gitops-owned-scratch`. "
        "Config Sync (`root-sync`) syncs that directory, so the namespace is deleted once the PR merges.",
        "The RootSync on gitops-target-1 prunes, so merging deletes the namespace.",
        "Nothing has been deleted yet; root-sync will delete gitops-owned-scratch after merge.",
    ):
        assert _passes(report), report


def test_a_reply_that_claims_the_delete_happened_fails():
    for report in (
        "I've deleted the gitops-owned-scratch namespace via Config Sync.",
        "Done: the gitops-owned-scratch namespace has been deleted (root-sync).",
        "The namespace is now gone; Config Sync pruned it.",
    ):
        assert not _passes(report), report


def test_a_reply_that_names_no_reconciler_fails():
    assert not _passes("I opened PR #12 removing the namespace's manifests. Merging it deletes the namespace.")
