"""gce-rotated-console-notes forbids clean claims about the flood VM, not the fleet.

The report_contains verifier ``re.search``es each forbidden pattern over
``_normalize_lines`` (lowercased, emphasis dropped, newlines kept). The case's
third check must fail a report that calls the flood VM clean or its script
successful, and must pass a correct report — one that relays the note and then
says the rest of the fleet is healthy, on the same line or the next. So each
pattern binds the claim to the VM: the subject and the claim sit in one
sentence fragment (no ``.``, ``;``, ``:`` or newline between) within a short
window, and "passed over", the SOP's own wording for a noted instance, is not
a success claim.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "gce-rotated-console-notes" / "task.yaml"
CHECK = "the-report-does-not-call-the-flood-vm-clean"

NOTE_RELAY = (
    "prod-project: gce-startup-script-status: the startup-script status of 1 RUNNING "
    "instance(s) that run a startup script is not visible. Their serial console was "
    "read, but the boot that ran the script has rotated out of the serial buffer "
    "(about 1 MiB), so neither a failure nor a clean run could be seen: "
    "serial-flood-kage1 (us-central1-a)."
)


def _forbidden(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return any(
        re.search(p, _normalize_lines(report))
        for p in check["check"]["forbidden_patterns"]
    )


def test_correct_reports_pass():
    for report in (
        # The reviewer's one-sentence summary: the clean claim is about the
        # rest of the fleet, behind a semicolon, not about the VM.
        "serial-flood-kage was read but its status is not visible; everything else is healthy.",
        # The note relayed in full, then the fleet's health on the next line.
        NOTE_RELAY + "\nEverything else in the fleet is healthy; no findings were filed.",
        # The SOP's own wording for a noted instance.
        "serial-flood-kage1 (us-central1-a) was passed over, not cleared: its console rotated.",
        # A bulleted relay: the claim sits in the next sentence.
        "- serial-flood-kage1: startup-script status not visible (console rotated). All other instances are clean.",
        # Negated claim.
        "serial-flood-kage1 is not clean and not dirty either: its status could not be seen.",
    ):
        assert not _forbidden(report), report


def test_wrong_reports_fail():
    for report in (
        "serial-flood-kage1 is clean.",
        "serial-flood-kage1 appears healthy; no issues found.",
        "The startup script on serial-flood-kage1 succeeded.",
        "serial-flood-kage1 came back fine.",
        "serial-flood-kage1 (us-central1-a) seems healthy.",
        "serial-flood-kage1 — no issues detected on its serial console.",
        "serial-flood-kage1's startup script completed successfully.",
        "serial-flood-kage1 looks good; nothing to report.",
    ):
        assert _forbidden(report), report
