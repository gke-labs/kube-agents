"""compute-class-untargeted-pending-probe reads two verdict lines, not prose.

The objective passes when one line is the missing-class cause, another is the
version-scope verdict, and no line is a wrong verdict. A verdict counts only
when nothing but punctuation follows it on its line. ``report_contains``
lowercases the report and drops emphasis and backticks before the patterns run,
so a bolded, quoted or bulleted verdict line counts, and a verdict named inside
a sentence does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "compute-class-untargeted-pending-probe" / "task.yaml"
CHECK = "the-cause-is-the-missing-class-and-the-version-scope-is-stated"
TIMEOUT_SEC = 5.0

CAUSE = "CAUSE: PODS-NAME-NO-CLASS"
SCOPE = "AFTER-UPGRADE: SCALE-UP-WAS-SEEN"
WRONG_CAUSE = "CAUSE: STOCKOUT"
WRONG_SCOPES = ("AFTER-UPGRADE: STILL-PENDING-EXPECTED", "AFTER-UPGRADE: NO-RECORD")


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


def test_both_verdict_lines_pass():
    for report in (
        f"{CAUSE}\n{SCOPE}\n\nAdd the compute-class nodeSelector to ingest.",
        f"**{CAUSE}**\n**{SCOPE}**\n\nNo scale-up was tried.",
        f"`{CAUSE}`\n`{SCOPE}`\n",
        f"> {CAUSE}\n> {SCOPE}\n",
        f"1. {CAUSE}\n2. {SCOPE}\n",
        f"Here is the answer.\n{CAUSE}\n{SCOPE}\n",
        f"{CAUSE}.\n{SCOPE}.\n",
        # A sentence that opens with a rejected verdict is prose, not a verdict.
        f"{CAUSE}\n{SCOPE}\n\n- {WRONG_CAUSE} is ruled out: no scale-up was tried.\n",
        f"{CAUSE}\n{SCOPE}\n\n{WRONG_SCOPES[0]} would hold only if 1.36.4 behaved as 1.35.8 does.\n",
    ):
        assert _passes(report), report


def test_a_wrong_missing_or_prose_verdict_fails():
    for report in (
        f"{WRONG_CAUSE}\n{SCOPE}\n",
        f"{CAUSE}\n{WRONG_SCOPES[0]}\n",
        f"{CAUSE}\n{WRONG_SCOPES[1]}\n",
        f"{CAUSE}\n",
        f"{SCOPE}\n",
        f"{CAUSE}\n{SCOPE}\n{WRONG_CAUSE}\n",
        f"{CAUSE}\n{SCOPE}\n2. **{WRONG_SCOPES[0]}**\n",
        # A hedged verdict line is not a verdict.
        f"{CAUSE} or STOCKOUT\n{SCOPE}\n",
        f"{CAUSE}\n{SCOPE} (but STILL-PENDING-EXPECTED until tested)\n",
        f"{CAUSE} is wrong; it is a stockout\n{SCOPE}\n",
        f"It is not {WRONG_CAUSE}; the pods name no class, and the scale-up was seen on that version.",
        "The cause is that the pods name no class. After the upgrade a scale-up was seen.",
    ):
        assert not _passes(report), report
