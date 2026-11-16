"""Count a synchronous chat turn the gateway failed to complete, on the chat_metrics registry.

Installed as ``gateway/chat_turn_metrics.py`` and called from three places
``apply_chat_turn_metrics.py`` patches into the Hermes gateway: the ``except``
body of the agent turn (``run_turn.py``, ``_hmwa_agent_error_reply``, where an
exception out of the turn lands), the delivery of a result the agent marked
``failed`` (``run_turn.py``; a provider 429 or 5xx, a connection error and the
gateway timeout come back this way), and the provider-authentication failure
in the turn runner (``run_turn_runner.py``). Each counts one turn under its
platform on the ``chat_metrics`` plugin's registry, the one the plugin's
listener serves on ``CHAT_METRICS_PORT`` and the operator's usage poller reads
into ``status.usage.chatTurnsErroredTotal``. A failed result whose delivery
then raises counts at the second and the first site both; that double count is
accepted as rare.

Never raises and never imports at module load: the gateway must run as upstream
ships it when the plugin is absent, and the registry is reached lazily, by
putting the bundled plugins directory on ``sys.path`` once, so that the module
object is the one the plugin and ``session_store`` share.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

# Where the image keeps the bundled plugins; the gateway is started with
# ``--plugins`` pointing here (deploy/shared/docker-entrypoint.sh), and the
# verifier points it at a directory of its own.
DEFAULT_PLUGINS_DIR = "/opt/defaults/plugins"
LOGGER = logging.getLogger("kube-agents.chat-turn-metrics")

_registry: Any = None
_lookup_failed = False


def _platform_name(source: Any) -> str:
    """The Hermes platform name of a turn's source, or '' when it has none."""
    platform = getattr(source, "platform", source)
    value = getattr(platform, "value", platform)
    return str(value or "")


def _load_registry() -> Any:
    """The chat_metrics registry, imported once; None when the plugin is not there."""
    global _registry, _lookup_failed
    if _registry is not None or _lookup_failed:
        return _registry
    if DEFAULT_PLUGINS_DIR not in sys.path:
        sys.path.insert(0, DEFAULT_PLUGINS_DIR)
    try:
        from chat_metrics import metrics  # the plugin's module, which owns REGISTRY
    except Exception as exc:  # noqa: BLE001 - a gateway without the plugin counts nothing, once logged
        _lookup_failed = True
        LOGGER.info("chat_metrics is not importable (%s); failed turns are not counted", type(exc).__name__)
        return None
    _registry = metrics.REGISTRY
    return _registry


def record_turn_error(source: Any) -> None:
    """Count one failed synchronous turn for ``source``'s platform. Never raises."""
    try:
        registry = _load_registry()
        if registry is not None:
            registry.record_turn_error(_platform_name(source))
    except Exception:  # noqa: BLE001 - a metric must never fail the turn's own error path
        LOGGER.debug("failed turn not counted", exc_info=True)
