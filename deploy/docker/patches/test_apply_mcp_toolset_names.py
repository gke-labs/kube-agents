"""Unit tests for apply_mcp_toolset_names.py against a miniature cli.py.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real anchor is asserted against the shipped image by
verify_mcp_toolset_names.py; these tests cover the applier's own contract.
"""

import tempfile
import unittest
from pathlib import Path

import patchlib
from apply_mcp_toolset_names import CLI_RELATIVE, MARKER, NAMES_ANCHOR, apply

# The shape of the check in cli.py at v2026.9.14: the exclusion set, then the
# comprehension that prints.
CLI_STUB = '''class HermesCLI:
    def __init__(self, toolsets):
        if toolsets and "all" not in toolsets and "*" not in toolsets:
            # Validate each toolset
            mcp_names = set((CLI_CONFIG.get("mcp_servers") or {}).keys())
            invalid = [t for t in toolsets if not validate_toolset(t) and t not in mcp_names]
            if invalid:
                self._console_print(f"[bold red]Warning: Unknown toolsets: {', '.join(invalid)}[/]")
        self.invalid = invalid
'''


def stage(source=CLI_STUB):
    root = Path(tempfile.mkdtemp())
    (root / CLI_RELATIVE).write_text(source)
    return root


class ApplierTest(unittest.TestCase):
    def test_prefixed_aliases_join_the_exclusion(self):
        root = stage()
        apply(root)
        patched = (root / CLI_RELATIVE).read_text()
        self.assertIn(MARKER, patched)
        self.assertIn(NAMES_ANCHOR, patched, "the original assignment must survive; the aliases are added beside it")
        ns = {
            "CLI_CONFIG": {"mcp_servers": {"gke": {}, "platform_control": {}}},
            "validate_toolset": lambda t: t == "hermes-cli",
        }
        exec(compile(patched, "cli.py", "exec"), ns)
        ns["HermesCLI"]._console_print = lambda self, *a, **k: None
        cli = ns["HermesCLI"](["hermes-cli", "mcp-gke", "mcp-platform_control", "mcp-nowhere", "gke"])
        self.assertEqual(cli.invalid, ["mcp-nowhere"])

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_is_refused(self):
        root = stage(CLI_STUB.replace("mcp_names = set(", "mcp_names = frozenset("))
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertNotIn(MARKER, (root / CLI_RELATIVE).read_text())


if __name__ == "__main__":
    unittest.main()
