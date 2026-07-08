from typing import Any

from .store import log_event_to_db


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", log_event_to_db)
