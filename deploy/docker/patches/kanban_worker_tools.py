"""Hide worker-only kanban tools from orchestrator profiles.

Installed into the image at ``/opt/hermes/tools/kanban_worker_tools.py`` and
wired into ``tools/kanban_tools.py`` by ``deploy/docker/Dockerfile`` via
``apply_kanban_worker_tools.py``.

Upstream gates each kanban tool with a ``check_fn``, and has two of them:

``_check_kanban_mode``
    True for a dispatcher-spawned worker (``HERMES_KANBAN_TASK`` is set) *or*
    any profile carrying the ``kanban`` toolset.

``_check_kanban_orchestrator_mode``
    The board-routing surface — the mirror image, false for workers. Upstream
    applies it to ``kanban_list`` and ``kanban_unblock``.

The third case has no implementation: tools that only make sense *inside* a
run. Closing a card, blocking it, heartbeating it, linking it, attaching an
artifact to it — all of these belong to the specialist doing the work. Every
one of them is nonetheless registered with ``_check_kanban_mode``, so the Chat
Agent front door is handed the full lifecycle surface purely because it carries
the ``kanban`` toolset to create cards.

``agents/chat/SOUL.md`` §1.5 already states the boundary in prose:

    Never call ``kanban_complete``, ``kanban_block``, ``kanban_heartbeat``, or
    ``kanban_link`` — those belong to the specialist actually doing the work,
    not the front door.

Prose is not free. Hermes has no per-tool denylist — ``agent.disabled_toolsets``
is toolset-level, and ``_strip_blocked_tools`` in ``tools/delegate_tool.py`` is
scoped to delegation — so those schemas ship on every single front-door model
call and the rule has to be re-read and re-obeyed each time. Measured with
``hermes prompt-size`` on 2026-08-05, the tools below cost the Chat Agent
**10,041 characters (~2,510 tokens) per call** for capabilities it is forbidden
to use:

======================  ======
tool                    chars
======================  ======
``kanban_complete``      3,410
``kanban_block``         1,700
``kanban_attach``        1,356
``kanban_attach_url``    1,156
``kanban_heartbeat``       918
``kanban_link``            773
``kanban_attachments``     728
======================  ======

``check_kanban_worker_mode`` supplies the missing third gate. Nothing changes
for a worker: the dispatcher sets ``HERMES_KANBAN_TASK`` before spawning it
(``hermes_cli/kanban_db.py``, ``_default_spawn``), so a worker keeps every tool
it has today. Orchestrator profiles keep ``kanban_create``, ``kanban_show``,
``kanban_list``, ``kanban_comment`` and ``kanban_unblock`` — exactly the surface
SOUL.md permits them.

A cron run is deliberately treated as *not* a worker's own card here, which is
consistent with ``tools/cron_run_scope.py``: a dispatched run borrows the
worker's environment but owns no card, and is already refused if it tries to
close the caller's.

A ``delegate_task`` child is not a worker either, and that case is why this gate
consults ``agent.delegation_context`` rather than the environment alone. The
child runs ``run_conversation`` in the parent's own process, so
``HERMES_KANBAN_TASK`` still holds whatever the parent's dispatcher set and says
nothing at all about the child. Upstream treats that as the defining case: both
``_check_kanban_mode`` and ``_check_kanban_orchestrator_mode`` open with ``if
_is_delegated_child_context(): return False``, and
``model_tools._compute_tool_definitions`` skips its ``HERMES_KANBAN_TASK``
force-add of the ``kanban`` toolset for the same reason. Reading only the env
var made this the one kanban gate in the file that answered *True* for a child —
exactly inverted, since the child was then the only caller offered the seven
tools below and none of the five an orchestrator keeps.

Nothing reaches that inversion today, and it is worth being precise about why,
because the reason lives in a file nobody editing this one would think to read.
``tools/delegate_tool.py`` blocks the toolset twice over: ``_strip_blocked_tools``
adds ``kanban`` to the names it removes from the child's inherited toolsets, and
``child_disabled_toolsets`` appends ``"kanban"`` unconditionally, so a child's
tool assembly contains no kanban tool and never evaluates this gate at all.
Measured in the image on 2026-08-08 against the child's real shape
(``enabled=[kanban, web]``, ``disabled=[kanban]``): zero kanban tools. Remove the
strip and the same child is handed precisely the seven. Had one ever been
called, six would have been refused anyway — ``_reject_delegated_child_mutation``
guards ``_handle_complete``, ``_handle_block``, ``_handle_heartbeat``,
``_handle_link``, ``_handle_attach`` and ``_handle_attach_url`` before they touch
the board — and the seventh, ``_handle_attachments``, is a read whose default
task id ``_default_task_id`` already withholds from a child. So this gate is the
layer under two working layers, which is what buys it the freedom to answer
``on_unknown=False`` below, where ``hermes_cli/kanban_guardrail_exit.py`` — which
has no layer under it — answers ``True`` from the same predicate. Both readings
come from ``tools/kanban_ownership.py``, which carries the full argument.

One caveat travels with the short-circuit. The answer is a ContextVar, while
``tools/registry.py`` caches ``check_fn`` results for 30 seconds keyed on
``(fn, profile)`` alone — so a child that did evaluate this gate would leave a
``False`` behind for its parent worker to read. Upstream's two gates carry the
identical exposure, and it is not worth patching the registry for it: the only
way to reach that cache write is the path ``delegate_tool.py`` already closes.
"""

from __future__ import annotations

try:  # In the image, /opt/hermes is on sys.path.
    from tools.kanban_ownership import is_delegated_child, is_dispatcher_worker
except ImportError:  # Local unittest discovery: the patches dir is top-level.
    from kanban_ownership import is_delegated_child, is_dispatcher_worker

#: Tools registered upstream with ``_check_kanban_mode`` that only make sense
#: inside a dispatcher-spawned run. Kept here as documentation and as the
#: contract ``apply_kanban_worker_tools.py`` asserts against.
WORKER_ONLY_TOOLS = (
    "kanban_complete",
    "kanban_block",
    "kanban_heartbeat",
    "kanban_link",
    "kanban_attach",
    "kanban_attach_url",
    "kanban_attachments",
)


def check_kanban_worker_mode() -> bool:
    """True only inside a dispatcher-spawned kanban run.

    The mirror of upstream's ``_check_kanban_orchestrator_mode``, delegated-child
    short-circuit included, so that all three kanban gates agree about who a
    ``delegate_task`` child is. Deliberately does *not* consult the profile's
    toolset list: carrying the ``kanban`` toolset is what lets a profile route
    work, and routing work is precisely the case these tools must stay hidden
    for.
    """
    # on_unknown=False: this gate only chooses which schemas ship, and
    # _reject_delegated_child_mutation still refuses a child's board writes, so
    # an unreadable delegation context must not cost a real worker its terminal
    # tools. tools/kanban_ownership.py argues both polarities.
    if is_delegated_child(on_unknown=False):
        return False
    return is_dispatcher_worker()
