#!/usr/bin/env python3
"""Wire tools/kanban_wake_guard.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Four anchored
edits across three files, plus their imports:

* ``gateway/wake.py`` — ``_self_post_chat_completion``'s request headers gain
  the wake marker. Every caller of the self-post is a synthetic turn.
* ``gateway/platforms/api_server.py`` — two edits. The class attribute that
  wraps the mixin's chat-completions handler in ``_admit_api_agent_request``
  wraps it in ``wake_marked`` first, so a marked request runs as a marked turn
  whichever branch of the handler runs it. And ``_run_agent``'s
  ``run_in_executor`` call is wrapped in ``carry_wake_mark``, so the mark
  reaches the thread the turn runs on.
* ``tools/kanban_tools.py`` — ``kanban_create`` checks the gate right after
  upstream's delegated-child check, before it reads any argument.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_wake_guard.py``. Usage::

    python3 apply_kanban_wake_guard.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

WAKE_RELATIVE = "gateway/wake.py"
API_RELATIVE = "gateway/platforms/api_server.py"
TOOLS_RELATIVE = "tools/kanban_tools.py"

# The self-post's headers: the API key and the session to continue.
HEADERS_ANCHOR = (
    '    headers = {"Authorization": f"Bearer {api_key}", '
    '"X-Hermes-Session-Id": session_id}\n'
)
HEADERS_PATCHED = HEADERS_ANCHOR + (
    "    # kube-agents patch: mark the turn as a wake, so kanban_create in a\n"
    "    # gateway conversation can refuse new work on it (#2610). See\n"
    "    # tools/kanban_wake_guard.py.\n"
    "    headers[_KANBAN_WAKE_HEADER] = \"1\"\n"
)
WAKE_TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_wake_guard.py\n"
    "from tools.kanban_wake_guard import WAKE_HEADER as _KANBAN_WAKE_HEADER  # noqa: E402\n"
)

# The admission wrapper applied to the mixin's handler.
HANDLER_ANCHOR = (
    "    _handle_chat_completions = _admit_api_agent_request("
    "OpenAICompatRoutesMixin._handle_chat_completions)\n"
)
HANDLER_PATCHED = (
    "    # kube-agents patch: a wake self-post records its session as on a\n"
    "    # wake turn until the handler returns (#2610). See\n"
    "    # tools/kanban_wake_guard.py.\n"
    "    _handle_chat_completions = _admit_api_agent_request(\n"
    "        _kanban_wake_marked(OpenAICompatRoutesMixin._handle_chat_completions)\n"
    "    )\n"
)
# Inserted before the class that uses it, so the name exists when the class
# body runs (a trailer would be too late for a class attribute).
API_IMPORT_ANCHOR = "from contextvars import ContextVar\n"
API_IMPORT_PATCHED = API_IMPORT_ANCHOR + (
    "# kube-agents patch: see tools/kanban_wake_guard.py\n"
    "from tools.kanban_wake_guard import (\n"
    "    carry_wake_mark as _kanban_wake_carry,\n"
    "    wake_marked as _kanban_wake_marked,\n"
    ")\n"
)

# _run_agent hands the turn to an executor thread; ContextVars don't follow.
EXECUTOR_ANCHOR = "            return await loop.run_in_executor(None, _run)\n"
EXECUTOR_PATCHED = (
    "            # kube-agents patch: carry a wake turn's mark onto the thread\n"
    "            # (#2610). See tools/kanban_wake_guard.py.\n"
    "            return await loop.run_in_executor(None, _kanban_wake_carry(_run))\n"
)

# The first statement of the kanban_create handler, which only it has.
CREATE_ANCHOR = '    _reject_delegated_child_mutation("kanban_create")\n'
CREATE_PATCHED = CREATE_ANCHOR + (
    "    # kube-agents patch: a wake turn in a gateway conversation can't start\n"
    "    # new work; that needs the person to ask (#2610). See\n"
    "    # tools/kanban_wake_guard.py.\n"
    "    _wake_err = _kanban_refuse_create_on_wake()\n"
    "    if _wake_err:\n"
    "        return tool_error(_wake_err)\n"
)
TOOLS_TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_wake_guard.py\n"
    "from tools.kanban_wake_guard import (  # noqa: E402\n"
    "    refuse_create_on_wake as _kanban_refuse_create_on_wake,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    wake = patchlib.Patch(root, WAKE_RELATIVE, prefix="kanban_wake_guard")
    wake.refuse_if_patched("headers[_KANBAN_WAKE_HEADER]")
    wake.substitute(HEADERS_ANCHOR, HEADERS_PATCHED, label="self-post header")
    wake.append(WAKE_TRAILER)

    api = patchlib.Patch(root, API_RELATIVE, prefix="kanban_wake_guard")
    api.refuse_if_patched("_kanban_wake_marked(")
    api.substitute(API_IMPORT_ANCHOR, API_IMPORT_PATCHED, label="handler import")
    api.substitute(HANDLER_ANCHOR, HANDLER_PATCHED, label="chat-completions wrapper")
    api.substitute(EXECUTOR_ANCHOR, EXECUTOR_PATCHED, label="executor carry")

    tools = patchlib.Patch(root, TOOLS_RELATIVE, prefix="kanban_wake_guard")
    tools.refuse_if_patched("_kanban_refuse_create_on_wake()")
    tools.substitute(CREATE_ANCHOR, CREATE_PATCHED, label="create gate")
    tools.append(TOOLS_TRAILER)

    wake.commit("1 anchor")
    api.commit("3 anchors")
    tools.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
