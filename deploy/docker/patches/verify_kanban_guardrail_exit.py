#!/usr/bin/env python3
"""Build gate for the kanban guardrail-exit patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_guardrail_exit.py``. The applier only proves its seven anchors
matched exactly once; that says nothing about whether the inserted code is
reachable, whether it still composes with the upstream helpers it calls, or
whether the board write it makes lands.

Seven things are checked, and each one is a way the patch could match its anchor
and still be useless:

1. **The nudge is reachable and terminal.** Parsed out of the *patched*
   ``agent/turn_tool_round.py``: the insert has to sit inside the
   guardrail-halt branch, ahead of the ``return _verdict("break")`` it is there
   to pre-empt, it has to hand back ``_verdict("continue")``, and it has to
   clear ``agent._tool_guardrail_halt_decision`` first — ``reset_for_turn``
   clears that once per turn, not per round, so leaving it set sends the next
   round straight back into the branch and out through the same return. The
   locals it rebinds (``final_response``, ``_turn_exit_reason``) and mutates (``messages``)
   must be fields of ``ToolRoundVerdict``, or the loop never sees them. Also
   asserted: the insert never touches ``_turn_web_search_count``, which would
   hand the model another 50 searches.
2. **It still composes with upstream.** ``guardrail_halt_nudge`` is driven
   against the real ``agent.kanban_stop.build_kanban_stop_nudge`` — a signature
   change there is silent, because the patch swallows exceptions and falls back
   to the old exit path by design.
3. **The backstop is inside the funnel.** The ``finalize_turn`` insert is checked
   for placement within the function every exit passes through, after the
   budget fallback that records an exhausted budget itself and before the
   ``completed`` determination.
4. **The board write lands.** ``record_missing_terminal_call`` is driven against
   a real kanban database through the real ``_record_task_failure``: claim
   released, run closed, failure counted, exit reason legible in the event, and
   no text upstream would re-read as an auth blocker or a protocol violation.
   Plus a worker whose run already ended (it blocked, the card was re-run, and
   it ended in text afterwards), which must leave the next worker's run open,
   and the same hand-over landing between the backstop's read and
   ``_record_task_failure``'s write, which the run check inside the write
   transaction must refuse.
   Plus the three cases that must *not* write — a card the board no longer shows as
   ``running`` (compaction can drop a terminal tool call out of ``messages``,
   the board cannot), a goal-mode worker between turns, and a dispatched cron
   run, which is holding its *caller's* task id. The cron case is checked
   against the real ``tools.cron_run_scope``: the unit tests can only reach that
   module as a top-level sibling, so this is the only place the import the
   exclusion actually defaults to is exercised.

5. **The rate-limit sites are where the code says a 429 goes.** The stash in
   ``agent/turn_api_error.py`` follows the classification it copies from,
   inside ``handle_api_error``; the ``cli.py`` block runs ahead of the exit-code
   decision it must not change, and that decision still routes ``rate_limit``
   to ``KANBAN_RATE_LIMIT_EXIT_CODE``; the chat-render block sits in
   ``_chat_render_turn`` ahead of the response panel. ``transient`` is still a
   valid block kind and ``rate_limit`` is still how ``FailoverReason`` spells a
   429.
6. **The block lands.** Both sites are driven against a real board through the
   real ``block_task``: the card ends ``blocked`` with ``block_kind=transient``,
   the run closed ``blocked`` with the provider text as its summary, and no
   failure counted. A stale run id is refused, a non-429 exhaustion still
   records ``timed_out``, and ``billing`` is not blocked.
7. **A failed chat-path turn is charged by the worker and retried.** The
   charged text is checked against the pinned tree's own
   ``_RESPAWN_BLOCKER_RE``, so a Hermes bump that widens it fails the build.
   The real, patched ``_chat_render_turn`` is run on the ka-next-4 401 result
   (display hooks stubbed) inside a forked stand-in worker the dispatcher
   reaps. It must leave exit code 1 (75 for a rate limit, 1 for ``billing``),
   a comment naming ``failure_reason`` and the provider error, and its own
   ``crashed`` charge: card back in ``ready``, one failure counted,
   ``check_respawn_guard`` returning ``None``, the reaper leaving it alone, and
   the next ``dispatch_once`` respawning it. A second failure reaches
   ``gave_up``. Three workers failing in one reaper pass all stay retryable,
   which the reaper's burst breaker would not allow. A stale run id writes
   nothing and falls back to the reaper. The ``cli.py`` exit is checked for
   placement after ``chat()`` and the summary, inside the ``try`` that
   finalizes the query.

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

TOOL_ROUND = HERMES / "agent" / "turn_tool_round.py"
FINALIZER = HERMES / "agent" / "turn_finalizer.py"
API_ERROR = HERMES / "agent" / "turn_api_error.py"
ITERATION_PREP = HERMES / "agent" / "turn_iteration_prep.py"
CLI = HERMES / "cli.py"
CHAT_MIXIN = HERMES / "hermes_cli" / "cli_chat_turn_mixin.py"

#: ``ast.unparse`` normalises quoting; derive the spellings rather than guess.
BREAK_VERDICT = ast.unparse(ast.parse('_verdict("break")', mode="eval").body)
CONTINUE_VERDICT = ast.unparse(ast.parse('_verdict("continue")', mode="eval").body)
#: The loop locals the nudge path rebinds (``final_response = None`` and
#: ``_turn_exit_reason = "unknown"``) and the one it mutates in place
#: (``append_message(messages, ...)``). All three reach the loop only through
#: ``_verdict``, which must build the verdict from those same names; a verdict
#: built from a copy taken earlier would drop the nudge on the floor with no
#: error.
REBOUND_LOCALS = ("final_response", "_turn_exit_reason")
MUTATED_LOCALS = ("messages",)


def _enclosing_block(tree, stmt):
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody", "handlers"):
            block = getattr(node, field, None)
            if isinstance(block, list) and stmt in block:
                return block
    return None


def _assigns_to(block, target):
    return [
        s
        for s in block
        if isinstance(s, ast.Assign)
        and any(ast.unparse(t) == target for t in s.targets)
    ]


def _def(tree, name):
    return next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )


# --- 1. The nudge is reachable, and it is the exit it claims to be ----------
print("guardrail-halt nudge (agent/turn_tool_round.py):")

round_tree = ast.parse(TOOL_ROUND.read_text())

HALT_TEST = "agent._tool_guardrail_halt_decision is not None"
halt_ifs = [
    node
    for node in ast.walk(round_tree)
    if isinstance(node, ast.If) and ast.unparse(node.test) == HALT_TEST
]
check(
    "the guardrail-halt branch is still a single branch",
    len(halt_ifs) == 1,
    f"found {len(halt_ifs)} — the anchor matched something that moved",
)

nudge_if = None
halt_return = None
if len(halt_ifs) == 1:
    halt = halt_ifs[0]
    nudge_ifs = [
        s
        for s in halt.body
        if isinstance(s, ast.If) and ast.unparse(s.test) == "_kanban_halt_nudge"
    ]
    returns = [
        s
        for s in halt.body
        if isinstance(s, ast.Return) and s.value is not None and ast.unparse(s.value) == BREAK_VERDICT
    ]
    check("the nudge sits inside the halt branch", len(nudge_ifs) == 1)
    check("the halt branch still ends in a break verdict", len(returns) == 1)
    if nudge_ifs:
        nudge_if = nudge_ifs[0]
    if returns:
        halt_return = returns[0]

if nudge_if is not None and halt_return is not None:
    check(
        "the nudge runs before the break verdict it pre-empts",
        nudge_if.lineno < halt_return.lineno,
        f"nudge at {nudge_if.lineno}, break verdict at {halt_return.lineno}",
    )

if nudge_if is not None:
    body = nudge_if.body
    assigns = {
        ast.unparse(t): ast.unparse(s.value)
        for s in body
        if isinstance(s, ast.Assign)
        for t in s.targets
    }

    last = body[-1]
    check(
        "the nudge path hands back a continue verdict rather than falling through",
        isinstance(last, ast.Return)
        and last.value is not None
        and ast.unparse(last.value) == CONTINUE_VERDICT,
        f"last statement is {ast.unparse(last)[:80]}",
    )
    check(
        "the halt decision is cleared before continuing",
        assigns.get("agent._tool_guardrail_halt_decision") == "None",
        "reset_for_turn clears it per turn, not per round — the next "
        "round would re-enter this branch and break anyway",
    )
    check(
        "the halt text is withheld as this turn's answer",
        assigns.get("final_response") == "None",
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

round_def = _def(round_tree, "run_tool_round")
verdict_cls = next(
    (n for n in round_tree.body if isinstance(n, ast.ClassDef) and n.name == "ToolRoundVerdict"),
    None,
)
check(
    "the nudge sits inside run_tool_round, the phase the loop copies verdicts back from",
    round_def is not None and nudge_if is not None and nudge_if in list(ast.walk(round_def)),
)
verdict_fields = (
    {
        ast.unparse(s.target)
        for s in verdict_cls.body
        if isinstance(s, ast.AnnAssign)
    }
    if verdict_cls is not None
    else set()
)
check(
    "the locals the nudge rebinds or mutates are fields of ToolRoundVerdict",
    all(name in verdict_fields for name in REBOUND_LOCALS + MUTATED_LOCALS),
    f"verdict fields: {sorted(verdict_fields)}; the loop never sees a rebinding the verdict does not carry",
)
# Being a field is necessary, not sufficient: ``_verdict`` has to read the
# loop's current binding of each name when it builds the verdict.
verdict_def = _def(round_def, "_verdict") if round_def is not None else None
verdict_kwargs = {}
if verdict_def is not None:
    for node in ast.walk(verdict_def):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ToolRoundVerdict":
            verdict_kwargs = {
                kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg
            }
check(
    "_verdict builds the verdict from the loop's own bindings of those names",
    all(verdict_kwargs.get(name) == name for name in REBOUND_LOCALS + MUTATED_LOCALS),
    f"_verdict keywords: {verdict_kwargs}",
)


# --- 2. It still composes with the upstream helper it borrows ---------------
print("composition with agent.kanban_stop:")

from agent.kanban_stop import build_kanban_stop_nudge  # noqa: E402
from hermes_cli.kanban_guardrail_exit import (  # noqa: E402
    DEFAULT_MAX_NUDGES,
    DETECTOR,
    OUTCOME,
    RATE_LIMIT_BLOCK_KIND,
    RATE_LIMIT_FAILURE_REASONS,
    RATE_LIMIT_REASON_PREFIX,
    RETRIES_EXHAUSTED_EXIT_REASON,
    block_rate_limited_worker,
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


# --- 3. The backstop is inside the funnel every exit passes through ---------
print("terminal-call backstop (agent/turn_finalizer.py):")

finalizer_tree = ast.parse(FINALIZER.read_text())
finalize = _def(finalizer_tree, "finalize_turn")
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
    body = finalize.body
    budget = next(
        (i for i, s in enumerate(body) if "_resolve_budget_fallback" in ast.unparse(s)), None
    )
    leak_check = next(
        (
            i
            for i, s in enumerate(body)
            if isinstance(s, ast.Try) and "_kanban_should_record_missing" in ast.unparse(s)
        ),
        None,
    )
    completed = next(
        (i for i, s in enumerate(body) if _assigns_to([s], "completed")), None
    )
    check(
        "the backstop runs after the budget fallback has recorded an exhausted budget",
        budget is not None and leak_check is not None and budget < leak_check,
        f"budget fallback at {budget}, backstop at {leak_check}",
    )
    check(
        "and before the completed determination",
        completed is not None and leak_check is not None and leak_check < completed,
        f"backstop at {leak_check}, completed at {completed}",
    )
    # Upstream's predicate, read from the image's own _resolve_budget_fallback
    # rather than from this patch's text, so a rewording upstream shows up here
    # as a mismatch rather than being mirrored by a check that only knows the
    # insert. ast.unparse normalises both sides' layout.
    upstream_predicate = None
    resolve = _def(finalizer_tree, "_resolve_budget_fallback")
    if resolve is not None:
        assign = next(
            (
                n
                for n in ast.walk(resolve)
                if isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "budget_exhausted"
            ),
            None,
        )
        if assign is not None:
            upstream_predicate = ast.unparse(assign.value)
    inserted_predicate = None
    if leak_check is not None:
        call = next(
            (
                n
                for n in ast.walk(body[leak_check])
                if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "_kanban_should_record_missing"
            ),
            None,
        )
        keyword = next(
            (k for k in (call.keywords if call is not None else ()) if k.arg == "iteration_limit_fallback"),
            None,
        )
        if keyword is not None:
            inserted_predicate = ast.unparse(keyword.value)
    check(
        "the budget exclusion is upstream's budget_exhausted predicate, read from _resolve_budget_fallback",
        upstream_predicate is not None and inserted_predicate == upstream_predicate,
        f"upstream computes {upstream_predicate!r}; the insert passes {inserted_predicate!r}. "
        "The budget path records timed_out itself; a different predicate double-counts or misses",
    )
    check(
        "the recorder takes the split modules' connect and _record_task_failure",
        "kanban_db_connect" in ast.unparse(finalize) and "kanban_db_dispatch" in ast.unparse(finalize),
        "hermes_cli.kanban_db no longer defines either; an AttributeError here is swallowed",
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


# --- 5. The rate-limit sites sit where a 429 actually goes ------------------
print("rate-limit sites (agent/turn_api_error.py, cli.py, hermes_cli/cli_chat_turn_mixin.py):")

STASH_TARGET = "agent._kube_last_api_failure"
CLASSIFY_TARGET = "classified"
CLI_MARKER = "_kube_block_rate_limited"
CHAT_MARKER = "_kube_block_rate_limited_chat"
PANEL_CALL = "self._chat_print_response_panel(turn, response)"

api_error_tree = ast.parse(API_ERROR.read_text())
stashes = [
    node
    for node in ast.walk(api_error_tree)
    if isinstance(node, ast.Assign)
    and any(ast.unparse(t) == STASH_TARGET for t in node.targets)
    and isinstance(node.value, ast.Tuple)
]
check("the stash is written exactly once", len(stashes) == 1, f"found {len(stashes)}")
if len(stashes) == 1:
    stash = stashes[0]
    stash_try = next(
        (
            node
            for node in ast.walk(api_error_tree)
            if isinstance(node, ast.Try) and stash in node.body
        ),
        None,
    )
    check("the stash is wrapped so it cannot raise out of the handler", stash_try is not None)
    block = _enclosing_block(api_error_tree, stash_try) if stash_try is not None else None
    check("the stash sits in a statement block", block is not None)
    if block is not None:
        classify = [
            s
            for s in _assigns_to(block, CLASSIFY_TARGET)
            if "classify_api_error" in ast.unparse(s.value)
        ]
        check(
            "the stash follows the classification in the same handler",
            len(classify) == 1 and block.index(classify[0]) < block.index(stash_try),
            "the stash would read a `classified` from somewhere else",
        )
        handler = _def(api_error_tree, "handle_api_error")
        check(
            "and that handler is handle_api_error, with the caught exception as api_error",
            handler is not None
            and stash_try in list(ast.walk(handler))
            and "api_error" in {a.arg for a in handler.args.args + handler.args.kwonlyargs},
        )
    check(
        "the stash records the classified reason and the summarised error",
        "classified.reason.value" in ast.unparse(stash)
        and "_summarize_api_error" in ast.unparse(stash),
    )

check(
    "the retries-exhausted exit reason is still spelled the way the branch keys on",
    f'_turn_exit_reason = "{RETRIES_EXHAUSTED_EXIT_REASON}"' in ITERATION_PREP.read_text(),
)

cli_tree = ast.parse(CLI.read_text())
exit_inits = [
    node
    for node in ast.walk(cli_tree)
    if isinstance(node, ast.Assign)
    and any(ast.unparse(t) == "_exit_code" for t in node.targets)
    and ast.unparse(node.value) == "0"
]
check(
    "the worker's exit-code block is still a single site",
    len(exit_inits) == 1,
    f"found {len(exit_inits)}",
)
if len(exit_inits) == 1:
    exit_init = exit_inits[0]
    block = _enclosing_block(cli_tree, exit_init)
    check("the exit-code block sits in a statement block", block is not None)
    if block is not None:
        index = block.index(exit_init)
        before = block[:index]
        after = block[index + 1 :]
        blockers = [
            s
            for s in before
            if isinstance(s, ast.Try) and CLI_MARKER in ast.unparse(s)
        ]
        check(
            "the rate-limit block runs before the exit code is decided",
            len(blockers) == 1,
            "a block after sys.exit never runs",
        )
        if blockers:
            check(
                "and it cannot change the exit code or exit itself",
                "_exit_code" not in ast.unparse(blockers[0])
                and "sys.exit" not in ast.unparse(blockers[0]),
                "exit 75 is the reaper's contract; the block is additive",
            )
        decision = next((s for s in after if isinstance(s, ast.If)), None)
        check(
            "the stock decision still routes rate_limit to the rate-limit exit code",
            decision is not None
            and "rate_limit" in ast.unparse(decision)
            and "KANBAN_RATE_LIMIT_EXIT_CODE" in ast.unparse(decision),
        )

chat_tree = ast.parse(CHAT_MIXIN.read_text())
render = _def(chat_tree, "_chat_render_turn")
check("the chat-render site is inside _chat_render_turn", render is not None and CHAT_MARKER in ast.unparse(render))
if render is not None:
    chat_blockers = [
        s
        for s in render.body
        if isinstance(s, ast.If) and CHAT_MARKER in ast.unparse(s)
    ]
    panel_calls = [
        s
        for s in render.body
        if isinstance(s, ast.Expr) and ast.unparse(s) == PANEL_CALL
    ]
    check(
        "the chat-render site runs where the failed result is still in hand, "
        "ahead of the response panel",
        len(chat_blockers) == 1
        and len(panel_calls) == 1
        and render.body.index(chat_blockers[0]) < render.body.index(panel_calls[0]),
    )
    check(
        "the chat-render site is gated on the worker's task id",
        chat_blockers and "HERMES_KANBAN_TASK" in ast.unparse(chat_blockers[0]),
    )
    panel = _def(chat_tree, "_chat_print_response_panel")
    check(
        "the response panel is still where the billing call-to-action reads failure_reason",
        panel is not None
        and "failure_reason" in ast.unparse(panel)
        and "billing" in ast.unparse(panel),
        "the site was placed ahead of the last consumer of the failed result; that consumer moved",
    )

from agent.error_classifier import FailoverReason  # noqa: E402
from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
from hermes_cli import kanban_db_dispatch as KD  # noqa: E402

check(
    "transient is still a valid block kind",
    RATE_LIMIT_BLOCK_KIND in K.VALID_BLOCK_KINDS,
    f"VALID_BLOCK_KINDS={sorted(K.VALID_BLOCK_KINDS)}",
)
check(
    "rate_limit is still how FailoverReason spells a 429",
    FailoverReason.rate_limit.value in RATE_LIMIT_FAILURE_REASONS,
)
check(
    "billing is deliberately not blocked",
    FailoverReason.billing.value not in RATE_LIMIT_FAILURE_REASONS,
    "a credit wall does not clear on its own; it is not blocked transient",
)


# --- 4. The board write lands -----------------------------------------------
print("board write:")

TMP = Path(tempfile.mkdtemp())
DB = TMP / "kanban.db"


def board():
    return KC.connect(DB)


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
    record_failure=KD._record_task_failure,
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

# Nothing to hand back twice. The board decides, not the transcript: the
# transcript can lie about a terminal call after compaction, the board cannot.
# The status alone is not enough, though — see the re-run case below.
check("the card is no longer running", not task_is_still_running(conn, card))
before = len(events(conn, card))
check(
    "a card the board has already moved is left alone",
    record_missing_terminal_call(
        task_id=card,
        turn_exit_reason="guardrail_halt",
        connect=board,
        record_failure=KD._record_task_failure,
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
        record_failure=KD._record_task_failure,
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
        record_failure=KD._record_task_failure,
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

# A worker that blocked its own card keeps running until it ends a turn in
# text; by then the card can be running again under the next worker's run.
# Its backstop must leave that run alone — _record_task_failure would close it.
TEXT_EXIT = "text_response(finish_reason=stop)"
conn = board()
moved = K.create_task(conn, title="Check PDB coverage (blocked, re-run)", assignee="platform")
K.recompute_ready(conn)
check("the first worker's card is claimed", K.claim_task(conn, moved))
first_run = K.get_task(conn, moved).current_run_id
check(
    "the first worker blocks its own run",
    K.block_task(conn, moved, reason="waiting on a sibling card", expected_run_id=first_run),
)
check("and the card is unblocked", K.unblock_task(conn, moved))
K.recompute_ready(conn)
check("the next worker claims it", K.claim_task(conn, moved))
second_run = K.get_task(conn, moved).current_run_id
check("under a new run", second_run != first_run, f"run {first_run} -> {second_run}")
check(
    "the first worker's backstop records nothing",
    record_missing_terminal_call(
        task_id=moved,
        turn_exit_reason=TEXT_EXIT,
        connect=board,
        record_failure=KD._record_task_failure,
        run_id=first_run,
        stop_nudges=0,
    )
    is False,
)
conn = board()
after = row(conn, moved)
check(
    "the next worker's run is still open and uncharged",
    after["status"] == "running"
    and open_runs(conn, moved) == 1
    and K.get_task(conn, moved).current_run_id == second_run
    and (after["consecutive_failures"] or 0) == 0,
    f"status={after['status']!r} open={open_runs(conn, moved)} "
    f"failures={after['consecutive_failures']!r}",
)
check(
    "the next worker's own leak is still charged to its run",
    record_missing_terminal_call(
        task_id=moved,
        turn_exit_reason=TEXT_EXIT,
        connect=board,
        record_failure=KD._record_task_failure,
        run_id=second_run,
        stop_nudges=2,
    )
    is True,
)
conn = board()
text_error = row(conn, moved)["last_failure_error"] or ""
check(
    "a text exit says the model replied in text, and how often it was nudged",
    text_error.startswith("model replied in text")
    and f"turn_exit_reason={TEXT_EXIT}" in text_error
    and "kanban nudges sent: 2" in text_error,
    f"last_failure_error={text_error!r}",
)

# The read above runs before _record_task_failure opens its own transaction.
# Hand the card over in exactly that gap: the run check inside the write has
# to refuse the charge, or the new run is closed as before.
params = inspect.signature(KD._record_task_failure).parameters
check(
    "_record_task_failure takes expected_run_id, defaulting to no check",
    "expected_run_id" in params and params["expected_run_id"].default is None,
    f"parameters: {list(params)}",
)
conn = board()
raced = K.create_task(conn, title="Check PDB coverage (hand-over mid-charge)", assignee="platform")
K.recompute_ready(conn)
K.claim_task(conn, raced)
raced_run = K.get_task(conn, raced).current_run_id
handed_over = {}


def hand_over_then_record(conn, task_id, **kwargs):
    K.block_task(conn, task_id, reason="hand-over", expected_run_id=raced_run)
    K.unblock_task(conn, task_id)
    K.recompute_ready(conn)
    K.claim_task(conn, task_id)
    handed_over["run"] = K.get_task(conn, task_id).current_run_id
    return KD._record_task_failure(conn, task_id, **kwargs)


raced_recorded = record_missing_terminal_call(
    task_id=raced,
    turn_exit_reason=TEXT_EXIT,
    connect=board,
    record_failure=hand_over_then_record,
    run_id=raced_run,
    stop_nudges=0,
)
check(
    "and the backstop does not report a charge the write refused",
    raced_recorded is False,
    f"returned {raced_recorded!r}; the finalizer would log 'recorded'",
)
conn = board()
after = row(conn, raced)
check(
    "a hand-over between the read and the write leaves the new run open",
    handed_over.get("run") not in (None, raced_run)
    and after["status"] == "running"
    and open_runs(conn, raced) == 1
    and K.get_task(conn, raced).current_run_id == handed_over.get("run")
    and (after["consecutive_failures"] or 0) == 0,
    f"handed over to {handed_over.get('run')!r}; status={after['status']!r} "
    f"open={open_runs(conn, raced)} failures={after['consecutive_failures']!r}",
)

# The same hand-over, and the new worker completes the card before the
# read-back: the card is no longer running, yet nothing of this worker's was
# charged, so the backstop must still say so.
conn = board()
closed = K.create_task(conn, title="Check PDB coverage (hand-over, then done)", assignee="platform")
K.recompute_ready(conn)
K.claim_task(conn, closed)
closed_run = K.get_task(conn, closed).current_run_id


def hand_over_then_new_worker_completes(conn, task_id, **kwargs):
    K.block_task(conn, task_id, reason="hand-over", expected_run_id=closed_run)
    K.unblock_task(conn, task_id)
    K.recompute_ready(conn)
    K.claim_task(conn, task_id)
    new_run = K.get_task(conn, task_id).current_run_id
    tripped = KD._record_task_failure(conn, task_id, **kwargs)
    K.complete_task(conn, task_id, result="done by the next worker", expected_run_id=new_run)
    return tripped


closed_recorded = record_missing_terminal_call(
    task_id=closed,
    turn_exit_reason=TEXT_EXIT,
    connect=board,
    record_failure=hand_over_then_new_worker_completes,
    run_id=closed_run,
    stop_nudges=0,
)
check(
    "a refused charge is not reported even after the new worker closes the card",
    closed_recorded is False and row(board(), closed)["status"] == "done",
    f"returned {closed_recorded!r}; status={row(board(), closed)['status']!r}",
)

# The text is read back by upstream: a respawn-blocker word parks the card as
# an auth/quota wall, and "protocol violation" feeds the clean-exit streak.
for reason in (TEXT_EXIT,) + EXIT_REASONS:
    text = missing_terminal_error(reason, 2)
    check(
        f"the {reason} text is not read as a respawn blocker",
        not KD._RESPAWN_BLOCKER_RE.search(text),
        f"matched {KD._RESPAWN_BLOCKER_RE.search(text)!r} in {text!r}",
    )
    check(
        f"the {reason} text is not read as a protocol violation",
        "protocol violation" not in text,
    )


# --- 6. The rate-limit block lands ------------------------------------------
print("rate-limit block:")

STORM_ERROR = (
    "Error code: 429 - {'error': {'message': 'litellm.RateLimitError: "
    'VertexAIException - {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
    "\"retryDelay\": \"54s\"}]}}', 'type': None, 'code': '429'}}"
)
STORM_RESULT = {"failed": True, "failure_reason": "rate_limit", "error": STORM_ERROR}


def block_row(conn, tid):
    return conn.execute(
        "SELECT status, claim_lock, block_kind, consecutive_failures "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def last_run(conn, tid):
    return conn.execute(
        "SELECT status, outcome, summary, ended_at FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


def claimed_card(title):
    conn = board()
    tid = K.create_task(conn, title=title, assignee="platform")
    K.recompute_ready(conn)
    check(f"{title!r} is claimed the way the dispatcher claims it", K.claim_task(conn, tid))
    run_id = K.get_task(conn, tid).current_run_id
    check("the dispatcher opened a run for it", isinstance(run_id, int))
    return tid, run_id


# The cli.py site, with the environment the dispatcher gives a worker.
cli_card, cli_run = claimed_card("Summarise fleet posture (cli.py site)")
did = block_rate_limited_worker(
    STORM_RESULT,
    connect=board,
    block_task=K.block_task,
    environ={"HERMES_KANBAN_TASK": cli_card, "HERMES_KANBAN_RUN_ID": str(cli_run)},
    cron_run=False,
    delegated_child=False,
)
check("the cli.py site blocks the card", did is True)
conn = board()
after = block_row(conn, cli_card)
check(
    "the card is blocked, not counted",
    after["status"] == "blocked"
    and after["claim_lock"] is None
    and (after["consecutive_failures"] or 0) == 0,
    f"status={after['status']!r} lock={after['claim_lock']!r} "
    f"failures={after['consecutive_failures']!r}",
)
check(
    f"the block kind is {RATE_LIMIT_BLOCK_KIND}",
    after["block_kind"] == RATE_LIMIT_BLOCK_KIND,
    f"block_kind={after['block_kind']!r}",
)
run = last_run(conn, cli_card)
check(
    "the run is closed blocked",
    run is not None and run["ended_at"] is not None and run["outcome"] == "blocked",
    f"run={dict(run) if run else None}",
)
check(
    "the provider's text is the run's summary",
    run is not None
    and (run["summary"] or "").startswith(RATE_LIMIT_REASON_PREFIX)
    and "RESOURCE_EXHAUSTED" in (run["summary"] or ""),
    f"summary={run['summary'] if run else None!r}",
)
check(
    "the reason reaches the blocked event",
    any(
        k == "blocked" and "RESOURCE_EXHAUSTED" in (p or "")
        for k, p in events(conn, cli_card)
    ),
    f"events: {[k for k, _ in events(conn, cli_card)]}",
)
check(
    "a second attempt finds nothing to do",
    block_rate_limited_worker(
        STORM_RESULT,
        connect=board,
        block_task=K.block_task,
        environ={"HERMES_KANBAN_TASK": cli_card, "HERMES_KANBAN_RUN_ID": str(cli_run)},
        cron_run=False,
        delegated_child=False,
    )
    is False,
)

# The site's own default opener, the one the shipped cli.py and chat-render
# paths use when no connect is injected: it has to be the split module's real
# connect on this tree, not the revert-scheduled compat forward.
from hermes_cli.kanban_guardrail_exit import _default_connect  # noqa: E402

check(
    "the default opener is the split module's connect",
    _default_connect() is KC.connect,
    f"resolved to {_default_connect()!r}",
)

# The finalize_turn site: retries exhausted with no response, after a 429.
fin_card, fin_run = claimed_card("Audit node pools (finalize_turn site)")
did = record_missing_terminal_call(
    task_id=fin_card,
    turn_exit_reason=RETRIES_EXHAUSTED_EXIT_REASON,
    connect=board,
    record_failure=KD._record_task_failure,
    block_task=K.block_task,
    last_api_failure=("rate_limit", STORM_ERROR),
    run_id=fin_run,
)
check("the finalize_turn site blocks the card", did is True)
conn = board()
after = block_row(conn, fin_card)
check(
    "blocked transient, no failure counted",
    after["status"] == "blocked"
    and after["block_kind"] == RATE_LIMIT_BLOCK_KIND
    and (after["consecutive_failures"] or 0) == 0,
    f"status={after['status']!r} kind={after['block_kind']!r} "
    f"failures={after['consecutive_failures']!r}",
)
check(
    "no timed_out event was written for it",
    OUTCOME not in [k for k, _ in events(conn, fin_card)],
)

# A stale run id: the dispatcher reclaimed and re-ran the card underneath this
# worker, so the block must be refused rather than land on someone else's run.
stale_card, stale_run = claimed_card("Rotate credentials (stale run)")
did = record_missing_terminal_call(
    task_id=stale_card,
    turn_exit_reason=RETRIES_EXHAUSTED_EXIT_REASON,
    connect=board,
    record_failure=KD._record_task_failure,
    block_task=K.block_task,
    last_api_failure=("rate_limit", STORM_ERROR),
    run_id=stale_run + 1000,
)
check("a stale run id is refused", did is False)
check(
    "and the card is still running for its real worker",
    block_row(board(), stale_card)["status"] == "running",
)

# A non-429 exhaustion still records the counted timed_out.
other_card, other_run = claimed_card("Reconcile IAM bindings (server error)")
did = record_missing_terminal_call(
    task_id=other_card,
    turn_exit_reason=RETRIES_EXHAUSTED_EXIT_REASON,
    connect=board,
    record_failure=KD._record_task_failure,
    block_task=K.block_task,
    last_api_failure=("server_error", "Error code: 503"),
    run_id=other_run,
)
check("a non-429 exhaustion is still recorded", did is True)
conn = board()
after = block_row(conn, other_card)
check(
    f"and it is the counted {OUTCOME}, not a block",
    (after["consecutive_failures"] or 0) == 1
    and OUTCOME in [k for k, _ in events(conn, other_card)]
    and after["block_kind"] != RATE_LIMIT_BLOCK_KIND,
    f"failures={after['consecutive_failures']!r} kind={after['block_kind']!r} "
    f"events={[k for k, _ in events(conn, other_card)]}",
)

# The recurrence rule the module docstring states, re-derived for v2026.9.14's
# ``_route_block``: ``unblock_task`` deliberately keeps ``block_kind`` and
# ``block_recurrences``, so a second block for the same kind counts
# ``recurrences = 2``, which meets ``BLOCK_RECURRENCE_LIMIT`` (2) and routes the
# card to ``triage`` with a ``block_loop_detected`` event instead of ``blocked``.
# If upstream changes that, the docstring lies.
conn = board()
check("the cli.py-site card can be unblocked", K.unblock_task(conn, cli_card))
K.recompute_ready(conn)
check("and re-claimed", K.claim_task(conn, cli_card))
re_run = K.get_task(conn, cli_card).current_run_id
did = block_rate_limited_worker(
    STORM_RESULT,
    connect=board,
    block_task=K.block_task,
    environ={"HERMES_KANBAN_TASK": cli_card, "HERMES_KANBAN_RUN_ID": str(re_run)},
    cron_run=False,
    delegated_child=False,
)
check("a second storm on the same card still writes", did is True)
conn = board()
after = block_row(conn, cli_card)
check(
    "and upstream's loop breaker routes it to triage, as the docstring says",
    after["status"] == "triage"
    and after["block_kind"] == RATE_LIMIT_BLOCK_KIND
    and "block_loop_detected" in [k for k, _ in events(conn, cli_card)],
    f"status={after['status']!r} kind={after['block_kind']!r} "
    f"events={[k for k, _ in events(conn, cli_card)]}; "
    f"BLOCK_RECURRENCE_LIMIT={getattr(K, 'BLOCK_RECURRENCE_LIMIT', None)!r}",
)

# billing keeps the stock exit at both sites.
bill_card, bill_run = claimed_card("Estimate egress cost (billing)")
did = block_rate_limited_worker(
    {"failed": True, "failure_reason": "billing", "error": "Error code: 402"},
    connect=board,
    block_task=K.block_task,
    environ={"HERMES_KANBAN_TASK": bill_card, "HERMES_KANBAN_RUN_ID": str(bill_run)},
    cron_run=False,
    delegated_child=False,
)
check("billing is not blocked", did is False)
check(
    "and the card is untouched",
    block_row(board(), bill_card)["status"] == "running",
)


# --- 7. A failed chat-path turn exits nonzero and the dispatcher retries it -
print("failed chat-path turn (hermes_cli/cli_chat_turn_mixin.py, cli.py):")

import types  # noqa: E402

from hermes_cli import cli_chat_turn_mixin as CHAT_MODULE  # noqa: E402
from hermes_cli import kanban_guardrail_exit as GUARD  # noqa: E402

#: The ka-next-4 result (t_80ff98c9, run 28), as nonretryable_client_error_result
#: built it: the summary Hermes made of the 401, failure_reason from the
#: classifier.
AUTH_ERROR = (
    "HTTP 401: litellm.AuthenticationError: Vertex_aiException - "
    '{"error": {"code": 401, "message": "Rejected by RpcSecurityPolicy for '
    "google.cloud.aiplatform.internal.PredictionService.GenerateContent: mint "
    'used too late"}}'
)
AUTH_RESULT = {
    "final_response": AUTH_ERROR,
    "messages": [],
    "api_calls": 3,
    "completed": False,
    "failed": True,
    "error": AUTH_ERROR,
    "failure_reason": "auth",
    "failure_retryable": False,
}
EXIT_ATTR = "_kube_kanban_exit_code"
SINGLE_QUERY_EXIT = "sys.exit(_kube_kanban_exit_code)"

check(
    "a 401 still classifies as the failure_reason this fixture carries",
    FailoverReason.auth.value == AUTH_RESULT["failure_reason"],
)
check(
    "the quiet path's rate-limit exit code is still the one the chat path mirrors",
    getattr(GUARD, "RATE_LIMIT_EXIT_CODE", None) == K.KANBAN_RATE_LIMIT_EXIT_CODE,
    f"module={getattr(GUARD, 'RATE_LIMIT_EXIT_CODE', None)!r} "
    f"upstream={K.KANBAN_RATE_LIMIT_EXIT_CODE!r}",
)
check(
    "the module and the inserts agree on where the exit code is left",
    getattr(GUARD, "EXIT_CODE_ATTR", None) == EXIT_ATTR,
)

# Placement: the exit sits after chat() and the summary, inside the try whose
# finally runs _finalize_single_query, as the -Q branch's sys.exit does.
single = _def(cli_tree, "_run_single_query_mode")
exits = [
    n for n in ast.walk(single) if isinstance(n, ast.If) and SINGLE_QUERY_EXIT in ast.unparse(n)
] if single is not None else []
check("the single-query exit is in _run_single_query_mode, once", len(exits) == 1, f"found {len(exits)}")
if len(exits) == 1:
    block = _enclosing_block(cli_tree, exits[0])
    calls = [ast.unparse(s) for s in (block or [])]
    idx = block.index(exits[0]) if block else -1
    check(
        "and it runs after chat() and the exit summary",
        block is not None
        and any("cli.chat(" in c for c in calls[:idx])
        and any("cli._print_exit_summary(" in c for c in calls[:idx]),
        f"statements before it: {calls[:idx]}",
    )
    owner = next(
        (n for n in ast.walk(single) if isinstance(n, ast.Try) and block is n.body),
        None,
    )
    check(
        "inside the try whose finally finalizes the single query",
        owner is not None and "_finalize_single_query" in ast.unparse(owner.finalbody),
    )
    check(
        "and nothing there makes a worker with no turn exit non-zero",
        "single_query_exit_code" not in "\n".join(calls)
        and "_kube_kanban_turn_seen" not in "\n".join(calls),
    )


class _Turn:
    def __init__(self, result):
        self.result = result
        self.use_streaming_tts = False


class _Self:
    """Just enough CLI for _chat_render_turn: every display hook is a no-op."""

    _voice_continuous = False
    _voice_tts = False

    def __init__(self):
        self.agent = types.SimpleNamespace(max_iterations=90)
        self.panels = []

    def _chat_resolve_interrupt(self, *a):
        return None, False

    def _chat_print_response_panel(self, turn, response):
        self.panels.append(response)

    def __getattr__(self, name):
        if name.startswith("_chat_") or name in ("_emit_focus_recovery_line", "_ring_bell"):
            return lambda *a, **k: None
        raise AttributeError(name)


def render_failed_turn(result, environ):
    """Run the real, patched _chat_render_turn; return the exit code it left."""
    fake_cli = types.ModuleType("cli")
    fake_cli._DIM = fake_cli._RST = ""
    fake_cli._cprint = lambda *a, **k: None
    fake_cli._suspend_output_history = lambda: __import__("contextlib").nullcontext()
    saved_cli = sys.modules.get("cli")
    saved_env = dict(os.environ)
    sys.modules["cli"] = fake_cli
    os.environ.update(environ)
    me = _Self()
    try:
        CHAT_MODULE.CLIChatTurnMixin._chat_render_turn(me, _Turn(result), None, None)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        if saved_cli is None:
            sys.modules.pop("cli", None)
        else:
            sys.modules["cli"] = saved_cli
    check("the response panel still prints the failure", me.panels == [result["final_response"]])
    return me.__dict__.get(EXIT_ATTR)


def spawn_worker(conn, tid):
    """Fork a stand-in worker process and record its pid the way the dispatcher
    does at spawn. It waits for ``release`` and exits with the code written
    there, so the exit lands after the board writes, as a real worker's does."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_end)
        data = os.read(read_end, 8)
        os._exit(int(data or b"0"))
    os.close(read_end)
    KD._set_worker_pid(conn, tid, pid)
    return pid, write_end


