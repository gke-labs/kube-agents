#!/usr/bin/env python3
"""Build gate for the report-format contract.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_report_format.py``. The applier proves its anchors matched; this
proves the three places that now describe a report's shape still describe the
*same* shape.

That is the whole risk in this patch. The stanza appended to a card body, the
schema wording a worker reads, and the detector the notifier logs from are three
statements of one contract living in three files. Drift between them is silent:
a rule the notifier warns about but the stanza never mentions is a complaint
about an instruction nobody was given, and a rule the stanza asks for but
nothing measures is decoration. So the checks below drive the real stanza
through the real detector with the real cards that caused this.

The one thing this file must keep proving is a negative: shape is *never* a
reason to refuse a completion. Section 5 drives the worst-shaped card in the
suite through the real ``kanban_complete`` gate and asserts it is stored.

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
    "the bare-list card is only taste",
    krf.serious_defects(BARE_LIST_CARD) == (),
    "a card whose honest answer is three values would raise a WARNING for "
    "having no sentence manufactured for it",
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
# The check this file exists for. Each rule the notifier can raise a WARNING
# over has to be a rule the stanza states, in words a model can act on.
print("contract consistency:")
check(
    "the stanza obeys its own rules",
    krf.serious_defects(krf.REPORT_FORMAT_STANZA) == (),
    "the instructions would be warned about if a worker sent them back verbatim",
)
check(
    "the stanza forbids the H1 the notifier warns about",
    "Never `#`" in krf.REPORT_FORMAT_STANZA,
)
check(
    "the stanza forbids the ASCII structure the notifier warns about",
    "=== Title ===" in krf.REPORT_FORMAT_STANZA,
)
for defect in krf.SERIOUS_DEFECTS:
    check(
        f"the warning for {defect} names the edit to make",
        bool(krf.DEFECT_ADVICE.get(defect, "").strip()),
    )

# --- 5. Shape is not a reason to refuse a completion ------------------------
# A shape check lived in kanban_result_required from 2026-08-08 to 2026-08-11.
# It returned before kb.complete_task, so a refused report was gone, while the
# retry skipped the check and stored whatever came back — including the same
# badly-shaped copy. It could lose a complete report but could not guarantee a
# well-formed one. These checks are what stops it coming back.
print("kanban_complete takes the report whatever it looks like:")
import tools.kanban_result_required as krr  # noqa: E402

krr._refused_at.clear()
err, out = krr.require_result("t_c781d6b0", "Slept 1ms.", GOOD_CARD)
check("a well-shaped report is not touched", err is None and out == GOOD_CARD)

krr._refused_at.clear()
err, out = krr.require_result("t_88cdceb1", "Slept 1ms.", H1_CARD)
check(
    "the H1 report is stored on its first attempt",
    err is None and out == H1_CARD,
    "the report that used to be discarded is discarded again",
)
check(
    "a mis-shaped report does not spend the empty-result nudge",
    krr._refused_at == {},
    "shape is not a refusal, so it must not consume the one nudge that exists "
    "for a card that sent no result at all",
)
check(
    "the H1 card really is the worst case being waved through",
    "top-level-heading" in krf.serious_defects(H1_CARD),
    "the check above passes vacuously if the detector stops seeing this card",
)

krr._refused_at.clear()
ascii_card = "=== Timing Details ===\n\n" + "\n".join(
    f"{i}. STEP {i} completed in 0.00{i} seconds" for i in range(1, 9)
)
check(
    "the ASCII-structured card is serious enough to have been refused before",
    "ascii-substitute" in krf.serious_defects(ascii_card),
)
err, out = krr.require_result("t_ascii", "Done.", ascii_card)
check("the ASCII-structured report is stored too", err is None and out == ascii_card)

krr._refused_at.clear()
krr.require_result("t_shared", "a status line", None)
err4, out4 = krr.require_result("t_shared", "a status line", H1_CARD)
check(
    "the nudged retry's report is taken as it stands",
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

_, ugly_stored = krr.require_result("t_seam_ugly", "Slept 1ms.", H1_CARD)
ugly_delivered = result_block("\nSlept 1ms.", ugly_stored)
check(
    "an ugly report reaches the message too",
    "Sleep 1ms - Task 2 Completion Report" in ugly_delivered,
    "this is the whole point of the change: delivered ugly beats not delivered",
)
check(
    "the notifier can see a short report",
    krf.result_shape_defects(BARE_LIST_CARD) != (),
    "the delivery-path log is blind to exactly the reports it exists for",
)
krr._refused_at.clear()

# --- Result -----------------------------------------------------------------
if FAILURES:
    print(f"\nverify_kanban_report_format: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nverify_kanban_report_format: all checks passed")
