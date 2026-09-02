"""Tests for runtime_mode, the single agent-side reader of the mode switch."""

import os
import unittest
from unittest import mock

import runtime_mode


class TestRuntimeMode(unittest.TestCase):
    def test_absent_is_today(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(runtime_mode.mode(), "today")
            self.assertFalse(runtime_mode.is_next())

    def test_next(self):
        with mock.patch.dict(os.environ, {"KUBEAGENTS_MODE": "next"}):
            self.assertEqual(runtime_mode.mode(), "next")
            self.assertTrue(runtime_mode.is_next())

    def test_today_explicit(self):
        with mock.patch.dict(os.environ, {"KUBEAGENTS_MODE": "today"}):
            self.assertEqual(runtime_mode.mode(), "today")
            self.assertFalse(runtime_mode.is_next())

    def test_unrecognized_fails_closed(self):
        # The same rule as the operator's renderMode: a value this build does
        # not know means the dark stack stays dark.
        with mock.patch.dict(os.environ, {"KUBEAGENTS_MODE": "quantum"}):
            self.assertEqual(runtime_mode.mode(), "today")
            self.assertFalse(runtime_mode.is_next())


if __name__ == "__main__":
    unittest.main()
