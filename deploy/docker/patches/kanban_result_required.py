"""Make ``result`` the single field that carries a completed card's answer.

Installed into the image at ``/opt/hermes/tools/kanban_result_required.py`` and
wired into ``tools/kanban_tools.py`` by
``deploy/docker/patches/apply_kanban_result_required.py``.

The companion patch ``gateway/kanban_notifier.py`` posts ``result`` into the
chat thread. This one makes sure there is something in it to post.

Why
---
On 2026-08-07 a user asked which platform cron jobs were enabled. Three cards
ran (``t_8d1cf5cf``, ``t_97f721b6``, ``t_68fbd0b6``); all three closed ``done``;
the user never got the list. Dumping the board afterwards showed
``tasks.result IS NULL`` and ``result_len: 0`` on every one of them. The workers
had built the catalogue — it is still sitting in
``/opt/data/kanban/logs/t_8d1cf5cf.log``, 5.5 KB of it — printed it to their own
stdout, and then dropped it on the floor at the ``kanban_complete`` call.

They were doing what they were told. Upstream's own tool schema describes
``result`` as::

    Short result log line (legacy field, maps to task.result). Use ``summary``
    instead when possible; this exists for compatibility with callers that
    still set --result on the CLI.

and ``summary`` as "Human-readable handoff, 1-3 sentences", while the gate below
it accepts ``summary or result``. So the model is told the only field that can
carry a report is legacy, told to prefer a field documented as 1-3 sentences,
and then allowed to close the card with just that. A worker holding a 5.5 KB
answer follows the schema and throws it away.

Worse, ``summary`` cannot carry a report even if a worker tries: the kernel
writes the completion event as (``hermes_cli/kanban_db.py``)::

    ev_summary = (summary if summary is not None else result) or ""
    ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""

First line only, 400 characters, no ellipsis. The chat notifier reads that field
and nothing else, so a multi-line summary loses everything after line one
silently.

What this changes
-----------------
Two things, both deterministic — there is no attempt to classify a card as
"asked for information" or not. Every card has an answer, even if the answer is
one line ("Restarted the deployment; 3/3 pods ready"), so every card is
required to carry one.

1. **Schema wording.** ``result`` becomes the required field that carries the
   deliverable and says it is posted to the requester verbatim; ``summary``
   becomes an explicitly one-line, 400-character status header. A prompt that
   prevents the mistake is worth more than a gate that rejects it.

2. **The gate.** Upstream's ``if not (summary or result)`` becomes a check that
   ``result`` carries something.

3. **No shape check.** One lived here from 2026-08-08 to 2026-08-11: a
   ``result`` that opened sections with ``#``, or marked them with
   ``=== Title ===`` and no real Markdown, was refused with the edit to make.
   It was removed because it could lose a report, and because measuring it
   showed it was not buying enough to be worth that.

   The refusal returned before ``kb.complete_task``, so a refused completion
   wrote nothing at all: the card stayed open, no result was stored, and the
   only copy of the report was in the worker's context. Usually the worker
   reformats and the retry lands. When the run ended in between — a turn limit,
   a cancellation, a cron child exiting — the report was gone and the card was
   left open with nothing on it.

   Set against that, what the check enforced was one nudge, not a shape. The
   retry skipped it entirely, so a worker that resubmitted the same badly
   formatted text unchanged was accepted. It could cost a complete report; it
   could not promise a well-formed one. For a stack whose whole premise is that
   the report reaching the requester's thread *is* the deliverable, that trade
   does not survive.

   The contract is still stated where stating it is free: the report-format
   stanza stapled to the card body at ``kanban_create``, and the schema wording
   in point 1 — a prompt that prevents the mistake is worth more than a gate
   that rejects it. ``tools/kanban_report_format.py`` still owns the defect
   list, and ``gateway/kanban_notifier.py`` records the shape it delivered so a
   badly formatted report is visible afterwards rather than refused in advance.

Never wedging the card
----------------------
A gate that can refuse forever is a worse bug than the one it fixes, so a
refusal is never repeated back to back. The first content-free completion is
rejected with an instruction; the retry is accepted even when it is content-free
again, promoting ``summary`` into ``result`` so the card still carries the best
text available. One nudge is what a model needs to correct a tool call, and the
card reaches ``done`` on its second attempt either way.

Remembering the refusal, and forgetting it
------------------------------------------
"The retry" only means anything if the handler remembers refusing this card, and
that memory has to expire. It first did not: ``_nudged`` was a module-level
``set`` that only ever grew, so a task id in it meant "accept this completion,
whatever it contains" for the life of the process.

On the worker path that was harmless by accident. ``dispatch_in_gateway`` moves
the *dispatcher loop* into the gateway, not the worker —
``gateway/kanban_watchers.py``'s ``_kanban_dispatcher_watcher`` calls
``kanban_db.dispatch_once`` with no ``spawn_fn``, so ``_default_spawn`` still
``subprocess.Popen``s ``hermes -p <profile> chat -q "work kanban task t_…"``.
One process per dispatch means the set holds one id and dies with the attempt
that created it.

It was not harmless in the gateway. ``toolsets: [kanban]`` is set on the
``platform`` profile and on the pod's root config, and
``_enforce_worker_task_ownership`` deliberately exempts a caller with no
``HERMES_KANBAN_TASK`` in its environment, because an orchestrator "sometimes
legitimately close[s] out child tasks". So a gateway session can complete any
card from a process that lives as long as the pod, and the first content-free
completion of a card spent that card's nudge permanently: every later attempt,
including one made hours afterwards on a card that had been reopened and
retried, was accepted in silence. That is the 2026-08-07 incident, reintroduced
by the code written to fix it.

The memory is therefore ``_refused_at``, task id to ``time.monotonic()``, read
by popping: a refusal older than ``NUDGE_TTL_SECONDS`` belongs to a different
attempt and the new attempt gets its own nudge. Expiry does not reopen the
wedge, because any caller that retries promptly — the only kind that ever
completes anything — is still inside the window on its second call.

Blank is empty, all the way down
--------------------------------
``result="   "`` used to reach the board. The gate reads it as empty, but the
promotion branch then handed the whitespace back as the value to store, and the
two lines quoted above run inside ``complete_task``'s ``write_txn``. ``"   "`` is
truthy, ``"   ".strip().splitlines()`` is ``[]``, and ``[0]`` raises
``IndexError`` with the ``UPDATE … SET status = 'done'`` already executed. The
transaction rolls back, the handler's blanket ``except Exception`` turns the
failure into ``kanban_complete: list index out of range``, and the card is still
``running`` — with its nudge already spent, so the identical retry takes the
identical path. That card never closes and its worker is never told why.

The expression reads ``summary`` first, so a blank ``summary`` wedges a card the
same way however good its ``result`` is; upstream's ``if not (summary or
result)`` never caught that either. Both fields are folded to ``None`` by
:func:`blank_to_none` before they leave here — ``None`` is the value
``complete_task`` is written to handle, and a field holding only whitespace is
not carrying anything anyway.

Scope: the gate lives in the ``kanban_complete`` tool handler. The CLI and the
scheduler write through ``kb.complete_task`` directly and are unaffected, so no
cron run and no human at a shell can be blocked by it. Every caller that does go
through the handler — a dispatched worker closing its own card, an orchestrator
gateway session closing somebody else's — is gated, which is why the refusal
memory has to be correct for a process that outlives a single card.
"""

