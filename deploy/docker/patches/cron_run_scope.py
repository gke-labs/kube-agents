"""Make a dispatched cron run report back, and stop it closing its caller's card.

Installed into the image at ``/opt/hermes/tools/cron_run_scope.py`` and wired
into ``cron/scheduler.py``, ``tools/cronjob_tools.py``, and
``tools/kanban_tools.py`` by ``deploy/docker/Dockerfile``.

Two upstream defects combine badly when an orchestrator profile dispatches a
cron job with ``cronjob(action='run')``. Both were observed in production on
2026-08-04, when three of the five fleet-audit streams published wrong results
— including a ledger issue closed as "no issues found" that had real findings,
and a second ledger issue opened and closed with eight-hour-stale data.

1. **The run's result is discarded.** ``cron/scheduler.py:run_job`` returns
   ``(success, output, final_response, error)``, but ``run_one_job`` throws
   ``final_response`` away and returns a bare ``bool``. The caller blocked for
   nearly five minutes and got back only ``{"executed": true,
   "execution_success": true, ...}`` — no report, no issue URL, not even the
   path of the saved output file. It had no way to see that the run had
   already published its result.

2. **The run terminates the dispatcher's kanban card.** A dispatched job runs
   in-process inside the kanban worker and inherits ``HERMES_KANBAN_TASK``.
   Every cron session called ``kanban_complete`` with no ``task_id``, and
   ``_default_task_id`` resolved it from the env to the *caller's* card. Each
   dispatcher then got ``could not complete t_… (unknown id or already
   terminal)`` and its own summary was silently dropped on the floor.

Together they leave a dispatcher with a finished-but-invisible run and a dead
card, which is what drove three of them to improvise: re-running the audit's
publish step against shared scratch state, restoring a stale findings file from
a ``.bak``, and finally hand-blanking it to force the issue closed.

The fix is a marker, ``HERMES_KANBAN_CRON_RUN``, held for the duration of a
dispatched run as both a context variable and an environment variable — the
pair ``gateway/session_context.py`` already uses for its per-job cron state. It
says "this run is a cron job borrowing a worker's environment", which is
precisely the distinction the kanban helpers could not previously draw. Defect
1 is fixed separately, by an ``outcome`` out-param that lets ``run_one_job``
hand its ``final_response`` back instead of dropping it.

``HERMES_KANBAN_TASK`` is deliberately left in place rather than unset for the
duration. ``kanban_tools.heartbeat_current_worker_from_env()`` reads it on
every call to keep the dispatcher's 15-minute claim alive, and a compliance
audit takes about 20 minutes — stripping it would let the claim lapse mid-run
and hand the card to a second worker.
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from typing import Iterator, Mapping, Optional

#: Name of the marker, used for both the context variable and the env var.
CRON_RUN_ENV = "HERMES_KANBAN_CRON_RUN"

#: Set by the kanban dispatcher to the card the worker is scoped to.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: Budget for the run report carried back in the tool result. Roomier than the
#: chat handoff limit because this goes to a model, not a chat window, and the
#: whole point is that the caller can read the run's own findings.
CRON_RESPONSE_LIMIT = 4000

#: Job id of the dispatch executing in this context, empty outside one.
_CRON_RUN_JOB: ContextVar = ContextVar(CRON_RUN_ENV, default="")

_UNSET = object()


@contextlib.contextmanager
def cron_run_scope(job_id: str) -> Iterator[None]:
    """Mark the current context as executing cron job ``job_id``.

    Sets both a context variable and the environment variable, in deliberate
    imitation of ``gateway/session_context.py`` — which keeps its cron
    auto-delivery vars in exactly this pair "so concurrent jobs don't clobber
    each other's delivery targets", and resolves reads context-var-first with
    an ``os.environ`` fallback.

    The context variable is what actually reaches the run: ``run_job`` takes a
    ``contextvars.copy_context()`` immediately before submitting
    ``agent.run_conversation`` to its worker thread, so a variable set here is
    visible to every tool the cron agent calls, and to nothing else in the
    process. The environment variable is the fallback that survives a fork —
    a cron agent that shells out to ``hermes kanban …`` gets the marker too.

    Both are restored on the way out, including on an exception and including
    the common case where there was no prior value, so a nested or repeated
    dispatch cannot leave the marker set after the run ends.
    """
    marker = str(job_id or "?")
    token = _CRON_RUN_JOB.set(marker)
    prior = os.environ.get(CRON_RUN_ENV, _UNSET)
    os.environ[CRON_RUN_ENV] = marker
    try:
        yield
    finally:
        _CRON_RUN_JOB.reset(token)
        if prior is _UNSET:
            os.environ.pop(CRON_RUN_ENV, None)
        else:
            os.environ[CRON_RUN_ENV] = prior  # type: ignore[arg-type]


def current_cron_job(environ: Optional[Mapping[str, str]] = None) -> str:
    """Return the job id of the dispatch running in this context, or ``""``.

    Context variable first, environment second — the same resolution order
    ``gateway/session_context.py:get_session_env`` uses, for the same reason:
    the context variable is concurrency-safe where it is set, and the
    environment is what a subprocess inherits.
    """
    job_id = _CRON_RUN_JOB.get()
    if job_id:
        return job_id
    env = os.environ if environ is None else environ
    return env.get(CRON_RUN_ENV) or ""


def resolve_default_task_id(
    arg: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Resolve a ``task_id`` argument, with the dispatcher's card as fallback.

    An explicit argument always wins. Inside a cron run there is no fallback:
    the ambient ``HERMES_KANBAN_TASK`` belongs to whoever dispatched the job,
    not to the job, so silently defaulting to it is how a cron run came to
    close its caller's card.
    """
    if arg:
        return arg
    if current_cron_job(environ):
        return None
    env = os.environ if environ is None else environ
    return env.get(WORKER_TASK_ENV) or None


