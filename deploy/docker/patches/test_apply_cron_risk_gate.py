"""Unit tests for apply_cron_risk_gate.py and build-time verify_cron_risk_gate.py."""

from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile
import unittest

import apply_cron_risk_gate
import apply_cron_tirith_scan

UPSTREAM = """\
def _format_tirith_description(result):
    return "tirith finding"

def _is_single_query_approval_context():
    return False

def check_all_command_guards(command, env_type, approval_callback=None, has_host_access=False):
    is_cli = _is_interactive_cli()
    is_gateway = _is_gateway_approval_context()
    is_ask = False
    if not is_cli and not is_gateway and not is_ask:
        if _is_single_query_approval_context():
            if _get_single_query_approval_mode() == "deny":
                return {"approved": False, "message": "single-query denied"}
            # single_query_mode: approve — fall through to auto-approve below.
        # Cron sessions: respect cron_mode config
        if _is_cron_approval_context():
            if _get_cron_approval_mode() == "deny":
                # Run detection to get a description for the block message
                is_dangerous, _pk, description = detect_dangerous_command(command)
                if is_dangerous:
                    return {"approved": False, "message": "dangerous: cron jobs run without a user present"}
                return {"approved": False, "message": "cron jobs run without a user present"}
        return {"approved": True, "message": None}
    return {"approved": True, "message": None}

def check_execute_code_guard(code, env_type, has_host_access=False):
    is_gateway = _is_gateway_approval_context()
    is_ask = False
    # Cron: no user is present to approve arbitrary code.
    if _is_cron_approval_context():
        if _get_cron_approval_mode() == "deny":
            return {"approved": False, "message": "denied"}
        return {"approved": True, "message": None}
    return {"approved": True, "message": None}
"""


class ApplyCronRiskGateTest(unittest.TestCase):
    def write_tree(self, source: str) -> tuple[pathlib.Path, pathlib.Path]:
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: [p.unlink() for p in sorted(root.rglob("*")) if p.is_file()]
        )
        target = root / "tools" / "approval.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        return root, target

    def test_applier_patches_cleanly_and_parses(self):
        root, target = self.write_tree(UPSTREAM)
        # Apply tirith scan first
        apply_cron_tirith_scan.apply(root)
        # Then apply risk gate
        apply_cron_risk_gate.apply(root)

        patched = target.read_text()
        ast.parse(patched)

        self.assertIn("from tools.cron_risk_gate import", patched)
        self.assertIn("cron_effective_mode(_cron_mode, current_cron_risk())", patched)
        self.assertIn("cron_execute_code_block()", patched)

    def test_the_patch_is_not_applied_twice(self):
        root, _ = self.write_tree(UPSTREAM)
        apply_cron_tirith_scan.apply(root)
        apply_cron_risk_gate.apply(root)
        with self.assertRaises(SystemExit) as caught:
            apply_cron_risk_gate.apply(root)
        self.assertIn("already patched", str(caught.exception))

    def test_verification_script_passes_against_patched_module(self):
        root, target = self.write_tree(UPSTREAM)
        apply_cron_tirith_scan.apply(root)
        apply_cron_risk_gate.apply(root)

        patches_dir = pathlib.Path(__file__).parent.resolve()
        tools_dir = root / "tools"
        (tools_dir / "__init__.py").write_text("")
        for mod_name in ("cron_risk_gate.py", "cron_run_scope.py", "cron_tirith_scan.py"):
            (tools_dir / mod_name).write_text((patches_dir / mod_name).read_text())

        sys_path_orig = list(sys.path)
        sys.path.insert(0, str(patches_dir))
        sys.path.insert(0, str(root))

        # Clear any cached tools modules
        for mod in list(sys.modules.keys()):
            if mod.startswith("tools.") or mod == "tools":
                del sys.modules[mod]

        try:
            import verify_cron_risk_gate
            rc = verify_cron_risk_gate.main()
            self.assertEqual(rc, 0)
        finally:
            sys.path[:] = sys_path_orig
            for mod in list(sys.modules.keys()):
                if mod.startswith("tools.") or mod == "tools":
                    del sys.modules[mod]


if __name__ == "__main__":
    unittest.main()
