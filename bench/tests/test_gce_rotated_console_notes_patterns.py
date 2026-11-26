"""gce-rotated-console-notes grades the harness's verbatim note lead, not free text.

`finish` writes the `report_notes` lines (each opening with
``REPORT_NOTE_PREFIX`` in ``audit_report.py``) and the fleet-audit skill's
report step copies them into the report verbatim, directly before the closing
ledger line. The case's third check requires that lead in the report.
Free-text clean-claim regexes failed in both directions — a correct report
with a clause after a comma ("serial-flood-x is not visible, the rest of the
fleet is healthy.") tripped them, and "serial-flood-x: clean." passed — so
the check pins the mechanism instead of the wording. This test holds the
task's phrase equal to the constant `finish` writes, and pins the three
checks' shapes to what the case header describes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

BENCH = Path(__file__).resolve().parent.parent
TASK = BENCH / "tasks" / "gce-rotated-console-notes" / "task.yaml"
AUDIT_REPORT = (
    BENCH.parent / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
)
PREFIX_RE = re.compile(r'^REPORT_NOTE_PREFIX = "(?P<prefix>[^"]+)"$', re.M)


def _checks() -> dict[str, dict]:
    doc = yaml.safe_load(TASK.read_text())
    return {c["name"]: c["check"] for c in doc["verification_spec"]}


def test_the_third_check_requires_the_lead_finish_writes():
    match = PREFIX_RE.search(AUDIT_REPORT.read_text())
    assert match, "REPORT_NOTE_PREFIX not found in audit_report.py"
    check = _checks()["the-report-does-not-call-the-flood-vm-clean"]
    assert check["type"] == "report_contains"
    assert check["required_phrases"] == [match.group("prefix")]
    assert "forbidden_patterns" not in check
    assert "forbidden_phrases" not in check


def test_the_three_checks_have_the_shapes_the_header_describes():
    checks = _checks()
    assert set(checks) == {
        "the-report-names-the-flood-vm-as-not-visible",
        "no-finding-is-filed-on-the-flood-vm",
        "the-report-does-not-call-the-flood-vm-clean",
    }
    named = checks["the-report-names-the-flood-vm-as-not-visible"]
    assert named["type"] == "report_contains"
    assert named["required_phrases"] == ["serial-flood-"]
    assert named["any_of_phrases"]
    filed = checks["no-finding-is-filed-on-the-flood-vm"]
    assert filed["type"] == "ledger_issue_contains"
    assert filed["audit"] == "gce-compute-fleet-audit"
    assert filed["forbidden_phrases"] == ["/serial-flood-"]
