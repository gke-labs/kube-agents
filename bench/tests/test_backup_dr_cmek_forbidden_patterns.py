"""backup-dr-cmek-selected-namespaces-probe forbids the rejected flags in use, not in mention.

The report_contains verifier ``re.search``es each forbidden pattern over
``_normalize_lines``: lowercased, Markdown emphasis and backticks dropped,
leading indentation collapsed, newlines kept. A substring would fail
``--enable-gke-backup-plan`` and a sentence, bullet or table that rules a flag
out, so each pattern needs command context: ``gcloud [beta|alpha] container``
earlier on the same line, or a line continued from one ending in a backslash.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "backup-dr-cmek-selected-namespaces-probe" / "task.yaml"
CHECK = "the-plan-uses-the-real-flags"


def _forbidden(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return any(re.search(p, _normalize_lines(report)) for p in check["check"]["forbidden_patterns"])


def test_rejected_flags_in_commands_are_forbidden():
    for report in (
        "```\ngcloud container clusters update prod-1 \\\n  --enable-gke-backup \\\n  --location=us-central1\n```",
        "gcloud container clusters update prod-1 --location=us-central1 --enable-gke-backup",
        "gcloud container clusters update prod-1 \\\n  --retention-days=30 \\",
        "gcloud container clusters update prod-1 \\\n  --retention-days 30 \\",
        "gcloud beta container backup-restore backup-plans create p --included-namespaces=payments,ledger",
        "gcloud beta container backup-restore backup-plans create p \\\n"
        "  --backup-encryption-key=projects/p/locations/us-central1/keyRings/k/cryptoKeys/gke",
        "gcloud container clusters update <CLUSTER_NAME> --enable-gke-backup --region <REGION> --quiet",
        'gcloud beta container backup-restore backup-plans create p \\\n  --retention-days 30 --cron-schedule "0 2 * * *" \\',
        'gcloud beta container backup-restore backup-plans create p \\\n  --cron-schedule="0 2 * * *" --retention-days=30 \\',
        "gcloud beta container backup-restore backup-plans create p \\\n"
        "  --selected-namespaces=payments,ledger --included-namespaces=payments \\",
        "gcloud container clusters update \\\n  prod-1 --enable-gke-backup --location=us-central1",
        "$ gcloud container clusters update prod-1 --enable-gke-backup",
        "Run: gcloud container clusters update prod-1 --location=us-central1 --enable-gke-backup",
        "1. gcloud container clusters update prod-1 --location=us-central1 --enable-gke-backup",
    ):
        assert _forbidden(report), report


def test_bounded_flags_and_mentions_are_not_forbidden():
    for report in (
        "./install.sh --enable-gke-backup-plan",
        "The plan keeps each backup 30 days via `--backup-retain-days`, not `--retention-days`.",
        "Use `--selected-namespaces`; `--included-namespaces` does not exist.",
        "  --encryption-key=projects/p/locations/us-central1/keyRings/k/cryptoKeys/gke \\",
        "gcloud container clusters update prod-1 --update-addons=BackupRestore=ENABLED --location=us-central1",
        "- `--retention-days`",
        "- `--retention-days` removed",
        "| `--backup-retain-days` | `--retention-days` |",
        "### Why not --included-namespaces",
        "Unlike gcloud's `--enable-gke-backup`, the addon flag is `--update-addons`.",
        "gcloud rejects `--retention-days`; use `--backup-retain-days`.",
        "gcloud does not have `--retention-days`; use `--backup-retain-days`.",
        "1. gcloud does not have --included-namespaces",
        "`--backup-encryption-key` is not a gcloud flag; the plan uses `--encryption-key`.",
        "**--retention-days** — not a gcloud flag",
        "Old flags:\n  --retention-days=30\n  --included-namespaces=payments",
    ):
        assert not _forbidden(report), report
