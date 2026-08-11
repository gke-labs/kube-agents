"""Who owns the card a turn is running against, and what to answer when unsure.

Installed into the image at ``/opt/hermes/tools/kanban_ownership.py`` by
``deploy/docker/Dockerfile``. Read by ``tools/kanban_worker_tools.py`` and
``hermes_cli/kanban_guardrail_exit.py``; ``tools/`` is already imported across
package boundaries that way — ``kanban_guardrail_exit`` reads
``tools.cron_run_scope`` — so reaching it costs nothing new.

Three contexts, one environment variable
----------------------------------------
``HERMES_KANBAN_TASK`` is the only ownership signal the environment carries, and
three different things can be running while it is set:

*A dispatcher-spawned worker.* ``hermes_cli/kanban_db.py``'s ``_default_spawn``
sets the variable before spawning the process, so here it means what it says:
this run holds that card and is expected to end it with ``kanban_complete`` or
``kanban_block``.

*A ``delegate_task`` child.* The child runs ``run_conversation`` in the
*parent's own process*, so the variable still holds whatever the parent's
dispatcher set and says nothing at all about the child, which owns no card.
Upstream treats this as the defining case: ``tools/kanban_tools.py``'s
``_reject_delegated_child_mutation`` refuses every board mutation from a child
precisely because stale or inherited ``HERMES_KANBAN_*`` env vars are not proof
of dispatcher ownership, and both of upstream's kanban gates
(``_check_kanban_mode`` and ``_check_kanban_orchestrator_mode``) open with
``if _is_delegated_child_context(): return False``.

*A cron run.* ``cronjob(action='run')`` executes the job in-process inside the
dispatching worker, and ``tools/cron_run_scope.py`` deliberately leaves
``HERMES_KANBAN_TASK`` pointing at the dispatcher's card for the duration, so
``heartbeat_current_worker_from_env`` can keep a 15-minute claim alive across a
20-minute audit. The run is explicitly barred from terminating that card:
``resolve_default_task_id`` returns ``None`` inside a run, and
``cron_ownership_violation`` rejects it by name.

The cron marker is read through ``cron_run_scope.current_cron_job`` rather than
from the environment, and that is the point: it lives in a context variable
scoped to the thread ``run_job`` submitted the run on, so under
``dispatch_in_gateway`` a second worker running concurrently with somebody
else's dispatch does not read it as its own. ``cron_run_scope`` used to write an
``os.environ`` half as well, which is process-wide and did exactly that; see
that module's docstring for why it no longer does.

Import failure is not the same thing as call failure
-----------------------------------------------------
Both readers are reached through an import that can fail, and the two failures
mean opposite things, so they are handled apart rather than swallowed into one
``except``:

*The import fails* — the reader's module is not on the path at all. That is a
structural fact about the process, not uncertainty: a process with no
``agent.delegation_context`` in it is not running a delegated child. Treating it
as uncertainty would flip these predicates on every host where this module is
importable but Hermes is not, the local test suite included. So an absent module
answers ``False``, except for the cron marker, which falls back to the
environment half — that is the one thing a ContextVar cannot do, cross a fork.

*The call fails* — the module is there and the reader raised. That is genuine
uncertainty: the question was meaningful and went unanswered. Only this case
consults ``on_unknown``.

Polarity belongs to the caller
------------------------------
The two callers want opposite answers from the same uncertainty, because a wrong
answer costs them different things. Passing it in keeps that visible at the call
site instead of buried in two near-identical private helpers, which is what these
used to be.

``hermes_cli/kanban_guardrail_exit.py`` passes ``on_unknown=True``. It decides
whether to charge a ``timed_out`` to a card and release its claim. A wrong "yes,
a child" or "yes, a cron run" skips one record on an exit path that is already
exceptional. A wrong "no" charges a failure to a card somebody else is still
working and releases their claim underneath them, mid-run, after which they
complete a card they no longer hold. Uncertainty there means "do not write to the
board" — which, for predicates that name the contexts that must *not* write, is
``True``.

``tools/kanban_worker_tools.py`` passes ``on_unknown=False``. It only decides
which tool schemas ship on a model call. A wrong "yes, a child" hides
``kanban_complete`` and ``kanban_block`` from a real dispatcher worker and
strands its card: a worker with no terminal tool cannot end its run, so it exits
rc=0, the reaper stamps a ``protocol_violation``, and the card burns another
attempt — and a reader that raises does so for every worker in the image, not
for one card. A wrong "no" restores exactly the behaviour that predates that
patch: ~2,510 tokens of schema the caller is forbidden to use, and at most one
refused tool call, because ``_reject_delegated_child_mutation`` still stands
between a child and every board mutation. Uncertainty there means "not a child".

Neither caller is free to drop its argument for a default. There is no answer
that is safe in both directions, so there is no default worth having.
"""

