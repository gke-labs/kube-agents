"""Wire tools/api_session_stop.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``, AFTER
``apply_kanban_wake_guard.py``: two of its anchors are lines that patch
rewrote. Six anchored edits across two files, plus their imports:

* ``gateway/platforms/api_server.py`` — five. The import, before the class that
  uses it. The chat-completions handler, already wrapped in the wake guard's
  ``wake_marked``, is wrapped in ``track`` inside it. ``_run_agent``'s
  ``run_in_executor`` call carries the turn's record through ``carry``. The
  agent is bound to the record right after ``_run_agent`` registers it for
  shutdown interrupts. The route table gains the stop route.
* ``tools/kanban_tools.py`` — ``kanban_create`` checks the stop gate right
  after the wake guard's gate.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/api_session_stop.py``. Usage::

    python3 apply_api_session_stop.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib

API_RELATIVE = "gateway/platforms/api_server.py"
TOOLS_RELATIVE = "tools/kanban_tools.py"

MARKER = "kube-agents patch: api_session_stop"

# Before the class that uses it, so the name exists when the class body runs.
API_IMPORT_ANCHOR = "from contextvars import ContextVar\n"
API_IMPORT_PATCHED = API_IMPORT_ANCHOR + (
    f"# {MARKER}: see tools/api_session_stop.py\n"
    "from tools import api_session_stop as _kube_session_stop\n"
)

# The wake guard's wrapping of the mixin's handler (apply_kanban_wake_guard.py).
HANDLER_ANCHOR = "        _kanban_wake_marked(OpenAICompatRoutesMixin._handle_chat_completions)\n"
HANDLER_PATCHED = (
    "        _kanban_wake_marked(_kube_session_stop.track(OpenAICompatRoutesMixin._handle_chat_completions))\n"
)

# The wake guard's carry onto the executor thread (apply_kanban_wake_guard.py).
EXECUTOR_ANCHOR = "            return await loop.run_in_executor(None, _kanban_wake_carry(_run))\n"
EXECUTOR_PATCHED = (
    f"            # {MARKER}: the turn's record rides to its thread too.\n"
    "            return await loop.run_in_executor(None, _kanban_wake_carry(_kube_session_stop.carry(self, _run)))\n"
)

# _run_agent's registration of the turn's agent for shutdown interrupts.
REGISTER_ANCHOR = "                    self._shutdown_interruptible_agents[id(agent)] = agent\n"
REGISTER_PATCHED = REGISTER_ANCHOR + (
    f"                    _kube_session_stop.bind(agent)  # {MARKER}\n"
)

ROUTES_ANCHOR = "        routes.extend(_api_runs._http_routes(self))\n"
ROUTES_PATCHED = ROUTES_ANCHOR + (
    f"        routes.append(_kube_session_stop.route(self))  # {MARKER}\n"
)

# The wake guard's gate at the top of kanban_create (apply_kanban_wake_guard.py).
CREATE_ANCHOR = "    if _wake_err:\n        return tool_error(_wake_err)\n"
CREATE_PATCHED = CREATE_ANCHOR + (
    f"    # {MARKER}: a turn the person stopped can't start new work (#3263).\n"
    "    _stop_err = _kube_refuse_create_on_stop()\n"
    "    if _stop_err:\n"
    "        return tool_error(_stop_err)\n"
)
TOOLS_TRAILER = (
    f"\n\n# {MARKER}: see tools/api_session_stop.py\n"
    "from tools.api_session_stop import refuse_create_on_stop as _kube_refuse_create_on_stop  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    api = patchlib.Patch(root, API_RELATIVE, prefix="api_session_stop")
    api.refuse_if_patched(MARKER)
    api.substitute(API_IMPORT_ANCHOR, API_IMPORT_PATCHED, label="module import")
    api.substitute(HANDLER_ANCHOR, HANDLER_PATCHED, label="chat-completions wrapper")
    api.substitute(EXECUTOR_ANCHOR, EXECUTOR_PATCHED, label="executor carry")
    api.substitute(REGISTER_ANCHOR, REGISTER_PATCHED, label="agent bind")
    api.substitute(ROUTES_ANCHOR, ROUTES_PATCHED, label="stop route")

    tools = patchlib.Patch(root, TOOLS_RELATIVE, prefix="api_session_stop")
    tools.refuse_if_patched(MARKER)
    tools.substitute(CREATE_ANCHOR, CREATE_PATCHED, label="create gate")
    tools.append(TOOLS_TRAILER)

    api.commit("5 anchors")
    tools.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
