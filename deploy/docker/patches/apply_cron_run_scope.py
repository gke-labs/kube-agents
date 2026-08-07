#!/usr/bin/env python3
"""Wire tools/cron_run_scope.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Nine anchored
string replacements across three files is past the point where an inline
``python3 -c`` stays readable, so the edits live here — but the guarantee is
the same as the other patches in the Dockerfile: every anchor must be found
the exact number of times expected, every edited file must still parse, and
anything else fails the build loudly rather than shipping a half-patched image.

Why each edit is needed is documented in the module docstring of
``deploy/docker/patches/cron_run_scope.py``. Usage::

    python3 apply_cron_run_scope.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# --- cron/scheduler.py: stop discarding the run's own report ----------------

SCHEDULER_SIGNATURE = (
    "def run_one_job(job: dict, *, adapters=None, loop=None, "
    "verbose: bool = False) -> bool:"
)

SCHEDULER_SIGNATURE_PATCHED = (
    "def run_one_job(job: dict, *, adapters=None, loop=None, "
    "verbose: bool = False, outcome=None) -> bool:"
)

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

# run_one_job runs inside a try/finally that stops the heartbeat thread
# upstream added in v2026.8.3; the cron scope has to nest inside that try so
# the heartbeat is still joined if the scope or the run raises.
CRONJOB_EXECUTE = (
    "        try:\n"
    "            processed = run_one_job(job)\n"
    "        finally:\n"
    "            _heartbeat_stop.set()\n"
    "            if _heartbeat_thread is not None:\n"
    "                _heartbeat_thread.join(timeout=_CRON_RUN_HEARTBEAT_INTERVAL + 1)\n"
    "        refreshed = get_job(job_id) or {}\n"
    '        ok = refreshed.get("last_status") == "ok"\n'
    "        return {\n"
    '            "claimed": True,\n'
    '            "success": bool(processed and ok),\n'
    '            "error": refreshed.get("last_error"),\n'
    "        }\n"
)

CRONJOB_EXECUTE_PATCHED = (
    "        # kube-agents patch: mark the thread as a cron run so the kanban\n"
    "        # tools can tell it apart from the worker whose env it inherited,\n"
    "        # and collect the run's report instead of throwing it away.\n"
    "        outcome: Dict[str, Any] = {}\n"
    "        try:\n"
    "            with cron_run_scope(job_id):\n"
    "                processed = run_one_job(job, outcome=outcome)\n"
    "        finally:\n"
    "            _heartbeat_stop.set()\n"
    "            if _heartbeat_thread is not None:\n"
    "                _heartbeat_thread.join(timeout=_CRON_RUN_HEARTBEAT_INTERVAL + 1)\n"
    "        refreshed = get_job(job_id) or {}\n"
    '        ok = refreshed.get("last_status") == "ok"\n'
    "        return {\n"
    '            "claimed": True,\n'
    '            "success": bool(processed and ok),\n'
    '            "error": refreshed.get("last_error"),\n'
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
    "    resolve_default_task_id,\n"
    ")"
)

# v2026.8.3 added the _is_delegated_child_context() early return. Keep it:
# resolve_default_task_id only knows about the cron rule, so replacing the
# whole body — as this patch used to — would silently drop upstream's
# delegate_task behaviour on every base-image bump.
KANBAN_DEFAULT_TASK = (
    "def _default_task_id(arg: Optional[str]) -> Optional[str]:\n"
    '    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""\n'
    "    if arg:\n"
    "        return arg\n"
    "    if _is_delegated_child_context():\n"
    "        return None\n"
    '    env_tid = os.environ.get("HERMES_KANBAN_TASK")\n'
    "    return env_tid or None\n"
)

KANBAN_DEFAULT_TASK_PATCHED = (
    "def _default_task_id(arg: Optional[str]) -> Optional[str]:\n"
    '    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set.\n'
    "\n"
    "    kube-agents patch: inside a cron run there is no fallback — the ambient\n"
    "    card belongs to whoever dispatched the job, not to the job. See\n"
    "    tools/cron_run_scope.py.\n"
    '    """\n'
    "    if arg:\n"
    "        return arg\n"
    "    if _is_delegated_child_context():\n"
    "        return None\n"
    "    # arg is falsy here, so this is the cron check plus the env fallback.\n"
    "    return resolve_default_task_id(arg)\n"
)

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

# (relative path, [(anchor, replacement, expected occurrences)])
PATCHES = (
    (
        "cron/scheduler.py",
        (
            (SCHEDULER_SIGNATURE, SCHEDULER_SIGNATURE_PATCHED, 1),
            (SCHEDULER_SAVE_OUTPUT, SCHEDULER_SAVE_OUTPUT_PATCHED, 1),
            (SCHEDULER_TAIL, SCHEDULER_TAIL_PATCHED, 1),
        ),
    ),
    (
        "tools/cronjob_tools.py",
        (
            (CRONJOB_IMPORT_ANCHOR, CRONJOB_IMPORT_PATCHED, 1),
            (CRONJOB_EXECUTE, CRONJOB_EXECUTE_PATCHED, 1),
            (CRONJOB_RESULT, CRONJOB_RESULT_PATCHED, 1),
        ),
    ),
    (
        "tools/kanban_tools.py",
        (
            (KANBAN_IMPORT_ANCHOR, KANBAN_IMPORT_PATCHED, 1),
            (KANBAN_DEFAULT_TASK, KANBAN_DEFAULT_TASK_PATCHED, 1),
            (KANBAN_OWNERSHIP, KANBAN_OWNERSHIP_PATCHED, 1),
            (KANBAN_MISSING_MSG, KANBAN_MISSING_MSG_PATCHED, 7),
        ),
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits in PATCHES:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"cron_run_scope patch: {path} does not exist")
        source = path.read_text()
        for anchor, replacement, expected in edits:
            found = source.count(anchor)
            if found != expected:
                raise SystemExit(
                    f"cron_run_scope patch: {relative}: expected {expected} "
                    f"occurrence(s) of anchor, found {found}. Upstream Hermes "
                    f"changed — re-derive the anchor before bumping the base "
                    f"image.\n--- anchor ---\n{anchor}"
                )
            source = source.replace(anchor, replacement)
        try:
            ast.parse(source)
        except SyntaxError as e:
            raise SystemExit(
                f"cron_run_scope patch: {relative} no longer parses after "
                f"patching: {e}"
            )
        path.write_text(source)
        print(f"cron_run_scope patch: {relative} ({len(edits)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