def exit_worker(worker, code):
    """Let the stand-in exit ``code`` and have the dispatcher reap it."""
    pid, write_end = worker
    os.write(write_end, str(code or 0).encode())
    os.close(write_end)
    for _ in range(500):
        if pid in KD.reap_worker_zombies():
            return
        __import__("time").sleep(0.01)


def env_for(tid, run_id):
    return {
        "HERMES_KANBAN_TASK": tid,
        "HERMES_KANBAN_RUN_ID": str(run_id),
        "HERMES_KANBAN_DB": str(DB),
        "HERMES_PROFILE": "platform",
    }


def run_failed_worker(tid, run_id, result=None):
    """Spawn, fail the turn through the real chat-render site, exit, reap.
    Returns the exit code the site left."""
    conn = board()
    worker = spawn_worker(conn, tid)
    code = render_failed_turn(result or AUTH_RESULT, env_for(tid, run_id))
    exit_worker(worker, code)
    return code


def respawn_once(tid):
    saved = KD._profile_exists_fn
    KD._profile_exists_fn = lambda: (lambda name: True)
    spawned = []
    try:
        KD.dispatch_once(board(), spawn_fn=lambda task, ws, board=None: spawned.append(task.id))
    finally:
        KD._profile_exists_fn = saved
    return tid in spawned