from __future__ import annotations

import os

#: The card id the dispatcher exports before spawning a worker. Inherited
#: unchanged by a ``delegate_task`` child and by a cron run, which is why it is
#: never sufficient on its own.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: The marker ``tools/cron_run_scope.py`` holds for the duration of a dispatched
#: run, as a context variable and — for a forked process launched with it
#: already set — an environment variable. Named here only for the fallback in
#: ``in_cron_run``; ``current_cron_job`` is the reader that matters.
CRON_RUN_ENV = "HERMES_KANBAN_CRON_RUN"


def _delegation_reader():
    """``agent.delegation_context.is_delegated_child_context``, or ``None``.

    ``None`` means the module is absent, which is the structural case above —
    distinct from the reader existing and raising, which the callers handle.
    Re-imported per call rather than bound at module import so that a test can
    swap the module in ``sys.modules`` and have the next call see it.
    """
    try:
        from agent.delegation_context import is_delegated_child_context
    except Exception:
        return None
    return is_delegated_child_context


def _cron_reader():
    """``cron_run_scope.current_cron_job``, or ``None`` if neither path resolves.

    In the image ``/opt/hermes`` is on ``sys.path`` and the module is
    ``tools.cron_run_scope``. Under local unittest discovery the patches
    directory is the top level and it is a plain sibling. Re-imported per call
    for the same reason as ``_delegation_reader``: replacing the module
    attribute has to take effect.
    """
    try:  # In the image, /opt/hermes is on sys.path.
        from tools.cron_run_scope import current_cron_job
    except Exception:
        try:  # Local unittest discovery: the patches dir is top-level.
            from cron_run_scope import current_cron_job
        except Exception:
            return None
    return current_cron_job


def is_delegated_child(*, on_unknown: bool) -> bool:
    """Whether this turn is a ``delegate_task`` child sharing its parent's process.

    ``on_unknown`` is the answer when ``is_delegated_child_context`` is present
    and raises. An absent ``agent.delegation_context`` answers ``False``
    regardless: no delegation machinery in the process means no delegated child.
    See the module docstring for why the caller has to choose.
    """
    reader = _delegation_reader()
    if reader is None:
        return False
    try:
        return bool(reader())
    except Exception:
        return on_unknown


def in_cron_run(*, on_unknown: bool) -> bool:
    """Whether this turn is a cron job borrowing a worker's environment.

    ``on_unknown`` is the answer when ``current_cron_job`` is present and raises.
    If neither ``tools.cron_run_scope`` nor a top-level ``cron_run_scope``
    resolves, the environment half of the marker is read directly — an absent
    module is a fact about the path, and the env var is still evidence.
    """
    reader = _cron_reader()
    if reader is None:
        return bool(os.environ.get(CRON_RUN_ENV))
    try:
        return bool(reader())
    except Exception:
        return on_unknown


def is_dispatcher_worker() -> bool:
    """Whether ``HERMES_KANBAN_TASK`` is set, and nothing more than that.

    No ``on_unknown``: an environment read has no failure mode to be uncertain
    about. It also has no *precision* — the variable is inherited by both of the
    other two contexts — so a caller that means "a worker running its own card"
    composes this with the predicates above rather than using it alone.
    """
    return bool(os.environ.get(WORKER_TASK_ENV))
