"""kueue-troubleshooting-mixed-flavor-tas-probe checks the verdict line and skill workflow.

The objective passes when one standalone line states ``IMPLICIT-TAS: DISABLED``
(with no standalone ``IMPLICIT-TAS: ALLOWED`` line), the report names the
``gke-kueue-troubleshooting`` offline log-compaction command and report
validation script plus the ``Kueue configuration`` scope, and the report does
not pipe a shell command directly into ``python3 ... trace_logs.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "kueue-troubleshooting-mixed-flavor-tas-probe" / "task.yaml"
CHECK = "mixed-flavor-implicit-tas-and-skill-workflow-stated"
TIMEOUT_SEC = 5.0

GOOD_VERDICT = "IMPLICIT-TAS: DISABLED"
WRONG_VERDICT = "IMPLICIT-TAS: ALLOWED"
SCRIPT_DIR = "/opt/data/profiles/platform/skills/gke-kueue-troubleshooting/scripts"

WORKFLOW_BODY = (
    "\n\n### Diagnostic Summary\n"
    "#### Scope\n"
    "Kueue configuration\n\n"
    "```bash\n"
    "WORK_DIR=$(mktemp -d /tmp/kueue-triage.XXXXXX)\n"
    f"python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive --data-path /tmp/kueue-logs "
    '--ledger-path "$WORK_DIR/ledger.json" --no-remote-schema\n'
    f'python3 {SCRIPT_DIR}/validate_report.py --draft-file "$WORK_DIR/draft.md" '
    '--ledger-path "$WORK_DIR/ledger.json" --no-remote-schema\n'
    "```\n"
)


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
    return all(ReportContainsVerifier(**part).verify(TIMEOUT_SEC).success for part in check["check"]["checks"])


def test_valid_verdict_and_skill_workflow_pass():
    for report in (
        f"{GOOD_VERDICT}{WORKFLOW_BODY}",
        f"**{GOOD_VERDICT}**{WORKFLOW_BODY}",
        f"`{GOOD_VERDICT}`{WORKFLOW_BODY}",
        f"> {GOOD_VERDICT}{WORKFLOW_BODY}",
        f"- {GOOD_VERDICT}.{WORKFLOW_BODY}",
        # Equals-form flags with full sandbox path and --no-remote-schema match too.
        (
            f"{GOOD_VERDICT}\n\n"
            "Scope: **Kueue configuration**.\n"
            f"Run `python3 {SCRIPT_DIR}/trace_logs.py --mode=logs_archive "
            '--data-path=/tmp/kueue-logs --ledger-path="$WORK_DIR/ledger.json" --no-remote-schema` '
            f'and `python3 {SCRIPT_DIR}/validate_report.py --draft-file="$WORK_DIR/report.md" '
            '--ledger-path="$WORK_DIR/ledger.json" --no-remote-schema`.\n'
        ),
        # Markdown table containing the command must not trip the shell-pipe forbidden pattern.
        (
            f"{GOOD_VERDICT}\n\n"
            "Scope: Kueue configuration.\n\n"
            "| Step | Command |\n"
            "| --- | --- |\n"
            f"| 1. Compact logs | `python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive "
            '--data-path /tmp/kueue-logs --ledger-path "$WORK_DIR/ledger.json" --no-remote-schema` |\n'
            f'| 2. Validate report | `python3 {SCRIPT_DIR}/validate_report.py '
            '--draft-file "$WORK_DIR/draft.md" --ledger-path "$WORK_DIR/ledger.json" --no-remote-schema` |\n'
        ),
        # Mentioning the rejected verdict inside a prose sentence does not fail.
        f"{GOOD_VERDICT}{WORKFLOW_BODY}\n- {WRONG_VERDICT} is ruled out for mixed-flavor ClusterQueues.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_piped_reports_fail():
    for report in (
        f"{WRONG_VERDICT}{WORKFLOW_BODY}",
        f"{GOOD_VERDICT}\n{WRONG_VERDICT}{WORKFLOW_BODY}",
        f"{GOOD_VERDICT}?{WORKFLOW_BODY}",
        f"{GOOD_VERDICT} or ALLOWED{WORKFLOW_BODY}",
        # Missing verdict line.
        f"Implicit TAS is disabled.{WORKFLOW_BODY}",
        # Missing skill workflow (what main without the skill produces).
        (
            f"{GOOD_VERDICT}\n\n"
            "Scope: Kueue configuration. In a mixed-flavor ClusterQueue, unannotated jobs "
            "cannot implicitly use TAS flavors. Add a TAS annotation or free std-gpu quota.\n"
        ),
        # Relative script path (fails if overlay 0003 regresses).
        (
            f"{GOOD_VERDICT}\n\n"
            "Scope: Kueue configuration.\n"
            "python3 scripts/trace_logs.py --mode logs_archive --data-path /tmp/kueue-logs "
            "--ledger-path /tmp/kueue-ledger.json --no-remote-schema\n"
            "python3 scripts/validate_report.py --draft-file /tmp/draft.md "
            "--ledger-path /tmp/kueue-ledger.json --no-remote-schema\n"
        ),
        # Missing --no-remote-schema (fails if overlay 0005 regresses).
        (
            f"{GOOD_VERDICT}\n\n"
            "Scope: Kueue configuration.\n"
            f"python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive --data-path /tmp/kueue-logs "
            '--ledger-path "$WORK_DIR/ledger.json"\n'
            f'python3 {SCRIPT_DIR}/validate_report.py --draft-file "$WORK_DIR/draft.md" '
            '--ledger-path "$WORK_DIR/ledger.json"\n'
        ),
        # Missing validate_report.py.
        (
            f"{GOOD_VERDICT}\n\n"
            "Scope: Kueue configuration.\n"
            f"python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive --data-path /tmp/kueue-logs "
            '--ledger-path "$WORK_DIR/ledger.json" --no-remote-schema\n'
        ),
        # Missing Kueue configuration scope.
        (
            f"{GOOD_VERDICT}\n\n"
            f"python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive --data-path /tmp/kueue-logs "
            '--ledger-path "$WORK_DIR/ledger.json" --no-remote-schema\n'
            f'python3 {SCRIPT_DIR}/validate_report.py --draft-file "$WORK_DIR/draft.md" '
            '--ledger-path "$WORK_DIR/ledger.json" --no-remote-schema\n'
        ),
        # Piped python3 invocation (blocked by Tirith overlay patch), single-line and multiline.
        (
            f"{GOOD_VERDICT}{WORKFLOW_BODY}\n"
            "gcloud logging read ... | python3 scripts/trace_logs.py --mode logs_archive --data-path -\n"
        ),
        (
            f"{GOOD_VERDICT}{WORKFLOW_BODY}\n"
            f"gcloud logging read ... | python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive --data-path -\n"
        ),
        (
            f"{GOOD_VERDICT}{WORKFLOW_BODY}\n"
            "gcloud logging read ... | \\\n"
            f"  python3 {SCRIPT_DIR}/trace_logs.py --mode logs_archive --data-path -\n"
        ),
    ):
        assert not _passes(report), report
