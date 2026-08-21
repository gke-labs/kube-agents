#!/usr/bin/env python3
"""Wire tools/cron_run_scope.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One AST locator
and nine anchored string replacements across three files is past the point
where an inline ``python3 -c`` stays readable, so the edits live here — but the
guarantee is the same as the other patches in the Dockerfile: every anchor must
be found the number of times expected, every edited file must still parse, and
anything else fails the build loudly rather than shipping a half-patched image.

Why each edit is needed is documented in the module docstring of
``deploy/docker/patches/cron_run_scope.py``. Usage::

    python3 apply_cron_run_scope.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

# --- cron/scheduler.py: stop discarding the run's own report ----------------
#
# The out-param goes on the end of run_one_job's keyword-only parameters.
# Located rather than spelled out: this used to be a literal anchor on the
# whole one-line signature, and v2026.8.13 both wrapped that line onto three
# and added an ``extra_prompt`` of its own, either of which broke the build for
# a change the patch has no opinion about. What it does have an opinion about
# is that this is still the shared execute→deliver→mark body the ticker and the
# cronjob tool both call, which is what expect_keyword_only asserts.

SCHEDULER_OUTCOME_PARAM = ", outcome=None"

#: Parameters the run entry point must still take for it to be the one this
#: patch means. Not the whole signature: upstream may add to it, and this patch
#: does not care.
SCHEDULER_EXPECTED_PARAMS = ("adapters", "loop", "verbose")

#: Text only a successful run leaves behind. The out-param is inserted rather
#: than substituted for an anchor, so the count check cannot tell a fresh file
#: from one this has already run against, and a second pass would append a
#: second ``outcome=None``.
SCHEDULER_PATCHED_MARKER = 'outcome["response"] = final_response'

SCHEDULER_SAVE_OUTPUT = '            output_file = save_job_output(job["id"], output)\n'

SCHEDULER_SAVE_OUTPUT_PATCHED = (
    SCHEDULER_SAVE_OUTPUT
    + "            # kube-agents patch: see tools/cron_run_scope.py\n"
    + "            if outcome is not None:\n"
    + "                # str(): save_job_output returns a pathlib.Path, and this\n"
    + "                # value ends up inside the json.dumps of the run action.\n"
    + '                outcome["output_file"] = str(output_file)\n'
)

# The success-path tail. Anchored on the multi-line finish_execution call
# rather than the whole tail: the except-path below it passes success=False on
# one line and returns False, so this block appears exactly once, and keeping
# the anchor to the lines the patch actually inserts against means upstream
# churn in the delivery_outcome branches above does not break it.
SCHEDULER_TAIL = (
    "        finish_execution(\n"
    "            execution_id,\n"
    "            success=success,\n"
    "            error=error,\n"
    "            delivery_outcome=delivery_outcome,\n"
    "        )\n"
    "        return True\n"
)

SCHEDULER_TAIL_PATCHED = (
    "        finish_execution(\n"
    "            execution_id,\n"
    "            success=success,\n"
    "            error=error,\n"
    "            delivery_outcome=delivery_outcome,\n"
    "        )\n"
    "        # kube-agents patch: hand the run's own report back to whoever\n"
    "        # dispatched it. See tools/cron_run_scope.py.\n"
    "        if outcome is not None:\n"
    '            outcome["response"] = final_response\n'
    '            outcome["success"] = success\n'
    '            outcome["error"] = error\n'
    '            outcome["delivery_error"] = delivery_error\n'
    "        return True\n"
)

# --- tools/cronjob_tools.py: return the report to the caller ----------------

CRONJOB_IMPORT_ANCHOR = "def _notify_provider_jobs_changed_safe() -> None:"

CRONJOB_IMPORT_PATCHED = (
    "# kube-agents patch: see tools/cron_run_scope.py\n"
    "from tools.cron_run_scope import clip_cron_response, cron_run_scope\n"
    "\n"
    "\n" + CRONJOB_IMPORT_ANCHOR
)

# The run_one_job call, and the return that reports on it, are two anchors
# rather than one span. v2026.8.13 split this fire path in half — the claim
# stays in _execute_job_now and the run moved to _run_claimed_job so a
# background dispatch can take the claim synchronously — and pushed a
# try/finally for the scheduler's in-flight registration in between them.
# Anchored separately, that reshuffle costs nothing; anchored as one block, as
# it was, it broke both edits at once.
#
# The cron scope nests inside the try that stops the heartbeat thread upstream
# added in v2026.8.3, so the heartbeat is still joined if the scope or the run
# raises.
CRONJOB_EXECUTE = (
    "            try:\n"
    "                processed = run_one_job(\n"
    "                    job, adapters=adapters, loop=gateway_loop,\n"
    "                    extra_prompt=extra_prompt,\n"
    "                )\n"
)

CRONJOB_EXECUTE_PATCHED = (
    "            # kube-agents patch: mark the thread as a cron run so the\n"
    "            # kanban tools can tell it apart from the worker whose env it\n"
    "            # inherited, and collect the run's report instead of throwing\n"
    "            # it away.\n"
    "            outcome: Dict[str, Any] = {}\n"
    "            try:\n"
    "                with cron_run_scope(job_id):\n"
    "                    processed = run_one_job(\n"
    "                        job, adapters=adapters, loop=gateway_loop,\n"
    "                        extra_prompt=extra_prompt, outcome=outcome,\n"
    "                    )\n"
)

CRONJOB_RETURN = (
    "        refreshed = get_job(job_id) or {}\n"
    '        ok = refreshed.get("last_status") == "ok"\n'
    "        return {\n"
    '            "claimed": True,\n'
    '            "success": bool(processed and ok),\n'
    '            "error": refreshed.get("last_error"),\n'
    "        }\n"
)

CRONJOB_RETURN_PATCHED = (
    "        refreshed = get_job(job_id) or {}\n"
    '        ok = refreshed.get("last_status") == "ok"\n'
    "        return {\n"
    '            "claimed": True,\n'
    '            "success": bool(processed and ok),\n'
    '            "error": refreshed.get("last_error"),\n'
    "            # kube-agents patch: the run's own report, collected above.\n"
    '            "response": outcome.get("response"),\n'
    '            "output_file": outcome.get("output_file"),\n'
    '            "delivery_error": outcome.get("delivery_error"),\n'
    "        }\n"
)

CRONJOB_RESULT = (
    '            elif exec_result.get("error"):\n'
    '                result["execution_error"] = exec_result["error"]\n'
    '            return json.dumps({"success": True, "job": result}, indent=2)\n'
)

CRONJOB_RESULT_PATCHED = (
    '            elif exec_result.get("error"):\n'
    '                result["execution_error"] = exec_result["error"]\n'
    "            # kube-agents patch: a synchronous run must report what it did.\n"
    "            # Without this the caller sees only executed/execution_success\n"
    "            # and cannot tell that the run already published its result.\n"
    '            response = clip_cron_response(exec_result.get("response"))\n'
    "            if response:\n"
    '                result["response"] = response\n'
    "            # str(): everything merged here is about to be json.dumps'd, and\n"
    "            # a TypeError there would lose the whole result, not just a field.\n"
    '            if exec_result.get("output_file"):\n'
    '                result["output_file"] = str(exec_result["output_file"])\n'
    '            if exec_result.get("delivery_error"):\n'
    '                result["delivery_error"] = str(exec_result["delivery_error"])\n'
    '            return json.dumps({"success": True, "job": result}, indent=2)\n'
)

# --- tools/kanban_tools.py: a cron run owns no card -------------------------

KANBAN_IMPORT_ANCHOR = "from hermes_cli.config import cfg_get, load_config"

KANBAN_IMPORT_PATCHED = (
    KANBAN_IMPORT_ANCHOR + "\n"
    "\n"
    "# kube-agents patch: see tools/cron_run_scope.py\n"
    "from tools.cron_run_scope import (\n"
    "    cron_ownership_violation,\n"
    "    missing_task_id_error,\n"
    ")"
)

# There is no _default_task_id edit here any more, and its absence is the
# patch, not an omission. v2026.8.13 absorbed that half: cron.scheduler.run_job
# now enters agent.delegation_context.non_dispatcher_owned_context() around the
# whole run, _default_task_id consults it through _is_dispatcher_owned_worker(),
# and a dispatched job therefore inherits no ambient card upstream-side. Keeping
# our own rewrite of that function would be a second implementation of a rule
# upstream now owns, pinned to a literal anchor on a body upstream is actively
# editing — every future bump would break the build to re-apply a no-op.
#
# What the scope is still needed for is everything below: upstream's marker
# says "not the dispatcher's worker", not "cron job X", so the refusal messages
# that name the job and the explicit-task_id guard both still come from here.
# verify_cron_run_scope.py asserts upstream's mechanism still returns no
# ambient card, because nothing else would now notice if it stopped.

KANBAN_OWNERSHIP = (
    '    env_tid = os.environ.get("HERMES_KANBAN_TASK")\n'
    "    if not env_tid:\n"
    "        # Orchestrator or CLI context — no task-scope restriction.\n"
    "        return None\n"
)

KANBAN_OWNERSHIP_PATCHED = (
    "    # kube-agents patch: a cron run borrows the worker's env but owns no\n"
    "    # card of its own. See tools/cron_run_scope.py.\n"
    "    cron_err = cron_ownership_violation(tid)\n"
    "    if cron_err:\n"
    "        return tool_error(cron_err)\n" + KANBAN_OWNERSHIP
)

# Told to set an env var that is already set, to a card it must not touch, a
# cron run would just pass that card explicitly. Give it the real answer.
KANBAN_MISSING_MSG = '"task_id is required (or set HERMES_KANBAN_TASK in the env)"'
KANBAN_MISSING_MSG_PATCHED = "missing_task_id_error()"

PREFIX = "cron_run_scope"


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    scheduler = patchlib.Patch(root, "cron/scheduler.py", prefix=PREFIX)
    scheduler.refuse_if_patched(SCHEDULER_PATCHED_MARKER)
    run_one = scheduler.find_def("run_one_job", label="cron run entry point")
    run_one.expect_keyword_only(*SCHEDULER_EXPECTED_PARAMS)
    # First, and by offset: substitute() rewrites the whole string and would
    # invalidate the locator's spans. The two anchors below sit inside this
    # same def, so the order also has to be this way round.
    scheduler.insert(run_one.keyword_only_end(), SCHEDULER_OUTCOME_PARAM)
    scheduler.substitute(
        SCHEDULER_SAVE_OUTPUT, SCHEDULER_SAVE_OUTPUT_PATCHED, label="saved output"
    )
    scheduler.substitute(SCHEDULER_TAIL, SCHEDULER_TAIL_PATCHED, label="run tail")
    scheduler.commit("1 locator, 2 anchors")

    cronjob = patchlib.Patch(root, "tools/cronjob_tools.py", prefix=PREFIX)
    cronjob.substitute(
        CRONJOB_IMPORT_ANCHOR, CRONJOB_IMPORT_PATCHED, label="scope import"
    )
    cronjob.substitute(CRONJOB_EXECUTE, CRONJOB_EXECUTE_PATCHED, label="scoped run")
    cronjob.substitute(CRONJOB_RETURN, CRONJOB_RETURN_PATCHED, label="run report")
    cronjob.substitute(CRONJOB_RESULT, CRONJOB_RESULT_PATCHED, label="tool result")
    cronjob.commit("4 anchors")

    kanban = patchlib.Patch(root, "tools/kanban_tools.py", prefix=PREFIX)
    kanban.substitute(
        KANBAN_IMPORT_ANCHOR, KANBAN_IMPORT_PATCHED, label="helper import"
    )
    kanban.substitute(
        KANBAN_OWNERSHIP, KANBAN_OWNERSHIP_PATCHED, label="ownership guard"
    )
    # One per lifecycle tool, and how many of those there are is upstream's
    # business: v2026.8.13 shipped nine where v2026.8.3 had seven.
    kanban.substitute_all(
        KANBAN_MISSING_MSG, KANBAN_MISSING_MSG_PATCHED, label="missing task_id"
    )
    kanban.commit("3 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
