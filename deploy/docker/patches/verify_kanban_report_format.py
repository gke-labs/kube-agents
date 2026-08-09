#!/usr/bin/env python3
"""Build gate for the report-format contract.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_report_format.py``. The applier proves its anchors matched; this
proves the three places that now describe a report's shape still describe the
*same* shape.

That is the whole risk in this patch. The stanza appended to a card body, the
detector the notifier logs from, and the gate in ``kanban_complete`` are three
statements of one contract living in three files. Drift between them is silent
and expensive in both directions: a rule the gate enforces but the stanza never
mentions refuses honest work, and a rule the stanza asks for but nothing checks
is decoration. So the checks below drive the real stanza through the real
detector, and the real gate with the real cards that caused this.

Usage::

    cd /opt/hermes && python3 verify_kanban_report_format.py
"""

from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


# The 2026-08-08 fan-out, verbatim off the board. One was read as fine and one
# as wrong; a detector that cannot tell them apart is not worth shipping.
GOOD_CARD = """### Sleep Task 1 Completion

The requested sleep of 1 millisecond has been executed. Here are the recorded \
active execution details:

- **Start Unix Epoch:** `1786240527.916398`
- **End Unix Epoch:** `1786240527.9178874`
- **Elapsed Active Execution Time:** `0.001489400863647461` seconds"""

H1_CARD = """# Sleep 1ms - Task 2 Completion Report

The 1ms sleep task was successfully completed.

### Timing Details
- **Start Epoch:** 1786240530.6882887
- **End Epoch:** 1786240530.6903057
- **Measured Active Duration:** 0.002017 seconds (2.017 ms)"""

BARE_LIST_CARD = """### Sleep Task 3 Execution Details
- **Active Start (Unix Epoch):** 1786240531.1585038
- **Active End (Unix Epoch):** 1786240531.1598377
- **Active Duration:** 0.0013339519500732422 seconds"""

# --- 1. The module is importable from where the runtime expects it ----------
print("tools.kanban_report_format:")
import tools.kanban_report_format as krf  # noqa: E402

check("the stanza is non-empty", bool(krf.REPORT_FORMAT_STANZA.strip()))
check(
    "the stanza carries its own marker",
    krf.FORMAT_MARKER in krf.REPORT_FORMAT_STANZA,
    "with_report_format would append a second copy on every pass",
)

# --- 2. The card body a worker is handed ------------------------------------
print("kanban_create body:")
import tools.kanban_tools as kt  # noqa: E402

check(
    "the handler is wired to the stanza",
    getattr(kt, "_with_report_format", None) is krf.with_report_format,
    "the import landed on a different function than the module exports",
)
_kanban_tools_src = open("tools/kanban_tools.py").read()
check(
    "the create handler appends the stanza",
    "body = _with_report_format(body)" in _kanban_tools_src,
)

plain = "Sleep for 1ms and report the epochs."
once = krf.with_report_format(plain)
check("a plain body gains the stanza", krf.FORMAT_MARKER in once)
check("the original brief survives", plain in once)
check(
    "appending twice appends once",
    krf.with_report_format(once) == once,
    "a card re-created or updated would accumulate copies of the stanza",
)
explicit = "Do X.\n\nReport format: a single pipe table, no prose."
check(
    "an explicit format directive is left alone",
    krf.with_report_format(explicit) == explicit,
    "the worker would be handed two briefs to reconcile",
)
check(
    "a card with no body at all still gets the shape",
    krf.with_report_format(None) == krf.REPORT_FORMAT_STANZA,
)

