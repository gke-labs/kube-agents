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

# The v2026.9.14 shape of tools/approval.py's two guard entry points, reduced
# to something that parses and runs on its own. Byte-identical to upstream for
# the anchored lines: the ``if not is_cli ...`` / ``for ctx in
# _unattended_contexts():`` / ``result = _unattended_deny(command, ctx)`` triple
# that apply_cron_tirith_scan.py inserts ahead of, and the ``for ctx`` /
# ``if ctx.mode() == "deny":`` / ``return _denied(`` triple in
# check_execute_code_guard. ``_Unattended.mode()`` resolves the getter on the
# approval_context module at call time, as upstream's does, because that is
# what the verifier pins.
UPSTREAM = """\
from tools import approval_context


def _format_tirith_description(result):
    return "tirith finding"


def _approved():
    return {"approved": True, "message": None}


def _denied(message, *, pattern_key, description, outcome):
    return {"approved": False, "message": message}


class _Unattended:
    def __init__(self, name):
        self.name = name

    def mode(self):
        return getattr(approval_context, f"_get_{self.name}_approval_mode")()


_CRON_CTX = _Unattended("cron")


def _unattended_contexts():
    contexts = []
    if _is_cron_approval_context():
        contexts.append(_CRON_CTX)
    return contexts


def _unattended_deny(command, ctx):
    if ctx.mode() != "deny":
        return None
    is_dangerous, _pk, description = detect_dangerous_command(command)
    if is_dangerous:
        return {"approved": False, "message": "dangerous: cron jobs run without a user present"}
    return {"approved": False, "message": "cron jobs run without a user present"}


def check_all_command_guards(command, env_type, approval_callback=None, has_host_access=False):
    is_cli = _is_interactive_cli()
    is_gateway = _is_gateway_approval_context()
    is_ask = False
    # Outside CLI/gateway/ask flows we never block on approvals: each
    # unattended context applies its configured deny/approve mode, else allow.
    if not is_cli and not is_gateway and not is_ask:
        for ctx in _unattended_contexts():
            result = _unattended_deny(command, ctx)
            if result is not None:
                return result
        return _approved()
    return _approved()


def check_execute_code_guard(code, env_type, has_host_access=False):
    pattern_key = "execute_code"
    description = "execute_code script execution"
    is_gateway = _is_gateway_approval_context()
    # No user is present to approve arbitrary code in -q / cron / unattended
    # sessions: the first active context resolves instantly from its mode.
    for ctx in _unattended_contexts():
        if ctx.mode() == "deny":
            return _denied(
                "BLOCKED: execute_code runs arbitrary local Python.",
                pattern_key=pattern_key, description=description, outcome="blocked",
            )
        return _approved()
    return _approved()
"""

# The sibling module the fixture imports the mode getters from. The verifier
# replaces both attributes; the defaults here are upstream's (deny, manual).
APPROVAL_CONTEXT_STUB = """\
def _get_cron_approval_mode():
    return "deny"


def _get_approval_mode():
    return "manual"
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
        self.assertIn("cron_command_policy_block(command, _cron_risk)", patched)
        self.assertIn("cron_execute_code_block()", patched)
        # The execute_code refusal lands ahead of upstream's per-context loop,
        # which would otherwise approve the script under cron_mode: approve.
        self.assertLess(
            patched.index("cron_execute_code_block()"),
            patched.index('        if ctx.mode() == "deny":\n            return _denied('),
        )

    def test_the_risk_gate_needs_the_tirith_scan_first(self):
        """Its first anchor is text apply_cron_tirith_scan.py inserts."""
        root, _ = self.write_tree(UPSTREAM)
        with self.assertRaises(SystemExit) as caught:
            apply_cron_risk_gate.apply(root)
        self.assertIn("found 0", str(caught.exception))

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
        (tools_dir / "approval_context.py").write_text(APPROVAL_CONTEXT_STUB)
        for mod_name in ("cron_risk_gate.py", "cron_run_scope.py", "cron_tirith_scan.py"):
            (tools_dir / mod_name).write_text((patches_dir / mod_name).read_text())
        cmd_policy = patches_dir.parents[2] / "agents" / "platform" / "scripts" / "command_policy.py"
        if cmd_policy.exists():
            (tools_dir / "command_policy.py").write_text(cmd_policy.read_text())

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