os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"

# The charged text, against the pinned tree's own respawn-guard pattern. A
# Hermes bump that widens _RESPAWN_BLOCKER_RE to match it fails the build here,
# instead of stranding every failed card in ready.
charged = getattr(GUARD, "FAILED_TURN_ERROR", None)
check(
    "the charged failure text is not a respawn blocker in this tree",
    charged is not None and not KD._RESPAWN_BLOCKER_RE.search(charged),
    f"FAILED_TURN_ERROR={charged!r}",
)
check(
    "and is not counted as a protocol violation",
    charged is not None and "protocol violation" not in charged,
)

# One worker, the ka-next-4 401.
auth_card, auth_run = claimed_card("Count pods in kubeagents-system (401)")
code = run_failed_worker(auth_card, auth_run)
check(
    "a failed kanban turn on the chat path exits 1, as the -Q path does",
    code == 1,
    f"exit code {code!r} (None is the stock chat path: exit 0)",
)
crashed = KD.detect_crashed_workers(board())
conn = board()
after = row(conn, auth_card)
run = conn.execute(
    "SELECT outcome, error, metadata FROM task_runs WHERE id = ?", (auth_run,)
).fetchone()
comments = [
    r["body"]
    for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (auth_card,))
]
check(
    "the reason is on the card as a comment naming failure_reason and the provider error",
    len(comments) == 1
    and "failure_reason=auth" in comments[0]
    and "mint used too late" in comments[0]
    and f"Run {auth_run} " in comments[0],
    f"comments: {comments}",
)
check(
    "the worker charged its own run crashed, not a protocol violation",
    run["outcome"] == "crashed" and run["error"] == charged
    and "protocol_violation" not in (run["metadata"] or ""),
    f"outcome={run['outcome']!r} error={run['error']!r} metadata={run['metadata']!r}",
)
check(
    "the reaper left it alone: the card was no longer running",
    auth_card not in crashed,
    f"crashed={crashed}",
)
check(
    "the failure is counted once and the card is back in ready",
    after["consecutive_failures"] == 1 and after["status"] == "ready",
    f"failures={after['consecutive_failures']!r} status={after['status']!r}",
)
check(
    "last_failure_error is the charged text",
    after["last_failure_error"] == charged,
    f"last_failure_error={after['last_failure_error']!r}",
)
check(
    "check_respawn_guard lets the card run again",
    KD.check_respawn_guard(conn, auth_card) is None,
    f"guard={KD.check_respawn_guard(conn, auth_card)!r}",
)
check("and the next dispatch_once respawns it", respawn_once(auth_card))

