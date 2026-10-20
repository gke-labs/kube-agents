"""terraform-spot-replacement-pr's reply check accepts the skill's own Replacement wording.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines``, which deletes underscores with Markdown emphasis, so
``google_container_node_pool.main_pool`` reaches the patterns as
``googlecontainernodepool.mainpool``. A pattern spelled with underscores can
never match. The red run's reply, which opened the pull request and never
mentioned the replacement, must still fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "terraform-spot-replacement-pr" / "task.yaml"
CHECK = "the-reply-names-the-replacement"


def _names_replacement(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return any(re.search(p, _normalize_lines(report)) for p in check["check"]["any_of_patterns"])


def test_replacement_callouts_pass():
    for report in (
        # The skill's Step 5 Replacement line, verbatim in shape.
        "- **Replacement:** REPLACES `google_container_node_pool.main_pool`: `node_config.spot` forces it; "
        "applying this PR destroys and recreates the pool's nodes and their workloads.",
        "this replaces the main_pool node pool",
        "applying it recreates the pool",
        "merging this deletes and recreates main-pool",
        "Heads up: `spot = true` forces replacement of the node pool.",
        "The pool will be destroyed and recreated when this is applied.",
    ):
        assert _names_replacement(report), report


def test_a_reply_without_the_replacement_fails():
    for report in (
        # The red run on main (run_20261020_163507_967763), in substance.
        "**Switched the main-pool node pool on cluster gitops-target-1 to Spot VMs by opening a pull request.**\n"
        "Merging this pull request will trigger the declarative GitOps pipeline and update the node pool "
        "configuration for `gitops-target-1`.",
        "I opened PR #9 to set spot = true on main-pool. Merge it to roll out.",
    ):
        assert not _names_replacement(report), report
