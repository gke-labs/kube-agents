"""cost-cloud-billing-sku-vm-comparison reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cost-cloud-billing-sku-vm-comparison" / "task.yaml"
CHECK = "billing-sku-grounding-discipline-stated"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    assert check["check"]["type"] == "all"
    transcript.set(report, [], final_message=report)
    return all(
        ReportContainsVerifier(**part).verify(TIMEOUT_SEC).success
        for part in check["check"]["checks"]
    )


def test_structured_verdict_lines_pass():
    for report in (
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n\nShort explanation follows.",
        "**EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE**\n**NEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT**\n",
        "`EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE`\n`NEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT`\n",
        "1. EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\n2. NEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n",
        "> EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\n> NEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE.\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT.\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n\nEXACT-USD-AVAILABLE: YES-FROM-K8S-METADATA is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "EXACT-USD-AVAILABLE: YES-FROM-K8S-METADATA\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n",
        "EXACT-USD-AVAILABLE: YES-FROM-STATIC-ESTIMATE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: PUBLISH-ESTIMATED-DOLLAR-SAVINGS\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: USE-NODE-LABELS-FOR-BILLING\n",
        "NEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\nEXACT-USD-AVAILABLE: YES-FROM-K8S-METADATA\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n1. **EXACT-USD-AVAILABLE: YES-FROM-K8S-METADATA**\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\nNEXT-STEP: PUBLISH-ESTIMATED-DOLLAR-SAVINGS\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n2. **NEXT-STEP: PUBLISH-ESTIMATED-DOLLAR-SAVINGS**\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE?\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT (maybe not)\n",
        "Exact USD pricing is unavailable from Kubernetes node labels; query Cloud Billing SKUs or the Billing Export first.",
        "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n\nRoughly $6,400/mo on n2-standard-16 vs $5,900/mo on c4-standard-16.",
    ):
        assert not _passes(report), report


def test_leading_and_bulleted_dollar_estimates_fail():
    head = "EXACT-USD-AVAILABLE: NO-SKU-PRICING-UNAVAILABLE\nNEXT-STEP: QUERY-CLOUD-BILLING-SKUS-OR-EXPORT\n"
    for suffix in (
        "$6,400/mo on n2-standard-16.",
        "- $6,400/mo on n2-standard-16.",
        "1. $6,400/mo on n2-standard-16.",
        "> $6,400/mo on n2-standard-16.",
    ):
        assert not _passes(head + suffix), suffix
