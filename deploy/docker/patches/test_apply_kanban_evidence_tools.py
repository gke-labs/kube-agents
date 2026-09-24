"""Unit tests for the evidence-tools applier against upstream's table-driven registration.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPLIER = HERE / "apply_kanban_evidence_tools.py"
RELATIVE = "tools/kanban_tools.py"
REGISTERED = "_register_evidence_tools(registry, _check_kanban_worker_mode, tool_error)"

# The shape of upstream's tools/kanban_tools.py since Hermes v2026.9.14, after
# apply_kanban_worker_tools.py has run: gates, the worker patch's import block,
# a handler, a module-level _TOOLS tuple of rows, and the one registration loop.
WORKER_IMPORT = textwrap.dedent(
    '''
    # kube-agents patch: see tools/kanban_worker_tools.py
    from tools.kanban_worker_tools import (
        WORKER_ONLY_TOOLS as _WORKER_ONLY_TOOLS,
        check_kanban_worker_mode as _check_kanban_worker_mode,
    )

    no_cache_check_fn(_check_kanban_worker_mode)
    '''
)

SAMPLE = textwrap.dedent(
    '''
    """Sample of upstream tools/kanban_tools.py."""
    from tools.registry import no_cache_check_fn, registry, tool_error


    def _check_kanban_mode():
        return True


    def _check_kanban_orchestrator_mode():
        return True
    {worker_import}

    def _handle_show(args, **kw):
        return "{{}}"


    KANBAN_SHOW_SCHEMA = {{"name": "kanban_show"}}
    _ORCHESTRATOR_TOOLS = frozenset({{"kanban_create"}})

    _TOOLS = (
        ("{tool}", KANBAN_SHOW_SCHEMA, _handle_show, "📋"),
    )

    for _name, _schema, _handler, _emoji in _TOOLS:
        _gate = _check_kanban_orchestrator_mode if _name in _ORCHESTRATOR_TOOLS else _check_kanban_mode
        registry.register(
            name=_name, toolset="kanban", schema=_schema, handler=_handler, check_fn=_gate, emoji=_emoji
        )
    '''
)


def run_applier(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APPLIER), str(root)], capture_output=True, text=True
    )


class ApplierTest(unittest.TestCase):
    def write_sample(self, *, worker_import: str = WORKER_IMPORT, tool: str = "kanban_show") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "tools").mkdir()
        (root / RELATIVE).write_text(
            SAMPLE.format(worker_import=worker_import, tool=tool), encoding="utf-8"
        )
        return root

    def test_block_lands_before_the_tool_table_and_the_file_still_compiles(self) -> None:
        root = self.write_sample()
        result = run_applier(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        patched = (root / RELATIVE).read_text(encoding="utf-8")
        self.assertIn(REGISTERED, patched)
        # After the worker import that binds the gate, before the table whose
        # loop registers upstream's tools.
        self.assertLess(
            patched.index("check_kanban_worker_mode as _check_kanban_worker_mode"),
            patched.index(REGISTERED),
        )
        self.assertLess(patched.index(REGISTERED), patched.index("_TOOLS = ("))
        compile(patched, RELATIVE, "exec")

    def test_refuses_to_run_before_the_worker_gate_exists(self) -> None:
        root = self.write_sample(worker_import="")
        result = run_applier(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run apply_kanban_worker_tools.py first", result.stderr)

    def test_refuses_a_second_run(self) -> None:
        root = self.write_sample()
        self.assertEqual(run_applier(root).returncode, 0)
        second = run_applier(root)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already patched", second.stderr)

    def test_refuses_a_table_that_is_not_the_kanban_table(self) -> None:
        root = self.write_sample(tool="some_other_tool")
        result = run_applier(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("carries no kanban_show row", result.stderr)


if __name__ == "__main__":
    unittest.main()