def cron_ownership_violation(
    tid: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Return a rejection message when a cron run targets its caller's card.

    Only the caller's own card is protected. A cron run that names some other
    task is left alone — that is ordinary cross-task work, and the existing
    worker-ownership rules still apply to it.

    Returns ``None`` when the call is allowed.
    """
    job_id = current_cron_job(environ)
    if not job_id:
        return None
    env = os.environ if environ is None else environ
    worker_tid = env.get(WORKER_TASK_ENV)
    if not worker_tid or tid != worker_tid:
        return None
    return (
        f"cron job '{job_id}' is running inside the worker for task "
        f"{worker_tid} and does not own that card; refusing to mutate it. "
        f"The caller is blocked on this run and will close its own card once "
        f"you return. Report your result as your final response instead — it "
        f"is handed back to the caller verbatim."
    )


def missing_task_id_error(environ: Optional[Mapping[str, str]] = None) -> str:
    """Message for a lifecycle tool called with no resolvable ``task_id``.

    Under a cron run the stock advice — set ``HERMES_KANBAN_TASK`` — is exactly
    wrong: the env var is already set, to a card the run must not touch. Say so,
    and point at the final response as the way to report back.
    """
    job_id = current_cron_job(environ)
    if job_id:
        return (
            f"no task_id given, and this session is a run of cron job "
            f"'{job_id}'. It has no kanban card of its own — the card in the "
            f"environment belongs to the caller that dispatched this run, and "
            f"is not yours to complete or modify. Report your result as your "
            f"final response; it is handed back to the caller verbatim. Pass "
            f"an explicit task_id only to read or comment on some other task."
        )
    return "task_id is required (or set HERMES_KANBAN_TASK in the env)"


def clip_cron_response(text: object) -> str:
    """Clip a run's report to the tool-result budget without severing a token.

    Reuses the whitespace-boundary clip from ``gateway/kanban_handoff_clip.py``
    so that a report ending in a ledger URL either carries the whole URL or
    drops it — never a dead prefix.

    The import is deferred because ``gateway/__init__.py`` eagerly pulls in
    config, session, and delivery. This module is imported at the top of
    ``tools/kanban_tools.py``, which the tool registry loads while
    ``hermes_cli.config`` is still initialising — dragging the whole gateway
    package in at that point is exactly the eager-import cycle that package's
    own docstring warns about. By the time a cron run has a report to clip,
    delivery has already run and the gateway is long since loaded.

    If the import fails anyway the report is hard-cut rather than lost: a
    cosmetic clip must never be the reason a caller stops receiving results,
    which is the very failure this patch exists to fix. The Dockerfile asserts
    the real import resolves in the image, so the fallback should be dead code.
    """
    try:  # In the image, /opt/hermes is on sys.path.
        from gateway.kanban_handoff_clip import clip_handoff
    except Exception:
        try:  # Local unittest discovery: the patches dir is top-level.
            from kanban_handoff_clip import clip_handoff
        except Exception:
            return "" if text is None else str(text).strip()[:CRON_RESPONSE_LIMIT]
    return clip_handoff(text, CRON_RESPONSE_LIMIT)
