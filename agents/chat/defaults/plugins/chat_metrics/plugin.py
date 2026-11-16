"""Count every chat message that reaches the gateway, by platform, and serve the count.

Registered on `pre_gateway_dispatch`, the hook Hermes runs once per inbound
gateway event, after it has dropped its own internal events and before it
checks the sender against the allowlist and dispatches; `session_store`
records its metadata on the same hook, and counts each session it first sees
on this plugin's registry (metrics.REGISTRY), so the listener below serves
sessions beside messages. So an unlisted user's message counts, and a message
Hermes never hands to the hook does not. The hook rewrites nothing and never
raises: a count must not cost a turn.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# See the note in plugins/session_store/store.py: the plugin loader pins no
# sys.path entry, so a sibling import is resolved from this file's location.
_PLUGINS_DIR = str(Path(__file__).resolve().parents[1])
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from chat_metrics import metrics  # noqa: E402

logger = logging.getLogger("hermes.plugin.chat_metrics")

REGISTRY = metrics.REGISTRY
_listener = None
# Every hermes process in the container (a CLI call, a worker) runs plugin
# discovery, so each would otherwise try to bind the one port the pod has for
# this, and only the gateway sees messages: the listener belongs in the
# gateway process alone. Which process this is comes from its command line,
# the way Hermes itself tells a gateway apart (gateway/status.py), not from
# what has been imported so far: Hermes starts plugin discovery on a
# background thread before the gateway's own modules are loaded, so an import
# test races it. The subcommand tokens of `hermes [--profile X] gateway run`.
GATEWAY_SUBCOMMAND = ("gateway", "run")
PROFILE_FLAGS = ("--profile", "-p")


def _platform_of(event: Any) -> str:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", "") if source is not None else ""
    return getattr(platform, "value", None) or str(platform or "")


def count_inbound(event: Any = None, gateway: Any = None, session_store: Any = None, **kwargs: Any) -> Optional[Dict[str, str]]:
    """Count the event under its platform; rewrite nothing."""
    metrics.note_gateway(gateway)
    try:
        REGISTRY.record_inbound(_platform_of(event))
    except Exception as exc:  # noqa: BLE001 -- the count never costs a turn
        logger.error("Error in chat_metrics pre_gateway_dispatch hook: %s", exc, exc_info=True)
    return None


def subcommand_of(argv: list) -> str:
    """The first positional token after the profile flags: `gateway`, `send`,
    `chat`, ... Named in the log instead of the arguments, which for `send`
    carry the message text and its destination."""
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
        elif token in PROFILE_FLAGS:
            skip_next = True
        elif not token.startswith(tuple(f + "=" for f in PROFILE_FLAGS)) and not token.startswith("-"):
            return token
    return "no subcommand"


def in_gateway_process(argv: Optional[list] = None) -> bool:
    """Whether this process is the Hermes gateway, the one that receives messages.

    Hermes' own classifier when it is importable, else the same reading of the
    command line: the `gateway` subcommand followed by `run`, or bare `gateway`,
    which Hermes defaults to `run`, with `--profile X` / `-p X` skipped.
    """
    argv = list(sys.argv if argv is None else argv)
    try:
        from gateway.status import looks_like_gateway_command_line  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- outside Hermes, read argv the same way
        looks_like_gateway_command_line = None
    if looks_like_gateway_command_line is not None:
        return bool(looks_like_gateway_command_line(" ".join(str(a) for a in argv)))
    tokens = []
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
        elif token in PROFILE_FLAGS:
            skip_next = True
        elif not token.startswith(tuple(f + "=" for f in PROFILE_FLAGS)):
            tokens.append(token)
    for i, token in enumerate(tokens):
        if token == GATEWAY_SUBCOMMAND[0]:
            return i + 1 >= len(tokens) or tokens[i + 1] == GATEWAY_SUBCOMMAND[1]
    return False


def start_listener() -> None:
    """Start the metrics listener once, in the gateway process, on the operator's port, if one is set."""
    global _listener
    if _listener is not None:
        return
    if not in_gateway_process():
        # At info, so a gateway that misread its own command line is visible
        # in the log rather than a silent listener that never bound. The
        # subcommand alone: a `send` carries the message in its arguments.
        logger.info("not the gateway process (hermes %s); the inbound chat count is served from the gateway alone", subcommand_of(sys.argv))
        return
    port = metrics.listener_port_from_env()
    if port is None:
        logger.info("%s is not set; inbound chat messages are counted but not served", metrics.METRICS_PORT_ENV)
        return
    _listener = metrics.start_listener(port, REGISTRY)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", count_inbound)
    start_listener()