# --- 3. The detector agrees with the human who read the thread --------------
print("shape detector:")
check(
    "the card that read as fine is clean",
    krf.result_shape_defects(GOOD_CARD) == (),
    f"flagged {krf.result_shape_defects(GOOD_CARD)}",
)
check(
    "the H1 card is caught",
    "top-level-heading" in krf.result_shape_defects(H1_CARD),
)
check(
    "the bare-list card is seen",
    "heading-without-prose" in krf.result_shape_defects(BARE_LIST_CARD),
)
check(
    "the bare-list card is not refused",
    krf.gate_defects(BARE_LIST_CARD) == (),
    "a card whose honest answer is three values would pay a round trip to have "
    "a sentence manufactured for it",
)
check(
    "the floor is low enough to see the cards that caused this",
    krf.SHAPE_MIN_CHARS <= min(len(H1_CARD), len(BARE_LIST_CARD)),
    f"floor {krf.SHAPE_MIN_CHARS} hides a {len(BARE_LIST_CARD)}-char report",
)
check(
    "a one-line report has no shape to get wrong",
    krf.result_shape_defects("3/3 pods ready") == (),
)

# --- 4. The three statements of the contract agree --------------------------
# The check this file exists for. Each rule the gate can refuse a card over has
# to be a rule the stanza states, in words a model can act on.
print("contract consistency:")
check(
    "the stanza obeys its own rules",
    krf.gate_defects(krf.REPORT_FORMAT_STANZA) == (),
    "the instructions would be refused if a worker sent them back verbatim",
)
check(
    "the stanza forbids the H1 the gate refuses",
    "Never `#`" in krf.REPORT_FORMAT_STANZA,
)
check(
    "the stanza forbids the ASCII structure the gate refuses",
    "=== Title ===" in krf.REPORT_FORMAT_STANZA,
)
for defect in krf.GATED_DEFECTS:
    check(
        f"the refusal for {defect} names the edit to make",
        bool(krf.DEFECT_ADVICE.get(defect, "").strip()),
    )

# --- 5. The gate, driven for real -------------------------------------------
print("kanban_complete shape gate:")
import tools.kanban_result_required as krr  # noqa: E402

krr._refused_at.clear()
err, out = krr.require_result("t_c781d6b0", "Slept 1ms.", GOOD_CARD)
check("a well-shaped report is not touched", err is None and out == GOOD_CARD)

krr._refused_at.clear()
err, out = krr.require_result("t_88cdceb1", "Slept 1ms.", H1_CARD)
check("the H1 report is refused once", err is not None and out is None)
check("the refusal names the replacement heading", err and "`##`" in err)
fixed = H1_CARD.replace("# Sleep 1ms", "## Sleep 1ms", 1)
err2, out2 = krr.require_result("t_88cdceb1", "Slept 1ms.", fixed)
check("the reformatted retry is accepted", err2 is None and out2 == fixed)

krr._refused_at.clear()
krr.require_result("t_wedge", "a status line", H1_CARD)
err3, out3 = krr.require_result("t_wedge", "a status line", H1_CARD)
check(
    "a card is never wedged shut over formatting",
    err3 is None and out3 == H1_CARD,
    "one ugly report reaching chat beats a card stuck open forever",
)

krr._refused_at.clear()
krr.require_result("t_shared", "a status line", None)
err4, out4 = krr.require_result("t_shared", "a status line", H1_CARD)
check(
    "the empty gate and the shape gate share one nudge",
    err4 is None and out4 == H1_CARD,
    "a card refused for having no result, answered with a report that is "
    "present but ugly, would be refused a second time",
)
krr._refused_at.clear()

# --- 6. The seam: what the gate stores is what chat receives ----------------
print("delivery:")
from gateway.kanban_notifier import result_block  # noqa: E402

krr._refused_at.clear()
_, stored = krr.require_result("t_seam", "Slept 1ms.", GOOD_CARD)
delivered = result_block("\nSlept 1ms.", stored)
check("the report reaches the message", "Sleep Task 1 Completion" in delivered)
check("its Markdown reaches the message intact", "### " in delivered)
check(
    "the notifier can see a short report",
    krf.result_shape_defects(BARE_LIST_CARD) != (),
    "the delivery-path warning is blind to exactly the reports it exists for",
)
krr._refused_at.clear()

# --- Result -----------------------------------------------------------------
if FAILURES:
    print(f"\nverify_kanban_report_format: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nverify_kanban_report_format: all checks passed")
