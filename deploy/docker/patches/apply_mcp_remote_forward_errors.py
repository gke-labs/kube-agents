#!/usr/bin/env python3
"""Make mcp-remote answer the client when a forward to the remote fails.

Run by ``deploy/docker/Dockerfile`` against the mcp-remote clone, after
``npm install`` and before ``npm run build``.

``mcpProxy`` forwards every client message with::

    transportToServer.send(message).catch(onServerError)

and ``onServerError`` only logs. ``message.id`` is not even in scope there, so a
rejected forward can never be answered: the local client is left waiting on a
request id that will never come back, and it parks until its own deadline. The
sibling ``ignoredTools`` path thirty lines above gets this right — it builds a
JSON-RPC error and calls ``transportToClient.send`` — because ``request.id`` is
in scope. This patch gives the failure path the same treatment.

The trigger in production is a non-2xx HTTP status. The MCP SDK's
``StreamableHTTPClientTransport.send`` throws on ``!response.ok`` and discards
the body, even when that body is a perfectly good JSON-RPC message; the SDK
*rejects the send promise*, which a direct SDK client surfaces in milliseconds.
It is mcp-remote swallowing that rejection that turns a fast error into a hang.

Measured on task t_b9544b00: ``mcp__gke__list_clusters`` was called with
``parent="projects/-/locations/-"``. ``container.googleapis.com/mcp`` answered
HTTP 403 carrying ``{"id":2,"jsonrpc":"2.0","result":{"content":[{"text":
"Permission denied on resource project -.","type":"text"}],"isError":true}}``.
The proxy logged it and dropped it; the call blocked for 300.002s and a sibling
tool call in the same batch sat finished and unused for the whole window. With
this patch the same scenario returns a JSON-RPC error in single-digit
milliseconds, carrying the remote's own "Permission denied" text so the model
gets an actionable failure instead of silence.

Upstream: geelen/mcp-remote issue #293 describes this exact root cause and PR
#308 fixes it. Both were still open when this patch was written, against a
repository whose last commit was 2026-02-05, so there is nothing to wait for.
Drop this patch once #308 merges and the Dockerfile pin advances past it.

Note this is *not* a fork-vs-upstream problem: upstream ``geelen/mcp-remote``
carries the identical two lines. The pinned fork is used for its Google ADC
support, not because it introduced this bug.

Usage::

    python3 apply_mcp_remote_forward_errors.py [MCP_REMOTE_ROOT]  # /opt/mcp-remote
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "src/lib/utils.ts"

# The only non-Hermes target in this directory, so the standard "bump the base
# image" advice is the wrong advice: what moves this anchor is the mcp-remote
# pin, not a Hermes release.
PIN_NOTE = "mcp-remote changed — re-derive the anchor before advancing the pin."

INDENT = " " * 4

ANCHOR = f"{INDENT}transportToServer.send(message).catch(onServerError)\n"

# Mirrors the `ignoredTools` branch earlier in the same function, which is the
# codebase's own idiom for answering the client directly: same `jsonrpc`/`id`/
# `error` shape, same -32603, same `.catch(onClientError)` tail.
PATCHED = f"""\
{INDENT}// kube-agents patch: see deploy/docker/patches/apply_mcp_remote_forward_errors.py
{INDENT}transportToServer.send(message).catch((error: Error) => {{
{INDENT}  onServerError(error)
{INDENT}  // Upstream stops at the log above, which strands the caller: `message.id`
{INDENT}  // is out of scope inside onServerError, so a request that failed to
{INDENT}  // forward is never answered and the client waits out its own timeout.
{INDENT}  // Only requests get a reply — notifications carry no id, and responses
{INDENT}  // carry no method.
{INDENT}  if (message.id !== undefined && message.id !== null && message.method) {{
{INDENT}    transportToClient
{INDENT}      .send({{
{INDENT}        jsonrpc: '2.0' as const,
{INDENT}        id: message.id,
{INDENT}        error: {{
{INDENT}          code: -32603,
{INDENT}          message: `Failed to forward message to remote server: ${{error.message}}`,
{INDENT}        }},
{INDENT}      }})
{INDENT}      .catch(onClientError)
{INDENT}  }}
{INDENT}}})
"""

# Asserted in the built bundle by the Dockerfile, so a patch that silently stops
# applying fails the image build instead of shipping a hang.
BUILD_MARKER = "Failed to forward message to remote server"


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(
        root, RELATIVE, prefix="mcp_remote_forward_errors", note=PIN_NOTE
    )
    # Not patchlib.refuse_if_patched: the marker being present is more likely to
    # mean upstream merged PR #308 than that this ran twice, and the two have
    # different answers — drop the patch, versus fix the build step. The check
    # has to precede the substitution either way, because the replacement text
    # is where the marker comes from.
    if BUILD_MARKER in patch.source:
        raise SystemExit(
            f"mcp_remote_forward_errors patch: {RELATIVE} already contains "
            f"{BUILD_MARKER!r}. Upstream may have merged PR #308 — drop this "
            f"patch rather than applying it twice."
        )
    patch.substitute(ANCHOR, PATCHED)
    # TypeScript: there is no ast.parse gate to run, `npm run build` is it.
    patch.commit("1 anchor", parse=False)


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/mcp-remote"))
