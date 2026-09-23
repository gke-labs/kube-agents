#!/usr/bin/env python3
"""Wire tools/cron_run_scope.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two AST locators
and sixteen anchored string replacements across four files is past the point
where an inline ``python3 -c`` stays readable, so the edits live here — but the
guarantee is the same as the other patches in the Dockerfile: every anchor must
be found the number of times expected, every edited file must still parse, and
anything else fails the build loudly rather than shipping a half-patched image.

Why each edit is needed is documented in the module docstring of
``deploy/docker/patches/cron_run_scope.py``. Usage::

    python3 apply_cron_run_scope.py [HERMES_ROOT]   # default /opt/hermes

Anchors are derived against v2026.9.14. That release decomposed
``cron/scheduler.py`` (``_run_one_job_body`` now delegates its save/deliver
phase to ``_save_compose_deliver`` over a ``_RunDelivery`` record and its
bookkeeping tail to ``_finish_completed_run``), split ``tools/cronjob_tools.py``
into per-action helpers (``_action_run``, ``_action_create``) and rewrote the
kanban ownership helpers to raise ``_Reject`` instead of returning a
``tool_error``. The edit sites below follow those moves; the behaviour they
produce is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

# --- cron/scheduler.py: stop discarding the run's own report ----------------
#
# The out-param goes on the end of the keyword-only parameters of *both* halves
# of the run entry point. Located rather than spelled out: this used to be a
# literal anchor on the whole one-line signature, and v2026.8.13 both wrapped
# that line onto three and added an ``extra_prompt`` of its own, either of which
# broke the build for a change the patch has no opinion about. What it does have
# an opinion about is that this is still the shared execute→deliver→mark body
# the ticker and the cronjob tool both call, which is what expect_keyword_only
# asserts.
#
# Both halves, because v2026.8.19 split the function in two: ``run_one_job`` is
# now a wrapper that registers the fire owner and delegates, through
# ``_run_with_fire_claim_heartbeat``, to ``_run_one_job_body``, where the
# execute→deliver→mark sequence — and every write site below — actually lives.
# The parameter has to be on the wrapper because that is what callers name, on
# the body because that is what the writes read, and forwarded across the lambda
# in between. Putting it on the wrapper alone is not a build failure: both
# anchors still match, the file still parses, and the body raises ``name
# 'outcome' is not defined`` on the first real cron tick in a cluster.

SCHEDULER_OUTCOME_PARAM = ", outcome=None"

#: Parameters the run entry point must still take for it to be the one this
#: patch means. Not the whole signature: upstream may add to it, and this patch
#: does not care.
SCHEDULER_EXPECTED_PARAMS = ("adapters", "loop", "verbose")

#: The wrapper's delegation to the body. Anchored so the out-param is forwarded
#: rather than dropped at the split; ``expect_keyword_only`` on the body cannot
#: notice a caller that stops passing it.
SCHEDULER_DELEGATE = (
    "            lambda lost_ownership: _run_one_job_body(\n"
    "                job,\n"
    "                adapters=adapters,\n"
    "                loop=loop,\n"
    "                verbose=verbose,\n"
    "                extra_prompt=extra_prompt,\n"
)

SCHEDULER_DELEGATE_PATCHED = SCHEDULER_DELEGATE + "                outcome=outcome,\n"

#: Text only a successful run leaves behind. The out-param is inserted rather
#: than substituted for an anchor, so the count check cannot tell a fresh file
#: from one this has already run against, and a second pass would append a
#: second ``outcome=None``.
SCHEDULER_PATCHED_MARKER = 'outcome["response"] = final_response'

# v2026.9.14 collapsed the two run_job call shapes (with and without a
# cancel_event) into one call over a kwargs dict, so the scope now wraps that
# single call. The ``except BaseException`` that follows it is upstream's
# teardown-on-raise handler and is left exactly where it was: the scope's
# ContextVar reset runs on the way out of the ``with`` before that handler sees
# the exception, which is the order the old two-branch form had too.
SCHEDULER_RUN_JOB = (
    "        try:\n"
    "            success, output, final_response, error = run_job(job, **_run_kwargs)\n"
    "        except BaseException:\n"
)

SCHEDULER_RUN_JOB_PATCHED = (
    "        try:\n"
    "            # kube-agents patch: enter cron run & risk scope so scheduled runs\n"
    "            # enforce risk-keyed approval gates and execute_code blocks.\n"
    "            # See tools/cron_run_scope.py and tools/cron_risk_gate.py.\n"
    "            from tools.cron_run_scope import cron_run_scope\n"
    '            with cron_run_scope(job["id"], risk=str(job.get("risk") or "high")):\n'
    "                success, output, final_response, error = run_job(job, **_run_kwargs)\n"
    "        except BaseException:\n"
)

# save_job_output moved out of the body into _save_compose_deliver, which does
# not see the out-param. The saved path travels back on the _RunDelivery record
# that function already fills in for the bookkeeping tail, so the record gains a
# field and the tail copies it across. A dataclass field with a default rather
# than an attribute set on the fly: a save that raises before the assignment
# would otherwise leave the tail reading an attribute that does not exist.
SCHEDULER_DELIVERY_FIELD = "    side_effect_ownership_lost: bool = False\n"

SCHEDULER_DELIVERY_FIELD_PATCHED = (
    SCHEDULER_DELIVERY_FIELD
    + "    # kube-agents patch: where the output landed, for the run's own report.\n"
    "    # See tools/cron_run_scope.py.\n"
    "    output_file: Optional[str] = None\n"
)

SCHEDULER_SAVE_OUTPUT = (
    '        output_file = save_job_output(job["id"], output)\n'
    "    if verbose:\n"
    '        logger.info("Output saved to: %s", output_file)\n'
)

SCHEDULER_SAVE_OUTPUT_PATCHED = (
    SCHEDULER_SAVE_OUTPUT
    + "    # kube-agents patch: see tools/cron_run_scope.py\n"
    "    # str(): save_job_output returns a pathlib.Path, and this value ends up\n"
    "    # inside the json.dumps of the run action.\n"
    "    d.output_file = str(output_file)\n"
)

# The success-path tail. v2026.9.14 moved the finish_execution/return pair into
# _finish_completed_run, which sees neither final_response nor the out-param,
# so the report is handed back in the body just before that call — after the
# empty-response soft-failure has adjusted d.success, which is the state the
# old tail wrote too. The one visible difference from anchoring inside the
# tail: a run whose owner-fenced mark_job_run is refused now also reports,
# which is the better answer for a caller that waited on it.
SCHEDULER_TAIL = "        return _finish_completed_run(d, fire_owner, execution_id)\n"

SCHEDULER_TAIL_PATCHED = (
    "        # kube-agents patch: hand the run's own report back to whoever\n"
    "        # dispatched it. See tools/cron_run_scope.py.\n"
    "        if outcome is not None:\n"
    '            outcome["response"] = final_response\n'
    '            outcome["success"] = d.success\n'
    '            outcome["error"] = d.error\n'
    '            outcome["delivery_error"] = delivery_error\n'
    '            outcome["output_file"] = d.output_file\n'
    + SCHEDULER_TAIL
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
# v2026.9.14 turned the heartbeat that keeps the caller's inactivity watchdog
# at bay into a context manager (``_run_heartbeat``) around the call. The cron
# scope nests inside it, so the heartbeat is still joined if the scope or the
# run raises; the out-param is bound just ahead of the ``with`` so the return
# below can read it whether or not the run reached the scope.
CRONJOB_EXECUTE = (
    '            with _run_heartbeat(str(job.get("name") or job_id)):\n'
    "                processed = run_one_job(job, adapters=adapters, loop=gateway_loop, extra_prompt=extra_prompt)\n"
)

CRONJOB_EXECUTE_PATCHED = (
    "            # kube-agents patch: mark the thread as a cron run so the\n"
    "            # kanban tools can tell it apart from the worker whose env it\n"
    "            # inherited, and collect the run's report instead of throwing\n"
    "            # it away.\n"
    "            outcome: Dict[str, Any] = {}\n"
    '            with _run_heartbeat(str(job.get("name") or job_id)):\n'
    '                with cron_run_scope(job_id, risk=str(job.get("risk") or "high")):\n'
    "                    processed = run_one_job(\n"
    "                        job, adapters=adapters, loop=gateway_loop,\n"
    "                        extra_prompt=extra_prompt, outcome=outcome,\n"
    "                    )\n"
)

CRONJOB_RETURN = (
    '        return {"claimed": True, "success": bool(processed and ok), "error": run_error}\n'
)

CRONJOB_RETURN_PATCHED = (
    "        return {\n"
    '            "claimed": True,\n'
    '            "success": bool(processed and ok),\n'
    '            "error": run_error,\n'
    "            # kube-agents patch: the run's own report, collected above.\n"
    '            "response": outcome.get("response"),\n'
    '            "output_file": outcome.get("output_file"),\n'
    '            "delivery_error": outcome.get("delivery_error"),\n'
    "        }\n"
)

CRONJOB_RESULT = (
    '    elif exec_result.get("error"):\n'
    '        result["execution_error"] = exec_result["error"]\n'
    '    return _dumps({"success": True, "job": result})\n'
)

CRONJOB_RESULT_PATCHED = (
    '    elif exec_result.get("error"):\n'
    '        result["execution_error"] = exec_result["error"]\n'
    "    # kube-agents patch: a synchronous run must report what it did.\n"
    "    # Without this the caller sees only executed/execution_success\n"
    "    # and cannot tell that the run already published its result.\n"
    '    response = clip_cron_response(exec_result.get("response"))\n'
    "    if response:\n"
    '        result["response"] = response\n'
    "    # str(): everything merged here is about to be json.dumps'd, and\n"
    "    # a TypeError there would lose the whole result, not just a field.\n"
    '    if exec_result.get("output_file"):\n'
    '        result["output_file"] = str(exec_result["output_file"])\n'
    '    if exec_result.get("delivery_error"):\n'
    '        result["delivery_error"] = str(exec_result["delivery_error"])\n'
    '    return _dumps({"success": True, "job": result})\n'
)

# Allow runtime cronjob create to accept an explicit or default risk tier.
# ``cronjob()`` now snapshots ``locals()`` into the dict every action helper
# reads, so a parameter added here reaches ``_action_create`` as ``a["risk"]``
# with nothing else to thread. The model-facing handler forwards an enumerated
# list of schema fields that has never included ``risk`` (nor did the v2026.8.19
# lambda it replaced); the tier is stamped by the profile scaffold and by
# in-process callers, exactly as before.
CRONJOB_CREATE_PARAM_ANCHOR = (
    "    task_id: str = None,\n"
    "    session_id: Optional[str] = None,\n"
)

CRONJOB_CREATE_PARAM_PATCHED = (
    "    risk: Optional[str] = None,\n"
    "    task_id: str = None,\n"
    "    session_id: Optional[str] = None,\n"
)

CRONJOB_CREATE_CALL_ANCHOR = '            reasoning_effort=a["reasoning_effort"],\n'

CRONJOB_CREATE_CALL_PATCHED = (
    '            reasoning_effort=a["reasoning_effort"],\n'
    '            risk=a["risk"],\n'
)

# --- cron/jobs.py: stamp default risk on newly created jobs -----------------

JOBS_DEF_ANCHOR = (
    "    paused_reason: Optional[str] = None,\n"
    ") -> Dict[str, Any]:\n"
)

JOBS_DEF_PATCHED = (
    "    paused_reason: Optional[str] = None,\n"
    "    risk: Optional[str] = None,\n"
    ") -> Dict[str, Any]:\n"
)

JOBS_APPEND_ANCHOR = (
    "    with _jobs_lock():\n"
    "        save_jobs(load_jobs() + [job])\n"
    "    return job\n"
)

JOBS_APPEND_PATCHED = (
    "    # kube-agents patch: stamp risk tier on newly created cron jobs\n"
    "    # so runtime-created jobs run consistently across pod restarts.\n"
    "    # Clamp to high if created from inside a high-risk cron run (no privilege escalation).\n"
    "    try:\n"
    "        try:\n"
    "            from tools.cron_run_scope import current_cron_job, current_cron_risk\n"
    "        except ImportError:\n"
    "            from cron_run_scope import current_cron_job, current_cron_risk\n"
    '        _in_high_cron = bool(current_cron_job() and current_cron_risk() == "high")\n'
    "    except Exception:\n"
    "        _in_high_cron = False\n"
    '    _raw_risk = str(risk).strip().lower() if risk is not None else "low"\n'
    '    _eff_risk = _raw_risk if _raw_risk in ("low", "high") else "high"\n'
    "    if _in_high_cron:\n"
    '        _eff_risk = "high"\n'
    '    job["risk"] = _eff_risk\n'
    "\n"
    "    with _jobs_lock():\n"
    "        save_jobs(load_jobs() + [job])\n"
    "    return job\n"
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
# whole run (v2026.9.14 does it from _CronRunScope.enter()/exit() through the
# enter_/exit_non_dispatcher_owned_context pair), _default_task_id
# consults it through _is_dispatcher_owned_worker(), and a dispatched job
# therefore inherits no ambient card upstream-side. Keeping our own rewrite of
# that function would be a second implementation of a rule upstream now owns,
# pinned to a literal anchor on a body upstream is actively editing — every
# future bump would break the build to re-apply a no-op.
#
# What the scope is still needed for is everything below: upstream's marker
# says "not the dispatcher's worker", not "cron job X", so the refusal messages
# that name the job and the explicit-task_id guard both still come from here.
# verify_cron_run_scope.py asserts upstream's mechanism still returns no
# ambient card, because nothing else would now notice if it stopped.
#
# v2026.9.14 made the ownership helper raise ``_Reject`` (a finished tool_error
# the ``_kanban_handler`` wrapper renders) instead of returning the error
# string, so the refusal raises the same way. The env-scoped check that follows
# it is upstream's and is unchanged.
KANBAN_OWNERSHIP = (
    '    env_tid = os.environ.get("HERMES_KANBAN_TASK")\n'
    "    if env_tid and tid != env_tid:\n"
    "        raise _Reject(\n"
)

KANBAN_OWNERSHIP_PATCHED = (
    "    # kube-agents patch: a cron run borrows the worker's env but owns no\n"
    "    # card of its own. See tools/cron_run_scope.py.\n"
    "    cron_err = cron_ownership_violation(tid)\n"
    "    if cron_err:\n"
    "        raise _Reject(cron_err)\n" + KANBAN_OWNERSHIP
)

# Told to set an env var that is already set, to a card it must not touch, a
# cron run would just pass that card explicitly. Give it the real answer.
# v2026.9.14 folded the nine per-tool copies of this message into one
# ``_require_task_id`` helper; substitute_all keeps the count upstream's
# business either way.
KANBAN_MISSING_MSG = '"task_id is required (or set HERMES_KANBAN_TASK in the env)"'
KANBAN_MISSING_MSG_PATCHED = "missing_task_id_error()"

PREFIX = "cron_run_scope"


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    scheduler = patchlib.Patch(root, "cron/scheduler.py", prefix=PREFIX)
    scheduler.refuse_if_patched(SCHEDULER_PATCHED_MARKER)
    run_one = scheduler.find_def("run_one_job", label="cron run entry point")
    run_one.expect_keyword_only(*SCHEDULER_EXPECTED_PARAMS)
    run_body = scheduler.find_def("_run_one_job_body", label="cron run body")
    run_body.expect_keyword_only(*SCHEDULER_EXPECTED_PARAMS)
    # Both locators first, then both inserts, then the substitutes:
    # substitute() rewrites the whole string and would invalidate a locator's
    # spans, and an insert moves every offset after it. Splicing the higher
    # offset first is what leaves the other where its locator found it. Sorted
    # rather than hand-ordered, so which of the two defs comes first in the
    # file stays upstream's business.
    for offset in sorted(
        (run_one.keyword_only_end(), run_body.keyword_only_end()), reverse=True
    ):
        scheduler.insert(offset, SCHEDULER_OUTCOME_PARAM)
    scheduler.substitute(
        SCHEDULER_DELEGATE, SCHEDULER_DELEGATE_PATCHED, label="body delegation"
    )
    scheduler.substitute(
        SCHEDULER_RUN_JOB, SCHEDULER_RUN_JOB_PATCHED, label="scoped run_job"
    )
    scheduler.substitute(
        SCHEDULER_DELIVERY_FIELD,
        SCHEDULER_DELIVERY_FIELD_PATCHED,
        label="delivery record field",
    )
    scheduler.substitute(
        SCHEDULER_SAVE_OUTPUT, SCHEDULER_SAVE_OUTPUT_PATCHED, label="saved output"
    )
    scheduler.substitute(SCHEDULER_TAIL, SCHEDULER_TAIL_PATCHED, label="run tail")
    scheduler.commit("2 locators, 5 anchors")

    cronjob = patchlib.Patch(root, "tools/cronjob_tools.py", prefix=PREFIX)
    cronjob.substitute(
        CRONJOB_IMPORT_ANCHOR, CRONJOB_IMPORT_PATCHED, label="scope import"
    )
    cronjob.substitute(CRONJOB_EXECUTE, CRONJOB_EXECUTE_PATCHED, label="scoped run")
    cronjob.substitute(CRONJOB_RETURN, CRONJOB_RETURN_PATCHED, label="run report")
    cronjob.substitute(CRONJOB_RESULT, CRONJOB_RESULT_PATCHED, label="tool result")
    cronjob.substitute(
        CRONJOB_CREATE_PARAM_ANCHOR,
        CRONJOB_CREATE_PARAM_PATCHED,
        label="create param",
    )
    cronjob.substitute(
        CRONJOB_CREATE_CALL_ANCHOR,
        CRONJOB_CREATE_CALL_PATCHED,
        label="create call",
    )
    cronjob.commit("6 anchors")

    jobs = patchlib.Patch(root, "cron/jobs.py", prefix=PREFIX)
    jobs.substitute(
        JOBS_DEF_ANCHOR,
        JOBS_DEF_PATCHED,
        label="create_job def risk param",
    )
    jobs.substitute(
        JOBS_APPEND_ANCHOR,
        JOBS_APPEND_PATCHED,
        label="create_job stamp default risk",
    )
    jobs.commit("2 anchors")

    kanban = patchlib.Patch(root, "tools/kanban_tools.py", prefix=PREFIX)
    kanban.substitute(
        KANBAN_IMPORT_ANCHOR, KANBAN_IMPORT_PATCHED, label="helper import"
    )
    kanban.substitute(
        KANBAN_OWNERSHIP, KANBAN_OWNERSHIP_PATCHED, label="ownership guard"
    )
    # One per lifecycle tool, and how many of those there are is upstream's
    # business: v2026.8.13 shipped nine where v2026.8.3 had seven, and
    # v2026.9.14 folded them into one helper.
    kanban.substitute_all(
        KANBAN_MISSING_MSG, KANBAN_MISSING_MSG_PATCHED, label="missing task_id"
    )
    kanban.commit("3 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
