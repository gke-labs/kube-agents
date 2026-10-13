#!/usr/bin/env python3
"""Which remote MCP calls the credential proxy relays for the agent.

The broker's `POST /v1/mcp/<server>` route forwards one JSON-RPC message from
the agent's `mcp-remote` child to a remote MCP server on the broker's own
credential. This module is the whole of what decides whether it may: a table
of servers, each with the exact tool names it may call, in the style of
`api_policy.API_READ_ROUTES`, reviewed as a diff and widened only by pull
request.

**The table is the enforcement.** The token the broker forwards carries the
`cloud-platform` scope, and the GKE server offers tools that create clusters
and patch workloads on it. This table decides what reaches Google, and,
through `filter_tools_list`, what the model is offered. A Hermes
`tools.include` list in an agent config can narrow that offer further, but
nothing here depends on one. A name missing here is refused whatever the model
was offered, so a tool added upstream is refused until someone adds it by pull
request.

Standard library only, like `api_policy`: the process holding the credential
imports it, and so do its tests. `Decision` is reused from `command_policy` so
the audit line names a rule the same way for every door.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from command_policy import Decision

# The route prefix. The server name is the one path segment after it.
MCP_RELAY_PREFIX = "/v1/mcp/"

# The JSON-RPC methods relayed. These are what a stateless tools-only server
# needs: the handshake, a liveness ping, the tool list and the tool call.
# Everything else (resources, prompts, completion, logging/setLevel, sampling
# replies) is refused, because no server in the table offers it today and an
# unread method is an unread door.
RELAYED_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
        "tools/call",
    }
)
METHOD_TOOLS_CALL = "tools/call"
METHOD_TOOLS_LIST = "tools/list"

# Rule ids, in the order `evaluate` answers. The audit line and the refusal
# carry them verbatim. None of the messages repeat caller text: the
# conformance suite (A1) holds every refusal to that.
RULE_SERVER = "gcp.mcp.server"
RULE_UNREADABLE = "gcp.mcp.unreadable-request"
RULE_METHOD = "gcp.mcp.method"
RULE_READ_ONLY = "gcp.mcp.read-only"

# The code a refused request that is not a tool call answers with. -32601 is
# JSON-RPC's "method not found", which is what a refused method looks like to
# a client that cannot read our rule id.
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_VERSION = "2.0"

# The gke server's read tools: the 15 of its 23 that the server annotates
# `readOnlyHint: true`. Read from a live `tools/list` on 2026-10-13. This set
# is the one source: a Hermes `tools.include` list added to an agent config
# should be held to it by test, not kept by hand. Every name left out mutates
# something:
# create_cluster, update_cluster, cancel_operation, create_node_pool,
# update_node_pool, apply_k8s_manifest, delete_k8s_resource, patch_k8s_resource.
#
# `get_k8s_resource` and `describe_k8s_resource` take any kind, Secrets
# included. This table is per tool, not per argument; what a read returns is
# bounded by the broker's IAM roles (`roles/container.viewer` in the shipped
# read-only set), as it is for the kubectl path.
GKE_READ_TOOLS = frozenset(
    {
        "check_k8s_auth",
        "describe_k8s_resource",
        "get_cluster",
        "get_k8s_cluster_info",
        "get_k8s_logs",
        "get_k8s_resource",
        "get_k8s_rollout_status",
        "get_k8s_version",
        "get_node_pool",
        "get_operation",
        "list_clusters",
        "list_k8s_api_resources",
        "list_k8s_events",
        "list_node_pools",
        "list_operations",
    }
)

# The Developer Knowledge server's tools: all three are annotated
# `readOnlyHint: true`. Read from a live `tools/list` on 2026-10-13.
DEVELOPER_KNOWLEDGE_READ_TOOLS = frozenset(
    {
        "answer_query",
        "get_documents",
        "search_documents",
    }
)


@dataclass(frozen=True)
class McpServer:
    """One remote MCP server the broker relays to."""

    name: str  # the path segment after MCP_RELAY_PREFIX
    host: str  # the upstream host, over HTTPS on 443
    path: str  # the upstream path, with its leading slash
    tools: frozenset[str]  # every tools/call name permitted


MCP_SERVERS: tuple[McpServer, ...] = (
    McpServer("gke", "container.googleapis.com", "/mcp", GKE_READ_TOOLS),
    McpServer(
        "developer_knowledge",
        "developerknowledge.googleapis.com",
        "/mcp",
        DEVELOPER_KNOWLEDGE_READ_TOOLS,
    ),
)

_SERVERS_BY_NAME = {server.name: server for server in MCP_SERVERS}


def _validate_servers(servers: tuple[McpServer, ...]) -> None:
    """Refuse a table that cannot enforce what it appears to say. Raises at import."""
    names: set[str] = set()
    for index, server in enumerate(servers):
        where = f"MCP_SERVERS[{index}]"
        if server.name in names:
            raise ValueError(f"{where}: server name {server.name!r} is already used")
        names.add(server.name)
        if not server.name or "/" in server.name:
            raise ValueError(f"{where}: server name must be one non-empty path segment")
        if not server.host.endswith(".googleapis.com"):
            raise ValueError(f"{where}: host {server.host!r} is not a Google API host")
        if not server.path.startswith("/"):
            raise ValueError(f"{where}: path {server.path!r} must start with /")
        if not server.tools or not all(isinstance(tool, str) and tool for tool in server.tools):
            raise ValueError(f"{where}: tools must be a non-empty set of names")


_validate_servers(MCP_SERVERS)


class _DuplicateKey(ValueError):
    """A JSON object named the same key twice."""


def _refuse_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`object_pairs_hook` that refuses a repeated key.

    Python keeps the last of two equal keys and other parsers keep the first,
    so `{"name": "get_cluster", "name": "patch_k8s_resource"}` would be
    evaluated as one tool and could be run upstream as the other. The broker
    forwards its own re-serialisation, so the upstream sees what was evaluated
    either way; this refuses the request outright, rather than relying on that.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def server(name: str) -> McpServer | None:
    """The table entry for `name`, or None."""
    return _SERVERS_BY_NAME.get(name)


def parse(body: bytes) -> dict[str, Any] | None:
    """The request as one JSON object, or None if it is not exactly that.

    None for: bytes that are not UTF-8 JSON, a repeated key at any depth, a
    batch (a JSON array; refused rather than evaluated member by member, because
    one reply per refused member is a second protocol), or any other top-level
    value. Never raises.
    """
    try:
        message = json.loads(body.decode("utf-8"), object_pairs_hook=_refuse_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(message, dict):
        return None
    return message


def evaluate(server_name: str, message: dict[str, Any] | None) -> Decision:
    """Allow or refuse one relayed JSON-RPC message.

    Answers in this order:

    1. Server not in MCP_SERVERS -> ``gcp.mcp.server``.
    2. Not one JSON object (see `parse`), or `method` not a string ->
       ``gcp.mcp.unreadable-request``.
    3. Method not in RELAYED_METHODS -> ``gcp.mcp.method``.
    4. ``tools/call`` whose ``params.name`` is not a string in that server's
       set -> ``gcp.mcp.read-only``. A tool the server has never heard of
       answers the same way as a write tool: absence from the table is the
       refusal, so there is no list of tools to forget to add a new one to.
    5. Otherwise allowed, carrying the method for the audit line.

    Never raises.
    """
    entry = server(server_name)
    if entry is None:
        return Decision(
            allowed=False,
            rule_id=RULE_SERVER,
            message=(
                "This MCP server is not one the credential proxy relays. Servers are "
                "added to mcp_policy.MCP_SERVERS by pull request."
            ),
        )
    if message is None or not isinstance(message.get("method"), str):
        return Decision(
            allowed=False,
            rule_id=RULE_UNREADABLE,
            message=(
                "The credential proxy relays one JSON-RPC message per request: a single "
                "object with no repeated keys and a string method. Batches are refused."
            ),
        )
    method = message["method"]
    if method not in RELAYED_METHODS:
        return Decision(
            allowed=False,
            rule_id=RULE_METHOD,
            message=(
                "The credential proxy relays only initialize, ping, tools/list and "
                "tools/call to remote MCP servers."
            ),
        )
    if method == METHOD_TOOLS_CALL:
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or name not in entry.tools:
            return Decision(
                allowed=False,
                rule_id=RULE_READ_ONLY,
                message=(
                    f"The credential proxy allows only the read-only tools of the "
                    f"{entry.name} MCP server; this tool is not one of them. Changes to "
                    f"cloud resources go through a pull request, not an MCP tool."
                ),
            )
    return Decision(allowed=True, rule_id=method, message="")


def refusal(message: dict[str, Any] | None, decision: Decision) -> dict[str, Any] | None:
    """The JSON-RPC reply that carries a refusal to the model, or None.

    A refused ``tools/call`` answers as a tool *result* with ``isError: true``:
    that is what Hermes hands the model as the call's output, so the model reads
    SECURITY_POLICY_BLOCKED and the rule id where it reads every other tool
    failure. Any other refused request with an id answers as a JSON-RPC error.
    A notification (no id) gets None, and the handler answers HTTP 403 with no
    JSON-RPC body, because JSON-RPC forbids replying to one.
    """
    request_id = message.get("id") if message is not None else None
    if message is None:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": None,
            "error": {"code": JSONRPC_INVALID_REQUEST, "message": _blocked_text(decision)},
        }
    if request_id is None:
        return None
    if message.get("method") == METHOD_TOOLS_CALL:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": _blocked_text(decision)}],
            },
        }
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": JSONRPC_METHOD_NOT_FOUND, "message": _blocked_text(decision)},
    }


def _blocked_text(decision: Decision) -> str:
    """The text the model reads: the code, the message, and the rule on its own line.

    The same three parts, in the same order, that `credential_proxy_client`
    prints for a refused command, so one pattern reads both.
    """
    return f"SECURITY_POLICY_BLOCKED: {decision.message}\npolicy rule: {decision.rule_id}"


def filter_tools_list(server_name: str, body: bytes) -> bytes | None:
    """The upstream `tools/list` reply with every tool outside the table removed.

    Returns the re-serialised reply, or None when the body is not a JSON-RPC
    result carrying a `tools` array, which the handler answers as an upstream
    fault rather than passing on unfiltered. A JSON-RPC *error* reply has no
    tools to filter and comes back unchanged. A `nextCursor` is dropped: a later
    page would be fetched with a `tools/list` that this filters too, but no
    server in the table pages, and dropping it keeps that an assumption that
    fails closed.
    """
    entry = server(server_name)
    if entry is None:
        return None
    try:
        reply = json.loads(body.decode("utf-8"), object_pairs_hook=_refuse_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(reply, dict):
        return None
    if "error" in reply and "result" not in reply:
        return body
    result = reply.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return None
    result["tools"] = [
        tool
        for tool in result["tools"]
        if isinstance(tool, dict) and tool.get("name") in entry.tools
    ]
    result.pop("nextCursor", None)
    return json.dumps(reply, separators=(",", ":")).encode("utf-8")