# At max_retries the card gives up. respawn_once claimed it again; fail that run.
conn = board()
second_run = K.get_task(conn, auth_card).current_run_id
run_failed_worker(auth_card, second_run)
KD.detect_crashed_workers(board())
conn = board()
gave_up = [p for k, p in events(conn, auth_card) if k == "gave_up"]
check(
    "a second consecutive failure reaches gave_up (max_retries defaults to 2)",
    row(conn, auth_card)["status"] == "blocked" and len(gave_up) == 1,
    f"status={row(conn, auth_card)['status']!r} gave_up={gave_up}",
)
check(
    "and the gave_up event names failure_reason and this detector",
    gave_up and '"failure_reason": "auth"' in gave_up[0] and DETECTOR in gave_up[0],
    f"payload={gave_up[:1]}",
)

# A burst: three workers fail in the same reaper pass, as every in-flight
# worker did when the shared credential expired. The reaper's burst breaker
# would give up on all three at once; charged by the workers, each stays
# retryable.
burst = [claimed_card(f"Burst card {i} (401)") for i in range(3)]
workers = []
conn = board()
for tid, run_id in burst:
    workers.append(spawn_worker(conn, tid))
codes = [render_failed_turn(AUTH_RESULT, env_for(tid, run_id)) for tid, run_id in burst]
for worker, c in zip(workers, codes):
    exit_worker(worker, c)
