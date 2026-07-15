from typing import Any

from .audit import (
    log_post_approval_response,
    log_post_tool_call,
    log_pre_approval_request,
    log_pre_gateway_dispatch,
    log_pre_tool_call,
)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", log_pre_tool_call)
    ctx.register_hook("post_tool_call", log_post_tool_call)
    ctx.register_hook("pre_approval_request", log_pre_approval_request)
    ctx.register_hook("post_approval_response", log_post_approval_response)
    ctx.register_hook("pre_gateway_dispatch", log_pre_gateway_dispatch)