from __future__ import annotations

import time

#: How long a refusal stays on file. A model corrects a rejected tool call in its
#: next assistant turn, seconds later, so a content-free completion arriving
#: after this long is a fresh attempt at the card and has earned its own nudge.
#: 15 minutes is ``DEFAULT_CLAIM_TTL_SECONDS`` in ``hermes_cli/kanban_db.py``,
#: the soonest an abandoned claim can be handed to another worker, which makes it
#: the shortest window that cannot straddle two attempts on the worker path.
NUDGE_TTL_SECONDS = 15 * 60

#: Cards refused once and not yet completed, against the ``time.monotonic()``
#: reading taken at the refusal. Keyed by task id rather than held as a bare flag
#: because a worker process is nominally one card, but nothing in the tool
#: contract promises that and a shared process must not spend card B's nudge on
#: card A. Entries are popped by the completion that answers them and go stale on
#: their own; the only ones that linger belong to a caller that walked away, a
#: handful of floats in a gateway that runs for weeks.
_refused_at: dict[str, float] = {}

MISSING_RESULT_ERROR = (
    "result is required and was empty. `result` is what the person who asked "
    "actually receives — the gateway posts it into their chat thread verbatim. "
    "Call kanban_complete again with the full answer to this card in `result`: "
    "the list, the report, the findings, the numbers, whatever was asked for, "
    "complete and not summarised away. Do not leave it only in your transcript, "
    "in a file, or in a comment — none of those reach the user. Keep `summary` "
    "as the one-line status header."
)


