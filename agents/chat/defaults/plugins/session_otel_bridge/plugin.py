import logging
from typing import Any

from .bridge import OtelSessionBridge

logger = logging.getLogger("hermes.plugin.session_otel_bridge")


def register(ctx: Any) -> None:
    try:
        OtelSessionBridge().patch_tracer()
        logger.info("Installed session OTel bridge")
    except Exception as exc:
        logger.error("Failed to install session OTel bridge: %s", exc, exc_info=True)
        raise
