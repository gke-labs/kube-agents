"""Unit tests for upgrade.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin execution, and source ref alignment in upgrade.sh.
"""

import os
import pathlib
import subprocess
import unittest

from tests.testing.common import (
    INVALID_IMMUTABLE_REFS,
    UPGRADER_HELP_BANNER,
    VALID_IMMUTABLE_REFS,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"


class UpgradeScriptValidationTest(unittest.TestCase):
    def _run_upgrade_func(self, func_call, env=None, cwd=None):
        """Source upgrade.sh in test mode and run the given function call."""
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
        )

    def test_validate_immutable_ref_accepts_valid_refs(self):
        for ref in VALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_upgrade_func(cmd)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"upgrade.sh: expected ref '{ref}' to be valid, stderr: {proc.stderr}",
                )

    def test_validate_immutable_ref_rejects_invalid_refs(self):
        for ref in INVALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_upgrade_func(cmd)
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"upgrade.sh: expected ref '{ref}' to be rejected",
                )

    def test_piped_stdin_executes_main(self):
        """Ensures piped curl | bash invocations execute main and do not exit early."""
        upgrade_script_content = _UPGRADE_SH.read_text()
        proc = subprocess.run(
            ["bash", "-s", "--", "--help"],
            input=upgrade_script_content,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, f"Piped execution failed: {proc.stderr}")
        self.assertIn(UPGRADER_HELP_BANNER, proc.stdout)


if __name__ == "__main__":
    unittest.main()
