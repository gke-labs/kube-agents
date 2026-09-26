#!/usr/bin/env python3
"""Build gate for the MCP toolset-names patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_mcp_toolset_names.py``. The applier proves its anchor matched once;
this proves the patched check behaves: with ``mcp_servers`` naming ``gke``, a
toolset list carrying ``mcp-gke`` is not reported unknown, a toolset nobody
configured still is, and the bare name upstream already excluded still is
too. Driven against the real ``cli.py`` text by executing the patched block
with a stand-in ``validate_toolset`` that knows no MCP toolset, which is the
pre-discovery state the check runs in.

Usage::

    cd /opt/hermes && python3 verify_mcp_toolset_names.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def patched_block(source: str) -> str:
    """The lines from the ``mcp_names`` assignment to the ``invalid`` list."""
    lines = source.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if "mcp_names = set((CLI_CONFIG.get" in l), None)
    if start is None:
        fail("the mcp_names assignment is gone from cli.py; the anchor moved")
        return ""
    end = next((i for i in range(start, len(lines)) if "invalid = [" in lines[i]), None)
    if end is None:
        fail("the invalid list is gone from cli.py; the check moved")
        return ""
    block = "".join(lines[start : end + 1])
    if "_kube_mcp_name" not in block:
        fail("the patch marker is not inside the block; the aliases are excluded somewhere else or not at all")
    return block


def run_block(block: str, toolsets: list[str], servers: list[str]) -> list[str]:
    """Execute the block with a validate_toolset that knows no MCP toolset."""
    code = ast.parse("\n".join(l[12:] if l.startswith(" " * 12) else l for l in block.splitlines()))
    ns = {
        "CLI_CONFIG": {"mcp_servers": {s: {} for s in servers}},
        "toolsets": toolsets,
        "validate_toolset": lambda t: t in {"hermes-cli", "memory"},
    }
    exec(compile(code, "<patched-block>", "exec"), ns)
    return list(ns["invalid"])


def main() -> int:
    cli = HERMES / "cli.py"
    source = cli.read_text()
    block = patched_block(source)
    if block:
        try:
            compile(source, str(cli), "exec")
        except SyntaxError as exc:
            fail(f"cli.py does not compile after the patch: {exc}")
        invalid = run_block(block, ["hermes-cli", "mcp-gke", "mcp-platform_control", "memory"], ["gke", "platform_control"])
        if invalid:
            fail(f"configured MCP toolsets still report unknown: {invalid}")
        invalid = run_block(block, ["hermes-cli", "mcp-nowhere"], ["gke"])
        if invalid != ["mcp-nowhere"]:
            fail(f"an unconfigured MCP toolset is no longer reported: {invalid}")
        invalid = run_block(block, ["gke"], ["gke"])
        if invalid:
            fail(f"the bare server name upstream excluded is reported now: {invalid}")
    if FAILURES:
        for f in FAILURES:
            print(f"verify_mcp_toolset_names: {f}", file=sys.stderr)
        return 1
    print("verify_mcp_toolset_names: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
