#!/usr/bin/env python3
"""Build gate for the kanban guardrail-exit patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_guardrail_exit.py``. The applier only proves its five anchors
matched exactly once; that says nothing about whether the inserted code is
reachable, whether it still composes with the upstream helpers it calls, or
whether the board write it makes lands.

Six things are checked, and each one is a way the patch could match its anchor
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
   released, run closed, failure counted, exit reason legible in the event. Plus
   the three cases that must *not* write — a card the board no longer shows as
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
   records ``timed_out``, and ``billing`` is left to the stock exit.

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
    "a credit wall does not clear on its own; it keeps the stock exit",
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
check("billing is left to the stock exit", did is False)
check(
    "and the card is untouched",
    block_row(board(), bill_card)["status"] == "running",
)


print()
if FAILURES:
    print(f"verify_kanban_guardrail_exit: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_guardrail_exit: all checks passed")
