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

The block is located with ``ast`` rather than by text, so an upstream reformat
(the ``invalid`` comprehension wrapped over several lines, the check moved to
a different indent) is reported through ``fail()`` or handled, never as a
traceback out of a build step.

Usage::

    cd /opt/hermes && python3 verify_mcp_toolset_names.py
"""

from __future__ import annotations

import ast
import os
import sys
import textwrap
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _assigns_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Assign) and len(node.targets) == 1 and \
        isinstance(node.targets[0], ast.Name) and node.targets[0].id == name


def patched_block(source: str) -> str:
    """The statements from the ``mcp_names`` assignment to the ``invalid`` list.

    Located by the two assignments' positions in the parsed module, so the slice
    ends where the ``invalid`` statement ends however many lines it spans.
    Returns "" (after recording why) when either statement is not there.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        fail(f"cli.py does not parse: {exc}")
        return ""
    start = next((n for n in ast.walk(tree)
                  if _assigns_to(n, "mcp_names") and "CLI_CONFIG" in ast.unparse(n.value)), None)
    if start is None:
        fail("the mcp_names assignment is gone from cli.py; the anchor moved")
        return ""
    end = next((n for n in ast.walk(tree)
                if _assigns_to(n, "invalid") and n.lineno > start.lineno and n.col_offset == start.col_offset), None)
    if end is None:
        fail("the invalid list is gone from cli.py; the check moved")
        return ""
    lines = source.splitlines(keepends=True)
    block = textwrap.dedent("".join(lines[start.lineno - 1 : end.end_lineno]))
    if "_kube_mcp_name" not in block:
        fail("the patch marker is not inside the block; the aliases are excluded somewhere else or not at all")
    return block


def run_block(block: str, toolsets: list[str], servers: list[str]) -> list[str] | None:
    """Execute the block with a validate_toolset that knows no MCP toolset.

    None when the block does not run, with the reason recorded.
    """
    try:
        code = compile(block, "<patched-block>", "exec")
    except SyntaxError as exc:
        fail(f"the patched block does not compile on its own: {exc}")
        return None
    ns = {
        "CLI_CONFIG": {"mcp_servers": {s: {} for s in servers}},
        "toolsets": toolsets,
        "validate_toolset": lambda t: t in {"hermes-cli", "memory"},
    }
    try:
        exec(code, ns)
    except Exception as exc:  # noqa: BLE001 -- any failure is the build's to see
        fail(f"the patched block raised when run: {exc!r}")
        return None
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
        if invalid is not None and invalid != ["mcp-nowhere"]:
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
