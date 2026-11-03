"""The Hermes session id of the gateway turn in flight.

On a gateway platform turn (Google Chat, Slack, ...) Hermes binds the chat id,
not the session id, in the context a tool reads, so the session_store plugin
binds the session id here at `pre_gateway_dispatch` and the kanban patches
read it back (deploy/docker/patches/kanban_priority.py
`trusted_origin_session`). A ContextVar, so the value is the turn's own: each
inbound message runs in its own task context.

Hermes loads this plugin as `hermes_plugins.session_store`, the tests load it as
a top-level module, and a reader cannot know which name it got. So the first
copy imported registers a holder module under TURN_CONTEXT_MODULE carrying its
ContextVar and functions, any later copy shares that ContextVar, and a reader
looks the holder up by that name.
"""

import sys
import types
from contextvars import ContextVar

TURN_CONTEXT_MODULE = "kube_agents_turn_context"
_CONTEXT_VAR_NAME = "HERMES_GATEWAY_SESSION_ID"

_holder = sys.modules.get(TURN_CONTEXT_MODULE)
if _holder is None or not isinstance(getattr(_holder, _CONTEXT_VAR_NAME, None), ContextVar):
    _holder = types.ModuleType(TURN_CONTEXT_MODULE)
    setattr(_holder, _CONTEXT_VAR_NAME, ContextVar(_CONTEXT_VAR_NAME, default=""))
    sys.modules[TURN_CONTEXT_MODULE] = _holder
HERMES_GATEWAY_SESSION_ID: ContextVar = getattr(_holder, _CONTEXT_VAR_NAME)


def bind_session(session_id: str) -> None:
    """Bind the turn's Hermes session id in the current context."""
    HERMES_GATEWAY_SESSION_ID.set(session_id or "")


def current_session() -> str:
    """The Hermes session id bound for this turn, or `""`."""
    return HERMES_GATEWAY_SESSION_ID.get()


if not hasattr(_holder, "current_session"):
    _holder.bind_session = bind_session
    _holder.current_session = current_session
