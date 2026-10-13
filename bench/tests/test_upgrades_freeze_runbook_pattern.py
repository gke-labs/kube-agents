"""upgrades-freeze-runbook-probe's node-pool check matches the command, not prose about it.

The report_contains verifier runs ``re.search`` over the whole report after
``_normalize`` flattens it to one lowercased line, so a sentence naming
``clusters upgrade`` and ``--node-pool`` looks like a command unless the
pattern insists on the ``gcloud`` head and on argument-shaped tokens between
the head and the flag.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize

TASK = Path(__file__).resolve().parent.parent / "tasks" / "upgrades-freeze-runbook-probe" / "task.yaml"
CHECK = "the-runbook-uses-clusters-upgrade-node-pool"


def _matches(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return any(re.search(p, _normalize(report)) for p in check["check"]["any_of_patterns"])


def test_node_pool_upgrade_commands_match():
    for report in (
        "```\ngcloud container clusters upgrade prod-1 \\\n  --node-pool=default-pool \\\n"
        "  --cluster-version=1.33.2-gke.1 --location us-central1\n```",
        "gcloud container clusters upgrade $CLUSTER --location=$REGION --node-pool $POOL --cluster-version <version>",
        "gcloud beta container clusters upgrade prod-1 --node-pool=pool-a",
        "`gcloud container clusters upgrade prod-1 --location us-central1 --node-pool pool-a`",
    ):
        assert _matches(report), report


def test_prose_and_other_commands_do_not_match():
    for report in (
        "Node pools are upgraded with `clusters upgrade` and the `--node-pool` flag. Runbook: 1. "
        "`gcloud container node-pools upgrade default-pool --cluster prod-1 --cluster-version X`",
        "Step 2: `gcloud container node-pools upgrade default-pool --cluster prod-1 --cluster-version "
        "1.33.2-gke.1` (note: `clusters upgrade` is the control-plane form; add `--node-pool` only for a single pool)",
        "gcloud container clusters upgrade prod-1 --master --cluster-version X; then set --node-pool-soak-duration",
        "gcloud container clusters upgrade prod-1 --cluster-version X, then repeat with --node-pool for each pool",
        "gcloud container clusters upgrade prod-1 --node-pool-soak-duration=600s",
    ):
        assert not _matches(report), report
