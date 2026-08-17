#!/usr/bin/env python3
"""Build gate for the kanban result capture patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_result_required.py``. The applier only proves its anchors
matched; this exercises the patched code in the image the way a worker actually
reaches it.

It is deliberately behavioural rather than textual. The regression that
motivated the whole change was not a failed edit — it was a field the model was
told not to use, so the check that matters is "does a worker get refused when it
drops the answer".

The other half of that question — "does the answer then reach the message" —
belongs to ``gateway/kanban_notifier.py`` and is gated by
``verify_kanban_notifier.py``, which runs earlier in the build. What stays here
is the seam between the two: the incident replay below drives a real refusal
through the real gate and then hands what the gate stored to the real delivery
path, which is the only assertion in either file that neither patch can make on
its own.

Usage::

    cd /opt/hermes && python3 verify_kanban_result.py
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


# --- 1. The tool schema a worker actually reads -----------------------------
print("kanban_complete schema:")
from tools.kanban_tools import KANBAN_COMPLETE_SCHEMA as SCHEMA  # noqa: E402

props = SCHEMA["parameters"]["properties"]
blob = repr(SCHEMA)

check("result is a required parameter", "result" in SCHEMA["parameters"]["required"])
check(
    "result is described as REQUIRED",
    "REQUIRED" in props["result"]["description"],
)
check(
    "result no longer described as a legacy log line",
    "legacy field" not in blob,
    "upstream's discouraging wording survived the patch",
)
check(
    "summary no longer invites a 1-3 sentence report",
    "1-3 sentence" not in blob,
)
check(
    "summary states the real 400-character single-line cut",
    "400" in props["summary"]["description"]
    and "FIRST LINE" in props["summary"]["description"],
)
check(
    "the tool description's tail is preserved",
    "created_cards" in SCHEMA["description"] and "artifacts" in SCHEMA["description"],
    "the description was replaced wholesale instead of having its head swapped",
)

# --- 2. The gate ------------------------------------------------------------
print("require_result gate:")
import tools.kanban_result_required as krr  # noqa: E402

krr._refused_at.clear()
err, out = krr.require_result("t_verify", "a status line", None)
check("a completion with no result is refused", err is not None)
check("the refusal names the field", err and "result" in err)
err2, out2 = krr.require_result("t_verify", "a status line", "the answer")
check("the retry carrying a result is accepted", err2 is None and out2 == "the answer")
check(
    "the answered refusal is forgotten",
    krr._refused_at == {},
    "the gate keeps a ledger of every card it ever refused, so a card reopened "
    "and retried in this process would be closed empty in silence",
)
err2b, _ = krr.require_result("t_verify", "a status line", None)
check("a card reopened after a refusal is nudged again", err2b is not None)

krr._refused_at.clear()
krr.require_result("t_wedge", "a status line", None)
err3, out3 = krr.require_result("t_wedge", "a status line", None)
check(
    "a card is never wedged shut by the gate",
    err3 is None,
    "a second empty completion was still refused",
)
check("summary is promoted so the card carries something", out3 == "a status line")

krr._refused_at.clear()
err4, _ = krr.require_result("t_ok", "s", "  \n ")
check("a whitespace-only result counts as empty", err4 is not None)

# Blank is the only refusal. A shape check here from 2026-08-08 to 2026-08-11
# returned before kb.complete_task, so a report it refused was gone; the retry
# skipped the check and stored the same ugly copy anyway. It could lose a
# complete report but could not guarantee a well-formed one.
krr._refused_at.clear()
ugly = "# Task 2 Completion Report\n\n" + "\n".join(
    f"- Start Epoch: 178624053{i}.688288{i}" for i in range(1, 6)
)
err5, out5 = krr.require_result("t_shape", "a status line", ugly)
check(
    "a mis-shaped result is delivered rather than refused",
    err5 is None and out5 == ugly,
    "the gate is discarding complete reports over formatting again",
)
check(
    "shape does not spend the nudge that exists for an empty result",
    krr._refused_at == {},
)

krr._refused_at.clear()
krr.require_result("t_blank", None, "  \n ")
_, blank_out = krr.require_result("t_blank", None, "  \n ")
check(
    "a blank result is never handed back as the value to store",
    blank_out is None,
    "complete_task indexes line zero of the stripped value inside write_txn, so "
    "whitespace rolls the completion back with IndexError and the card, whose "
    "nudge is already spent, fails the same way on every retry",
)
krr._refused_at.clear()

with open("tools/kanban_tools.py", encoding="utf-8") as _kanban_tools_file:
    _kanban_tools_src = _kanban_tools_file.read()
check(
    "the gate is wired into the completion handler",
    "_require_result(tid, summary, result)" in _kanban_tools_src,
)
check(
    "the handler's own summary is folded before it reaches complete_task",
    "summary = _blank_to_none(summary)" in _kanban_tools_src,
    "a whitespace-only summary still wedges the card however good the result is",
)

# --- 3. The incident, replayed ----------------------------------------------
# The seam between the two patches, and the only thing here that needs both:
# what the gate stores on the retry is what the notifier has to deliver. Every
# other delivery assertion lives in verify_kanban_notifier.py.
print("2026-08-07 incident replay:")
from gateway.kanban_notifier import result_block  # noqa: E402

catalogue = "\n".join(f"{i}. cron-job-{i} — 0 {i} * * *" for i in range(1, 10))

krr._refused_at.clear()
incident_summary = (
    "Successfully inspected and cataloged all 9 active platform-agent-level and "
    "system-wide cron jobs. Compiled their detailed purposes, schedules, and "
    "active configurations."
)
err, _ = krr.require_result("t_8d1cf5cf", incident_summary, None)
check("the completion that lost the catalogue is refused", err is not None)
_, stored = krr.require_result("t_8d1cf5cf", incident_summary, catalogue)

delivered = result_block("\n" + incident_summary, stored)
check("the retry's catalogue reaches the message", "cron-job-1" in delivered)
check("every job in the catalogue is delivered", all(f"cron-job-{i}" in delivered for i in range(1, 10)))
krr._refused_at.clear()

# --- Result -----------------------------------------------------------------
if FAILURES:
    print(f"\nverify_kanban_result: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nverify_kanban_result: all checks passed")