crashed = KD.detect_crashed_workers(board())
conn = board()
rows = [row(conn, tid) for tid, _ in burst]
check(
    "three failed workers in one pass all stay retryable (no gave_up on attempt 1)",
    all(r["status"] == "ready" and r["consecutive_failures"] == 1 for r in rows)
    and not any(k == "gave_up" for tid, _ in burst for k, _ in events(conn, tid)),
    f"rows={[(r['status'], r['consecutive_failures']) for r in rows]} codes={codes} crashed={crashed}",
)

# A charge refused because the card changed hands: no comment, no charge, and
# the exit falls back to the reaper, which counts it as it always has.
moved_card, moved_run = claimed_card("Rotate credentials (card moved)")
conn = board()
worker = spawn_worker(conn, moved_card)
code = render_failed_turn(AUTH_RESULT, env_for(moved_card, moved_run + 1000))
conn = board()
# A charge refused inside its transaction: the run moved after the worker's
# read. Nothing lands, so no comment may claim the run counted.
race_card, race_run = claimed_card("Rotate credentials (run moved mid-charge)")
_race_comments = []
_race_landed = GUARD.record_failed_turn(
    task_id=race_card,
    run_id=race_run,
    comment="should not appear",
    author="platform",
    failure_reason="auth",
    failure_retryable=False,
    connect=board,
    add_comment=lambda conn, tid, author, body: _race_comments.append(body),
    record_failure=lambda conn, tid, **kw: KD._record_task_failure(
        conn, tid, **dict(kw, expected_run_id=race_run + 1000)
    ),
) if hasattr(GUARD, "record_failed_turn") else None
check(
    "a charge refused inside its transaction leaves no comment and is reported",
    _race_landed is False
    and _race_comments == []
    and row(board(), race_card)["status"] == "running",
    f"landed={_race_landed!r} comments={_race_comments} status={row(board(), race_card)['status']!r}",
)