def blank_to_none(value: object) -> object:
    """``None`` unless ``value`` carries printable text, otherwise ``value``.

    A field holding only whitespace carries nothing, but it is truthy, which is
    how ``"   "`` slips past an emptiness check and into the
    ``.strip().splitlines()[0]`` in ``complete_task`` that raises ``IndexError``
    on it. Values with content come back untouched: ``kanban_complete`` has
    already run them through ``redact_sensitive_text`` and this is not the place
    to trim what that produced.
    """
    if value is None or not str(value).strip():
        return None
    return value


def require_result(
    task_id: object,
    summary: object,
    result: object,
) -> tuple[str | None, object]:
    """Validate a completion's ``result``.

    Returns ``(error, result_to_store)``. ``error`` is ``None`` when the
    completion may proceed; otherwise it is the text to hand back to the worker
    and the completion must not be written.

    Exactly one thing is refused: a blank ``result``. There is no length,
    quality or formatting floor. A card whose honest answer is one line must be
    able to close, and a report that renders badly still answers the question —
    only an absent one answers nothing. See the module docstring for the shape
    check that used to be here and why it went.

    The refusal happens at most once per :data:`NUDGE_TTL_SECONDS`: the retry is
    accepted even when it is blank again, with ``summary`` promoted into
    ``result`` so a worker that will not fill the field cannot wedge the card
    shut.

    ``result_to_store`` is never a blank string; see :func:`blank_to_none` for
    what that would cost.
    """
    summary = blank_to_none(summary)
    result = blank_to_none(result)
    key = str(task_id)
    # Popped rather than read. Whether the refusal is being answered or has gone
    # stale, this call ends it, and popping is what keeps the mapping to the
    # cards actually awaiting a retry.
    refused_at = _refused_at.pop(key, None)
    nudge_spent = (
        refused_at is not None
        and time.monotonic() - refused_at <= NUDGE_TTL_SECONDS
    )

    if result is None:
        if nudge_spent:
            # The retry, and it is empty again. Take what we can get rather than
            # hold the card open. ``summary`` may be ``None`` too, in which case
            # the card closes with no result exactly as upstream would have
            # allowed.
            return None, summary
        _refused_at[key] = time.monotonic()
        return MISSING_RESULT_ERROR, None

    # Shape is never refused. A report that renders badly still answers the
    # question; one that was thrown away answers nothing. See the module
    # docstring for the measurement that settled it.
    return None, result


# --- Schema wording ---------------------------------------------------------
# Applied to the live ``KANBAN_COMPLETE_SCHEMA`` dict at import time rather than
# by rewriting the string literals in place. Upstream builds each description
# from adjacent string literals wrapped at some arbitrary column, so a textual
# anchor would break on a reflow that changed nothing semantic. Matching against
# the assembled value is stable under rewrapping and still fails loudly when the
# wording itself changes.
#
# Each OLD string is an exact copy of upstream's assembled text; ``apply_schema``
# raises if one stops matching, which is the signal to re-derive it against the
# new base image.

OLD_TOOL_DESCRIPTION = (
    "Mark your current task done with a structured handoff for "
    "downstream workers and humans. Prefer ``summary`` for a "
    "human-readable 1-3 sentence description of what you did; put "
    "machine-readable facts in ``metadata`` (changed_files, "
    "tests_run, decisions, findings, etc). At least one of "
    "``summary`` or ``result`` is required. "
)

NEW_TOOL_DESCRIPTION = (
    "Mark your current task done. ``result`` is REQUIRED and carries the "
    "answer: whatever this card asked for goes there in full, and the "
    "gateway posts it verbatim into the chat thread of the person who "
    "asked. ``summary`` is a one-line status header, NOT the report — only "
    "its first line survives and only the first 400 characters of that, so "
    "anything you put there and nowhere else is lost. Put machine-readable "
    "facts in ``metadata`` (changed_files, tests_run, decisions, findings, "
    "etc). "
)

OLD_SUMMARY_DESCRIPTION = (
    "Human-readable handoff, 1-3 sentences. Appears in "
    "Run History on the dashboard and in downstream "
    "workers' context."
)

