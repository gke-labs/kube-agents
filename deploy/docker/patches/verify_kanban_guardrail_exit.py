#!/usr/bin/env python3
"""Build gate for the kanban guardrail-exit patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_guardrail_exit.py``. The applier only proves its two anchors
matched exactly once; that says nothing about whether the inserted code is
reachable, whether it still composes with the upstream helpers it calls, or
whether the board write it makes lands.

Four things are checked, and each one is a way the patch could match its anchor
and still be useless:

1. **The nudge is reachable and terminal.** Parsed out of the *patched*
   ``agent/conversation_loop.py``: the insert has to sit inside the
   guardrail-halt branch, ahead of the ``break`` it is there to pre-empt, and it
   has to clear ``agent._tool_guardrail_halt_decision`` before ``continue`` —
   ``reset_for_turn`` clears that once per turn, not per iteration, so leaving it
   set sends the next iteration straight back into the branch and out through the
   same ``break``. Also asserted: the insert never touches
   ``_turn_web_search_count``, which would hand the model another 50 searches.
2. **It still composes with upstream.** ``guardrail_halt_nudge`` is driven
   against the real ``agent.kanban_stop.build_kanban_stop_nudge`` — a signature
   change there is silent, because the patch swallows exceptions and falls back
   to the old exit path by design.
3. **The backstop is inside the funnel.** The ``finalize_turn`` insert is checked
   for placement within the function every ``break`` passes through.
4. **The board write lands.** ``record_missing_terminal_call`` is driven against
   a real kanban database through the real ``_record_task_failure``: claim
   released, run closed, failure counted, exit reason legible in the event. Plus
   the three cases that must *not* write — a card the board no longer shows as
   ``running`` (compaction can drop a terminal tool call out of ``messages``,
   the board cannot), a goal-mode worker between turns, and a dispatched cron
   run, which is holding its *caller's* task id. The cron case is checked
   against the real ``tools.cron_run_scope``: the unit tests can only reach that
   module as a top-level sibling, so this is the only place the import the
   exclusion actually defaults to is exercised.

The per-turn ``max_web_searches`` ceiling that triggered all this is a config
change, not a patch, and is gated where the template is built — see the
``/opt/platform-template/config.yaml`` assertion in the Dockerfile.

Usage::

    cd /opt/hermes && python3 verify_kanban_guardrail_exit.py
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))


# --- 1. The nudge is reachable, and it is the exit it claims to be ----------
print("guardrail-halt nudge (agent/conversation_loop.py):")

loop_tree = ast.parse((HERMES / "agent" / "conversation_loop.py").read_text())

HALT_TEST = "agent._tool_guardrail_halt_decision is not None"
halt_ifs = [
    node
    for node in ast.walk(loop_tree)
    if isinstance(node, ast.If) and ast.unparse(node.test) == HALT_TEST
]
check(
    "the guardrail-halt branch is still a single branch",
    len(halt_ifs) == 1,
    f"found {len(halt_ifs)} — the anchor matched something that moved",
)

nudge_if = None
halt_break = None
if len(halt_ifs) == 1:
    halt = halt_ifs[0]
    nudge_ifs = [
        s
        for s in halt.body
        if isinstance(s, ast.If) and ast.unparse(s.test) == "_kanban_halt_nudge"
    ]
    breaks = [s for s in halt.body if isinstance(s, ast.Break)]
    check("the nudge sits inside the halt branch", len(nudge_ifs) == 1)
    check("the halt branch still ends in a break", len(breaks) == 1)
    if nudge_ifs:
        nudge_if = nudge_ifs[0]
    if breaks:
        halt_break = breaks[0]

if nudge_if is not None and halt_break is not None:
    check(
        "the nudge runs before the break it pre-empts",
        nudge_if.lineno < halt_break.lineno,
        f"nudge at {nudge_if.lineno}, break at {halt_break.lineno}",
    )

if nudge_if is not None:
    body = nudge_if.body
    assigns = {
        ast.unparse(t): ast.unparse(s.value)
        for s in body
        if isinstance(s, ast.Assign)
        for t in s.targets
    }

    check(
        "the nudge path continues the loop rather than falling through",
        isinstance(body[-1], ast.Continue),
        f"last statement is {type(body[-1]).__name__}",
    )
    check(
        "the halt decision is cleared before continuing",
        assigns.get("agent._tool_guardrail_halt_decision") == "None",
        "reset_for_turn clears it per turn, not per iteration — the next "
        "iteration would re-enter this branch and break anyway",
    )
    check(
        "the halt text is withheld as this turn's answer",
        assigns.get("final_response") == "None"
        and "_pending_verification_response" in assigns,
        f"assignments: {sorted(assigns)}",
    )
    check(
        "the exit reason is taken back off guardrail_halt",
        assigns.get("_turn_exit_reason") == "'unknown'",
        f"left as {assigns.get('_turn_exit_reason')!r}",
    )

    nudge_src = ast.unparse(nudge_if)
    check(
        "the synthetic turn is marked as one",
        "_kanban_stop_synthetic" in nudge_src,
        "unmarked synthetic turns are indistinguishable from the user's",
    )
    check(
        "the nudge budget is spent, not just read",
        "agent._kanban_stop_nudges" in nudge_src,
        "without the increment the two-attempt bound never terminates",
    )
    check(
        "the search counter is left alone",
        "_turn_web_search_count" not in nudge_src,
        "resetting it hands the model another full cap",
    )


# --- 2. It still composes with the upstream helper it borrows ---------------
print("composition with agent.kanban_stop:")

from agent.kanban_stop import build_kanban_stop_nudge  # noqa: E402
from hermes_cli.kanban_guardrail_exit import (  # noqa: E402
    DEFAULT_MAX_NUDGES,
    DETECTOR,
    OUTCOME,
    guardrail_halt_nudge,
    missing_terminal_error,
    record_missing_terminal_call,
    should_record_missing_terminal,
    task_is_still_running,
)


class _Decision:
    """The shape ``_guardrail_block_result`` records on the agent."""

    tool_name = "web_search"
    code = "loop_web_search_cap"


DECISION = _Decision()
TERMINAL_MESSAGES = [
    {
        "role": "assistant",
        "tool_calls": [{"function": {"name": "kanban_complete"}}],
    }
]

os.environ["HERMES_KANBAN_TASK"] = "t_verify"
os.environ.pop("HERMES_KANBAN_GOAL_MODE", None)

nudge = guardrail_halt_nudge(
    build_kanban_stop_nudge, messages=[], attempts=0, decision=DECISION
)
check(
    "a halted worker gets a nudge",
    isinstance(nudge, str) and nudge,
    "upstream's build_kanban_stop_nudge signature or gating changed — the "
    "patch swallows that and silently reverts to the old exit",
)
if isinstance(nudge, str):
    check(
        "the nudge still carries upstream's terminal-call instruction",
        "kanban_complete" in nudge and "kanban_block" in nudge,
    )
    check(
        "it names the tool that is gone and why",
        "web_search" in nudge and "loop_web_search_cap" in nudge,
    )
    check(
        "it tells the model not to retry the exhausted tool",
        "do not try again" in nudge,
        "without this the model re-halts and burns the next nudge",
    )

check(
    "the nudge budget is bounded",
    guardrail_halt_nudge(
        build_kanban_stop_nudge,
        messages=[],
        attempts=DEFAULT_MAX_NUDGES,
        decision=DECISION,
    )
    is None,
    "an unbounded nudge loop is worse than the exit it replaces",
)
check(
    "a worker that already closed its card is not nudged",
    guardrail_halt_nudge(
        build_kanban_stop_nudge,
        messages=TERMINAL_MESSAGES,
        attempts=0,
        decision=DECISION,
    )
    is None,
)

del os.environ["HERMES_KANBAN_TASK"]
check(
    "non-kanban sessions keep the stock halt behaviour",
    guardrail_halt_nudge(
        build_kanban_stop_nudge, messages=[], attempts=0, decision=DECISION
    )
    is None,
    "the patch changed the exit for every interactive session too",
)
os.environ["HERMES_KANBAN_TASK"] = "t_verify"


# --- 3. The backstop is inside the funnel every break passes through --------
print("terminal-call backstop (agent/turn_finalizer.py):")

finalizer_tree = ast.parse((HERMES / "agent" / "turn_finalizer.py").read_text())
finalize = next(
    (
        n
        for n in ast.walk(finalizer_tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "finalize_turn"
    ),
    None,
)
check("finalize_turn is still the funnel", finalize is not None)

if finalize is not None:
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(finalize)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    check(
        "the leak check runs inside finalize_turn",
        "_kanban_should_record_missing" in calls,
        "an insert outside the funnel covers none of the seven exits",
    )
    check(
        "the recorder runs inside finalize_turn",
        "_kanban_record_missing_terminal" in calls,
    )
    check(
        "the exit reason reaches the board",
        "_turn_exit_reason" in ast.unparse(finalize),
        "without it the failure is as unexplained as the protocol violation "
        "it replaces",
    )

# The seven ways out of the loop that leak. Each has to be legible in the
# failure text, because that text is the only account of what happened.
EXIT_REASONS = (
    "guardrail_halt",
    "all_retries_exhausted_no_response",
    "partial_stream_recovery",
    "fallback_prior_turn_content",
    "empty_response_exhausted",
    "local_processing_error",
    "error_near_max_iterations",
)
errors = {r: missing_terminal_error(r) for r in EXIT_REASONS}
check(
    "every exit reason names itself in the failure text",
    all(r in errors[r] for r in EXIT_REASONS),
)
check(
    "the seven exits stay distinguishable after truncation to 500 chars",
    len({e[:500] for e in errors.values()}) == len(EXIT_REASONS),
)


def leaks(**over):
    kwargs = dict(
        task_id="t_verify",
        interrupted=False,
        failed=False,
        iteration_limit_fallback=False,
    )
    kwargs.update(over)
    return should_record_missing_terminal(**kwargs)


check("a worker that never closed its card is a leak", leaks() is True)
check(
    "the transcript is not consulted — the board is",
    "session_called_terminal"
    not in inspect.signature(should_record_missing_terminal).parameters,
    "a REJECTED kanban_complete still lands as a tool message named "
    "kanban_complete, so the transcript check let one refusal from "
    "kanban_result_required silence the backstop for the rest of the run",
)
check("an interrupt is not", leaks(interrupted=True) is False)
check("an already-failed turn is not", leaks(failed=True) is False)
check(
    "the iteration-budget path is not double-counted",
    leaks(iteration_limit_fallback=True) is False,
)
check("a non-worker session is not", leaks(task_id=None) is False)
check(
    "a goal-mode worker between turns is not",
    leaks(goal_mode=True) is False,
    "goal mode calls finalize_turn once per turn — recording on turn 1 of N "
    "would release the claim underneath the loop",
)

os.environ["HERMES_KANBAN_GOAL_MODE"] = "1"
check(
    "goal mode is read from the environment the dispatcher sets",
    leaks() is False,
    "the exclusion only works if it defaults to the real env var",
)
del os.environ["HERMES_KANBAN_GOAL_MODE"]

check("a dispatched cron run is not a leak", leaks(cron_run=True) is False)

# The unit tests can only reach cron_run_scope as a top-level sibling. In the
# image it is tools.cron_run_scope, and that import is the one that decides
# whether the exclusion defaults on at all — get it wrong and _in_cron_run
# silently degrades to the process-wide env var, losing the per-thread
# precision that makes it safe under dispatch_in_gateway.
from tools.cron_run_scope import cron_run_scope  # noqa: E402

with cron_run_scope("verify-fleet-audit"):
    check(
        "the exclusion defaults to tools.cron_run_scope's context variable",
        leaks() is False,
        "a cron run would charge its caller a timed_out and release the claim "
        "the caller is still blocked on",
    )
check(
    "and the scope hands the marker back on the way out",
    leaks() is True,
    "a leaked marker disables the backstop for every worker after it",
)

check(
    "a delegate_task child is not a leak", leaks(delegated_child=True) is False
)

# Both defaults come from tools/kanban_ownership.py, shared with
# tools/kanban_worker_tools.py, which asks the same two questions with
# on_unknown=False. verify_kanban_worker_tools.py proves the polarity argument
# is honoured; what has to be true here is that this module reached the shipped
# copy at all rather than falling through to a top-level sibling that happens to
# be importable.
_ownership = sys.modules.get("tools.kanban_ownership")
check(
    "the exclusions read tools/kanban_ownership.py",
    _ownership is not None
    and Path(_ownership.__file__).resolve()
    == (HERMES / "tools" / "kanban_ownership.py").resolve(),
    f"resolved to {getattr(_ownership, '__file__', None)!r}",
)

# Same argument as the cron scope above, against the module the runtime really
# reads. A child runs in the parent's process with the parent's
# HERMES_KANBAN_TASK still set, so a default that did not consult this would
# charge the PARENT a timed_out and release its claim mid-run.
from agent.delegation_context import (  # noqa: E402
    delegated_child_context,
    is_delegated_child_context,
)

check(
    "outside a delegation the real context says 'not a child'",
    is_delegated_child_context() is False,
)
with delegated_child_context():
    check(
        "the exclusion defaults to agent.delegation_context",
        leaks() is False,
        "upstream refuses every board mutation from a delegate_task child for "
        "the same reason: an inherited HERMES_KANBAN_TASK is not ownership",
    )
check(
    "and the delegation context hands control back on the way out",
    leaks() is True,
)


# --- 4. The board write lands -----------------------------------------------
print("board write:")

from hermes_cli import kanban_db as K  # noqa: E402

TMP = Path(tempfile.mkdtemp())
DB = TMP / "kanban.db"


def board():
    return K.connect(DB)


def row(conn, tid):
    return conn.execute(
        "SELECT status, claim_lock, consecutive_failures, last_failure_error "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def events(conn, tid):
    return [
        (r["kind"], r["payload"])
        for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        )
    ]


def open_runs(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ? AND ended_at IS NULL",
        (tid,),
    ).fetchone()["n"]


conn = board()
card = K.create_task(
    conn, title="Evaluate Config Connector Setup Patterns", assignee="platform"
)
K.recompute_ready(conn)
check("the card is claimed the way the dispatcher claims it", K.claim_task(conn, card))
check("the run is open before the leak", open_runs(conn, card) == 1)

recorded = record_missing_terminal_call(
    task_id=card,
    turn_exit_reason="guardrail_halt",
    connect=board,
    record_failure=K._record_task_failure,
)
check("the leak is recorded", recorded is True)

conn = board()
after = row(conn, card)
check(
    "the claim is handed back",
    after["claim_lock"] is None and after["status"] != "running",
    f"status={after['status']!r} lock={after['claim_lock']!r}",
)
check("the run is closed", open_runs(conn, card) == 0)
check("the failure is counted", after["consecutive_failures"] == 1)
check(
    "the exit reason survives to the board",
    "turn_exit_reason=guardrail_halt" in (after["last_failure_error"] or ""),
    f"last_failure_error={after['last_failure_error']!r}",
)
kinds = [k for k, _ in events(conn, card)]
check(
    f"the failure is classified {OUTCOME}",
    OUTCOME in kinds,
    f"events: {kinds}",
)
check(
    "the reason is legible in the event payload too",
    any(
        k == OUTCOME and "turn_exit_reason=guardrail_halt" in (p or "")
        for k, p in events(conn, card)
    ),
)

# Nothing to hand back twice. This is the guard that makes a false positive
# impossible rather than merely unlikely: the transcript can lie about a
# terminal call after compaction, the board cannot.
check("the card is no longer running", not task_is_still_running(conn, card))
before = len(events(conn, card))
check(
    "a card the board has already moved is left alone",
    record_missing_terminal_call(
        task_id=card,
        turn_exit_reason="guardrail_halt",
        connect=board,
        record_failure=K._record_task_failure,
    )
    is False,
)
check(
    "and nothing is written for it",
    len(events(board(), card)) == before,
)
check(
    "an unknown card is left alone",
    record_missing_terminal_call(
        task_id="t_does_not_exist",
        turn_exit_reason="guardrail_halt",
        connect=board,
        record_failure=K._record_task_failure,
    )
    is False,
)

# Repeated leaks must reach the circuit breaker rather than looping forever,
# and the breaker's event is the one place event_payload_extra lands.
conn = board()
for _ in range(6):
    K.recompute_ready(conn)
    if not K.claim_task(conn, card):
        break
    record_missing_terminal_call(
        task_id=card,
        turn_exit_reason="guardrail_halt",
        connect=board,
        record_failure=K._record_task_failure,
    )
    conn = board()

gave_up = [p for k, p in events(conn, card) if k == "gave_up"]
check(
    "repeated leaks trip the circuit breaker",
    gave_up,
    "the card would retry indefinitely",
)
if gave_up:
    check(
        "the breaker's event says what detected this and how the turn ended",
        DETECTOR in gave_up[-1] and "turn_exit_reason" in gave_up[-1],
        f"payload: {gave_up[-1]}",
    )
check(
    "the card ends up blocked for a human rather than in flight",
    row(conn, card)["status"] == "blocked",
    f"status={row(conn, card)['status']!r}",
)


print()
if FAILURES:
    print(f"verify_kanban_guardrail_exit: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_guardrail_exit: all checks passed")