check(
    "a stale run id writes nothing",
    row(conn, moved_card)["status"] == "running"
    and not conn.execute("SELECT 1 FROM task_comments WHERE task_id = ?", (moved_card,)).fetchone(),
)
exit_worker(worker, code)
crashed = KD.detect_crashed_workers(board())
check(
    "and its exit 1 is booked crashed by the reaper instead",
    code == 1 and moved_card in crashed and row(board(), moved_card)["consecutive_failures"] == 1,
    f"code={code!r} crashed={crashed}",
)

# billing exits 1 on the chat path, not the quiet path's 75: 75 would respawn
# a credit wall every cooldown with no limit.
bill2_card, bill2_run = claimed_card("Estimate egress cost (billing, chat path)")
code = render_failed_turn(
    {"final_response": "Error code: 402", "failed": True, "failure_reason": "billing", "error": "Error code: 402"},
    env_for(bill2_card, bill2_run),
)
check(
    "billing exits 1 on the chat path and is charged like any other failure",
    code == 1 and row(board(), bill2_card)["consecutive_failures"] == 1,
    f"exit code {code!r}",
)

# A rate limit keeps its transient block and mirrors the quiet path's 75.
rl_card, rl_run = claimed_card("Summarise fleet posture (chat path)")
code = render_failed_turn(
    dict(STORM_RESULT, final_response=STORM_ERROR),
    env_for(rl_card, rl_run),
)
conn = board()
check(
    "a rate limit is blocked transient and exits 75",
    code == K.KANBAN_RATE_LIMIT_EXIT_CODE and block_row(conn, rl_card)["status"] == "blocked",
    f"exit code {code!r} status={block_row(conn, rl_card)['status']!r}",
)
check(
    "and gets no comment or charge: the block carries the text",
    not conn.execute("SELECT 1 FROM task_comments WHERE task_id = ?", (rl_card,)).fetchone()
    and not (block_row(conn, rl_card)["consecutive_failures"] or 0),
)

# Not a worker: the chat path exits as it always has.
code = render_failed_turn(AUTH_RESULT, {"HERMES_KANBAN_TASK": ""})
check("a non-worker chat -q keeps its exit", code is None, f"exit code {code!r}")


print()
if FAILURES:
    print(f"verify_kanban_guardrail_exit: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_guardrail_exit: all checks passed")
