"""Stop kanban workers leaking out of ``run_conversation`` without a board write.

Two anchored edits, one per file:

1. ``agent/conversation_loop.py`` — the tool-guardrail halt branch breaks out of
   the agent loop from inside the tool-call branch, jumping over the kanban
   terminal-tool stop guard that lives ~760 lines below it in the *no*-tool-calls
   branch. Nudge the worker to finish on the board before taking that break.
2. ``agent/turn_finalizer.py`` — a backstop in ``finalize_turn``, the single
   funnel every ``break`` in the loop passes through, for the six sibling exits
   that leak the same way and for the halt path when its nudges are spent.

Both inserts mirror code that is already in the file: edit 1 copies the shape of
the stop guard's local-import + ``try``/``except`` at
``conversation_loop.py``'s "Kanban worker terminal-tool stop guard", and edit 2
copies the ``_record_task_failure`` call the iteration-budget block directly
above it already makes. Neither is idempotent — a second run fails on the
anchor count, which is the intent.

See the module docstring in kanban_guardrail_exit.py for the incident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

LOOP_RELATIVE = "agent/conversation_loop.py"
FINALIZER_RELATIVE = "agent/turn_finalizer.py"

# The two inner lines are each unique in the file on their own; anchoring on the
# whole block additionally pins the insertion point to just after the assistant
# halt message is appended, which is where ``messages`` first contains
# everything the model needs to act on the nudge.
HALT_ANCHOR = (
    '                    decision = agent._tool_guardrail_halt_decision\n'
    '                    _turn_exit_reason = "guardrail_halt"\n'
    '                    final_response = agent._toolguard_controlled_halt_response(decision)\n'
    '                    agent._emit_status(\n'
    '                        f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}"\n'
    '                    )\n'
    '                    messages.append({"role": "assistant", "content": final_response})\n'
)

HALT_INSERT = '''                    # kube-agents patch: the break below jumps over the
                    # kanban terminal-tool stop guard further down this loop, so
                    # a worker halted here exits rc=0 having never called
                    # kanban_complete/kanban_block and the dispatcher records an
                    # unexplained protocol violation. Give it the turn it needs
                    # to close its own card.
                    # See hermes_cli/kanban_guardrail_exit.py.
                    try:
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
                        logger.debug(
                            "kanban guardrail-halt check failed", exc_info=True
                        )
                        _kanban_halt_nudge = None

                    if _kanban_halt_nudge:
                        agent._kanban_stop_nudges = (
                            getattr(agent, "_kanban_stop_nudges", 0) + 1
                        )
                        messages.append({
                            "role": "user",
                            "content": _kanban_halt_nudge,
                            "_kanban_stop_synthetic": True,
                        })
                        agent._session_messages = messages
                        # Mandatory: reset_for_turn clears the halt decision once
                        # per turn, not per iteration. Leave it set and the next
                        # iteration re-enters this branch and breaks anyway.
                        agent._tool_guardrail_halt_decision = None
                        _turn_exit_reason = "unknown"
                        logger.info(
                            "kanban guardrail-halt nudge issued (attempt %d) "
                            "task=%s tool=%s code=%s",
                            agent._kanban_stop_nudges,
                            os.environ.get("HERMES_KANBAN_TASK", ""),
                            decision.tool_name,
                            decision.code,
                        )
                        agent._emit_status(
                            "⚠️ Kanban worker halted by a tool guardrail — "
                            "nudging it to finish on the board"
                        )
                        # Same finalizer contract as the stop guard: withhold the
                        # halt text as this turn's answer while continuing, so a
                        # later budget exhaustion cannot mistake it for one.
                        _pending_verification_response = final_response
                        _pending_verification_response_previewed = (
                            agent._interim_content_was_streamed(final_response or "")
                        )
                        final_response = None
                        continue

'''

FINALIZER_ANCHOR = "    # Determine if conversation completed successfully\n"

FINALIZER_INSERT = '''    # kube-agents patch: the guardrail-halt break and six sibling exits
    # (all_retries_exhausted_no_response, partial_stream_recovery,
    # fallback_prior_turn_content, empty_response_exhausted,
    # local_processing_error, error_near_max_iterations) all leave failed=False
    # with no board write, so a kanban worker exits rc=0 and the dispatcher
    # stamps an unexplained protocol violation after the fact. This is the one
    # funnel they all pass through. Goal-mode workers are excluded — their
    # intermediate turns end without a terminal call by design.
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
            iteration_limit_fallback=iteration_limit_fallback,
        )
    except Exception:
        logger.debug("kanban terminal-call backstop check failed", exc_info=True)
        _kanban_leaked = False

    if _kanban_leaked:
        try:
            from hermes_cli import kanban_db as _kb
            from hermes_cli.kanban_guardrail_exit import (
                record_missing_terminal_call as _kanban_record_missing_terminal,
            )

            if _kanban_record_missing_terminal(
                task_id=_kanban_task_id,
                turn_exit_reason=_turn_exit_reason,
                connect=_kb.connect,
                record_failure=_kb._record_task_failure,
            ):
                logger.info(
                    "recorded missing-terminal-call failure for task %s "
                    "(turn_exit_reason=%s)",
                    _kanban_task_id,
                    _turn_exit_reason,
                )
        except Exception:
            logger.warning(
                "Failed to record missing-terminal-call failure for task %s",
                _kanban_task_id,
                exc_info=True,
            )

'''

# Both inserts sit next to their anchor rather than consuming it, so the anchor
# count alone cannot tell a fresh file from an already-patched one. These
# markers can: each appears only in the inserted text.
EDITS = (
    (
        LOOP_RELATIVE,
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
)


def apply(root: Path) -> None:
    for relative, label, anchor, patched, marker in EDITS:
        patch = patchlib.Patch(root, relative, prefix="guardrail-exit")
        patch.refuse_if_patched(marker)
        patch.substitute(anchor, patched, label=label)
        patch.commit(label)


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
