"""Stop kanban workers leaking out of ``run_conversation`` without a board write.

Five anchored edits across five files. Since v2026.9.14 the turn loop is split
into phase helpers under ``agent/turn_*.py``, each returning a verdict the loop
copies back into its locals, so every site lives in the module that owns its
phase:

1. ``agent/turn_tool_round.py`` — the tool-guardrail halt branch ends the turn
   with ``return _verdict("break")`` from inside the tool round, jumping over the
   kanban stop gate that runs only in the text-response phase
   (``agent/turn_stop_gates.py``). Nudge the worker to finish on the board and
   return ``_verdict("continue")`` instead.
2. ``agent/turn_finalizer.py`` — a backstop in ``finalize_turn``, the single
   funnel every exit passes through, for the six sibling exits that leak the
   same way and for the halt path when its nudges are spent. A
   retries-exhausted exit whose last failure was a rate limit is blocked with
   the provider's text instead of being charged a ``timed_out``.
3. ``agent/turn_api_error.py`` — the retry loop's error handler stashes its last
   classified failure ``(reason, summary)`` on the agent, which is how edit 2
   tells a 429 exhaustion from the other retries-exhausted exits.
4. ``cli.py`` — ``_run_quiet_single_query``'s exit-code block, where a ``failed``
   result with ``failure_reason="rate_limit"`` becomes exit 75: block the card
   there, with the provider's text, before the process leaves. Only the
   fully-quiet ``-Q`` path (goal-mode workers) reaches that block.
5. ``hermes_cli/cli_chat_turn_mixin.py`` — ``_chat_render_turn``, where the
   non-quiet ``chat -q`` path every normal worker takes last holds the failed
   result. Same block; whichever of the two runs second finds the card already
   moved.

The inserts mirror code that is already in the tree: edit 1 copies the shape of
upstream's kanban stop gate in ``agent/turn_stop_gates.py`` (local import,
synthetic user row through ``append_message``, nudge counter on the agent), and
edit 2 copies the ``_record_task_failure`` call ``_resolve_budget_fallback``
directly above it already makes. None is idempotent — every insert sits next to
its anchor rather than consuming it, so a marker check refuses the second run.

See the module docstring in kanban_guardrail_exit.py for the incidents.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

TOOL_ROUND_RELATIVE = "agent/turn_tool_round.py"
FINALIZER_RELATIVE = "agent/turn_finalizer.py"
API_ERROR_RELATIVE = "agent/turn_api_error.py"
CLI_RELATIVE = "cli.py"
CHAT_RELATIVE = "hermes_cli/cli_chat_turn_mixin.py"

# The inner lines are each unique in the file on their own; anchoring on the
# whole block additionally pins the insertion point to just after the assistant
# halt message is appended, which is where ``messages`` first contains
# everything the model needs to act on the nudge.
HALT_ANCHOR = (
    '        decision = agent._tool_guardrail_halt_decision\n'
    '        _turn_exit_reason = "guardrail_halt"\n'
    '        final_response = agent._toolguard_controlled_halt_response(decision)\n'
    '        agent._emit_status(f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}")\n'
    '        append_message(messages, {"role": "assistant", "content": final_response})\n'
)

HALT_INSERT = '''        # kube-agents patch: the ``return _verdict("break")`` below ends the
        # turn from inside the tool round, jumping over the kanban stop gate
        # that runs only in the text-response phase (agent/turn_stop_gates.py),
        # so a worker halted here exits rc=0 having never called
        # kanban_complete/kanban_block and the dispatcher records an
        # unexplained protocol violation. Give it the turn it needs to close
        # its own card. See hermes_cli/kanban_guardrail_exit.py.
        try:
            import os as _kube_os
            from agent.kanban_stop import build_kanban_stop_nudge
            from hermes_cli.kanban_guardrail_exit import (
                guardrail_halt_nudge as _kanban_guardrail_halt_nudge,
            )

            _kanban_halt_nudge = _kanban_guardrail_halt_nudge(
                build_kanban_stop_nudge,
                messages=messages,
                attempts=getattr(agent, "_kanban_stop_nudges", 0),
                decision=decision,
            )
        except Exception:
            logger.debug("kanban guardrail-halt check failed", exc_info=True)
            _kanban_halt_nudge = None

        if _kanban_halt_nudge:
            agent._kanban_stop_nudges = getattr(agent, "_kanban_stop_nudges", 0) + 1
            append_message(messages, {
                "role": "user",
                "content": _kanban_halt_nudge,
                "_kanban_stop_synthetic": True,
            })
            agent._session_messages = messages
            # Mandatory: reset_for_turn clears the halt decision once per turn,
            # not per round. Leave it set and the next round re-enters this
            # branch and breaks anyway.
            agent._tool_guardrail_halt_decision = None
            _turn_exit_reason = "unknown"
            logger.info(
                "kanban guardrail-halt nudge issued (attempt %d) "
                "task=%s tool=%s code=%s",
                agent._kanban_stop_nudges,
                _kube_os.environ.get("HERMES_KANBAN_TASK", ""),
                decision.tool_name,
                decision.code,
            )
            agent._emit_status(
                "⚠️ Kanban worker halted by a tool guardrail — "
                "nudging it to finish on the board"
            )
            # The halt text is the assistant row the model now answers, not
            # this turn's reply: clear it so the finalizer cannot mistake it
            # for one. The tool-round verdict carries no pending-verification
            # slot, so a later budget exhaustion asks the model for a summary
            # rather than reusing the halt text. ``_verdict`` closes over these
            # locals and reads them as they stand here.
            final_response = None
            return _verdict("continue")

'''

# The first statement of finalize_turn after the budget fallback: the insert
# lands between them, so an exhausted budget has already been recorded (and
# the card already moved) by the time the backstop asks the board.
FINALIZER_ANCHOR = (
    "    completed = (\n"
    "        final_response is not None\n"
    "        and not failed\n"
)

FINALIZER_INSERT = '''    # kube-agents patch: the guardrail-halt exit and six sibling exits
    # (all_retries_exhausted_no_response, partial_stream_recovery,
    # fallback_prior_turn_content, empty_response_exhausted,
    # local_processing_error, error_near_max_iterations) all leave failed=False
    # with no board write, so a kanban worker exits rc=0 and the dispatcher
    # stamps an unexplained protocol violation after the fact. This is the one
    # funnel they all pass through. Goal-mode workers are excluded — their
    # intermediate turns end without a terminal call by design — and so is an
    # exhausted iteration budget, which _resolve_budget_fallback above has
    # already recorded as timed_out (same predicate as its budget_exhausted).
    # See hermes_cli/kanban_guardrail_exit.py.
    _kanban_task_id = os.environ.get("HERMES_KANBAN_TASK")
    try:
        from hermes_cli.kanban_guardrail_exit import (
            should_record_missing_terminal as _kanban_should_record_missing,
        )

        _kanban_leaked = _kanban_should_record_missing(
            task_id=_kanban_task_id,
            interrupted=interrupted,
            failed=failed,
            iteration_limit_fallback=(
                api_call_count >= agent.max_iterations
                or agent.iteration_budget.remaining <= 0
            ),
        )
    except Exception:
        logger.debug("kanban terminal-call backstop check failed", exc_info=True)
        _kanban_leaked = False

    if _kanban_leaked:
        try:
            from hermes_cli import kanban_db as _kb
            from hermes_cli.kanban_db_connect import connect as _kube_kanban_connect
            from hermes_cli.kanban_db_dispatch import (
                _record_task_failure as _kube_record_task_failure,
            )
            from hermes_cli.kanban_guardrail_exit import (
                record_missing_terminal_call as _kanban_record_missing_terminal,
                worker_run_id as _kube_worker_run_id,
            )

            # A retry loop that ended on a 429 blocks the card with the
            # provider's text (block_task, kind=transient) instead of being
            # charged a timed_out; the error handler's stash says which.
            if _kanban_record_missing_terminal(
                task_id=_kanban_task_id,
                turn_exit_reason=_turn_exit_reason,
                connect=_kube_kanban_connect,
                record_failure=_kube_record_task_failure,
                block_task=_kb.block_task,
                last_api_failure=getattr(agent, "_kube_last_api_failure", None),
                run_id=_kube_worker_run_id(),
            ):
                logger.info(
                    "recorded missing-terminal-call outcome for task %s "
                    "(turn_exit_reason=%s, last_api_failure=%s)",
                    _kanban_task_id,
                    _turn_exit_reason,
                    getattr(agent, "_kube_last_api_failure", None),
                )
        except Exception:
            logger.warning(
                "Failed to record missing-terminal-call failure for task %s",
                _kanban_task_id,
                exc_info=True,
            )

'''

# The one call that classifies an API error in the retry loop's handler
# (``handle_api_error``, whose ``api_error`` parameter is the caught
# exception). The stash has to land right after it: ``classified`` is the
# verdict, and the terminal branch that would otherwise be the only place to
# record it is not reached when the loop leaves through its ``while``
# condition instead.
CLASSIFY_ANCHOR = (
    "    classified = classify_api_error(\n"
    '        api_error, provider=getattr(agent, "provider", "") or "",\n'
    '        model=getattr(agent, "model", "") or "", approx_tokens=approx_tokens,\n'
    "        context_length=_ctx_len, num_messages=len(api_messages) if api_messages else 0,\n"
    "    )\n"
)

CLASSIFY_INSERT = '''    # kube-agents patch: keep the last classified failure where finalize_turn
    # can read it, so a retry loop that ends with no response after a 429
    # blocks the card with the provider's text instead of being charged a
    # timed_out like the other retries-exhausted exits.
    # See hermes_cli/kanban_guardrail_exit.py.
    try:
        agent._kube_last_api_failure = (
            classified.reason.value,
            agent._summarize_api_error(api_error),
        )
    except Exception:
        agent._kube_last_api_failure = None

'''

# The kanban worker's exit-code block in ``_run_quiet_single_query``: the three
# lines that turn a ``failed`` result into exit 1 and, for a rate limit, into
# KANBAN_RATE_LIMIT_EXIT_CODE. The block goes ahead of them so the card is
# closed before the process is.
CLI_ANCHOR = (
    "    _exit_code = 0\n"
    '    if isinstance(result, dict) and result.get("failed"):\n'
    "        _exit_code = 1\n"
)

CLI_INSERT = '''    # kube-agents patch: a worker whose 429 retries are exhausted returns
    # failed=True with failure_reason="rate_limit" and exits 75 below, and its
    # card is left running for the reaper to classify. Block it here, with the
    # provider's text as the reason, so the board carries the cause whatever
    # the reaper makes of the exit. See hermes_cli/kanban_guardrail_exit.py.
    try:
        from hermes_cli.kanban_guardrail_exit import (
            block_rate_limited_worker as _kube_block_rate_limited,
        )

        if _kube_block_rate_limited(result):
            logger.info(
                "blocked kanban task %s: provider rate limit exhausted the API retries",
                os.environ.get("HERMES_KANBAN_TASK", ""),
            )
    except Exception:
        logger.debug("kanban rate-limit block failed", exc_info=True)
'''

# ``_chat_render_turn`` is the post-turn render every ``chat()`` turn goes
# through, failed or not, with the result on ``turn.result``. The block goes
# ahead of the response panel, which is the first thing that reads the failed
# result, so a normal kanban worker (``hermes ... chat -q``, no ``-Q``) closes
# its card before it prints and returns through ``_print_exit_summary``.
CHAT_ANCHOR = "        self._chat_print_response_panel(turn, response)\n"

CHAT_INSERT = '''        # kube-agents patch: the non-quiet single-query path every normal
        # kanban worker takes (`hermes ... chat -q`) renders its turn here and
        # never reaches the exit-code block in cli.py, so this is where its
        # failed result is last in hand. Block the card here on a 429
        # exhaustion; the exit-code site covers the -Q path and finds nothing
        # left to do when this ran first.
        # See hermes_cli/kanban_guardrail_exit.py.
        if turn.result and os.environ.get("HERMES_KANBAN_TASK"):
            try:
                from hermes_cli.kanban_guardrail_exit import (
                    block_rate_limited_worker as _kube_block_rate_limited_chat,
                )

                if _kube_block_rate_limited_chat(turn.result):
                    logging.getLogger(__name__).info(
                        "blocked kanban task %s: provider rate limit exhausted the API retries",
                        os.environ.get("HERMES_KANBAN_TASK", ""),
                    )
            except Exception:
                logging.getLogger(__name__).debug(
                    "kanban rate-limit block failed", exc_info=True
                )
'''

# Every insert sits next to its anchor rather than consuming it, so the anchor
# count alone cannot tell a fresh file from an already-patched one. These
# markers can: each appears only in the inserted text.
EDITS = (
    (
        TOOL_ROUND_RELATIVE,
        "guardrail halt nudge",
        HALT_ANCHOR,
        HALT_ANCHOR + HALT_INSERT,
        "_kanban_guardrail_halt_nudge",
    ),
    (
        FINALIZER_RELATIVE,
        "finalize_turn terminal-call backstop",
        FINALIZER_ANCHOR,
        FINALIZER_INSERT + FINALIZER_ANCHOR,
        "_kanban_should_record_missing",
    ),
    (
        API_ERROR_RELATIVE,
        "last classified API failure stash",
        CLASSIFY_ANCHOR,
        CLASSIFY_ANCHOR + CLASSIFY_INSERT,
        "_kube_last_api_failure",
    ),
    (
        CLI_RELATIVE,
        "rate-limited worker exit block",
        CLI_ANCHOR,
        CLI_INSERT + CLI_ANCHOR,
        "_kube_block_rate_limited(",
    ),
    (
        CHAT_RELATIVE,
        "rate-limited worker chat-render block",
        CHAT_ANCHOR,
        CHAT_INSERT + CHAT_ANCHOR,
        "_kube_block_rate_limited_chat",
    ),
)


def apply(root: Path) -> None:
    for relative, label, anchor, patched, marker in EDITS:
        patch = patchlib.Patch(root, relative, prefix="guardrail-exit")
        patch.refuse_if_patched(marker)
        patch.substitute(anchor, patched, label=label)
        patch.commit(label)


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