NEW_SUMMARY_DESCRIPTION = (
    "One-line status header: what happened, in a single "
    "sentence. Only the FIRST LINE reaches chat, and only "
    "its first 400 characters — the kernel cuts the rest "
    "with no ellipsis. Never put the deliverable here; it "
    "goes in ``result``. Appears in Run History on the "
    "dashboard and in downstream workers' context."
)

OLD_RESULT_DESCRIPTION = (
    "Short result log line (legacy field, maps to "
    "task.result). Use ``summary`` instead when "
    "possible; this exists for compatibility with "
    "callers that still set --result on the CLI."
)

NEW_RESULT_DESCRIPTION = (
    "REQUIRED. The complete answer to this card — the "
    "list, the report, the findings, the RCA, the "
    "numbers. Not truncated and not reduced to one line: "
    "the gateway posts this verbatim into the chat thread "
    "of the person who asked, so it is the only thing they "
    "actually receive. If the card asked a question, the "
    "whole answer goes here. Do not summarise it away, and "
    "do not leave it only in your transcript, in a file, or "
    "in a comment — none of those reach the user. Write it "
    "as standard Markdown: `##` for sections and never `#` "
    "(the chat message already shows the card title), "
    "`[text](url)` for every artifact you name, backticks "
    "around raw values like ids, paths and timestamps, and a "
    "pipe table for tabular data — but keep the table to a few "
    "short columns and never let it be the only place a fact "
    "lives, because Google Chat drops tables."
)

OLD_GATE = (
    "    if not (summary or result):\n"
    "        return tool_error(\n"
    "            \"provide at least one of: summary (preferred), result\"\n"
    "        )\n"
)

# ``summary`` is folded separately because ``require_result`` only owns
# ``result``: the handler passes its own ``summary`` local straight on to
# ``kb.complete_task``, and a blank one raises ``IndexError`` there whatever the
# result holds. Rebinding it here rather than widening the gate's return keeps
# the ``_require_result(tid, summary, result)`` call verbatim, which is the
# string the Dockerfile greps for.
NEW_GATE = (
    "    # kube-agents patch: see tools/kanban_result_required.py\n"
    "    summary = _blank_to_none(summary)\n"
    "    _result_err, result = _require_result(tid, summary, result)\n"
    "    if _result_err:\n"
    "        return tool_error(_result_err)\n"
)


def _swap(schema: dict, path: tuple[str, ...], old: str, new: str) -> None:
    """Replace ``old`` with ``new`` at ``path`` in ``schema``, or raise.

    ``old`` is matched as a substring so the tool description — whose tail
    documents ``created_cards`` and ``artifacts`` and is left alone — can have
    its opening paragraph swapped without restating the rest.
    """
    missing = KeyError(
        f"kanban_result_required: {'.'.join(path)} missing from the "
        f"kanban_complete schema. Upstream Hermes changed — re-derive the "
        f"schema patch before bumping the base image."
    )
    node: object = schema
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            raise missing
        node = node[key]
    leaf = path[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise missing
    current = node[leaf]
    if not isinstance(current, str) or old not in current:
        raise ValueError(
            f"kanban_result_required: {'.'.join(path)} does not contain the "
            f"expected upstream wording. Upstream Hermes changed — re-derive "
            f"the schema patch before bumping the base image.\n"
            f"--- expected to find ---\n{old}\n--- actual ---\n{current!r}"
        )
    node[leaf] = current.replace(old, new)


def apply_schema(schema: dict) -> dict:
    """Rewrite ``kanban_complete``'s schema in place and return it.

    Called from ``tools/kanban_tools.py`` immediately before the tool is
    registered, so the corrected wording is what the registry sees regardless of
    whether ``registry.register`` stores the dict by reference or copies it.
    """
    _swap(schema, ("description",), OLD_TOOL_DESCRIPTION, NEW_TOOL_DESCRIPTION)
    props = ("parameters", "properties")
    _swap(
        schema, props + ("summary", "description"),
        OLD_SUMMARY_DESCRIPTION, NEW_SUMMARY_DESCRIPTION,
    )
    _swap(
        schema, props + ("result", "description"),
        OLD_RESULT_DESCRIPTION, NEW_RESULT_DESCRIPTION,
    )
    # Advertise the requirement in the place a model is most likely to honour
    # it. The handler enforces it either way; this stops a well-behaved caller
    # from having to be corrected at all.
    params = schema.get("parameters")
    if isinstance(params, dict):
        required = params.get("required")
        if isinstance(required, list) and "result" not in required:
            required.append("result")
    return schema
