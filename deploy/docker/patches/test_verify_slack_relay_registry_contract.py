#!/usr/bin/env python3
"""Unit tests for verify_slack_relay_registry_contract.py."""

import inspect
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import verify_slack_relay_registry_contract as contract


class VerifySlackRelayContractTest(unittest.TestCase):
    def test_shape_formatting(self):
        def sample_fn(self, a, b=1, *, c=False, **kwargs):
            pass

        self.assertEqual(contract.shape(sample_fn), "self, a, b=..., *, c=..., **kwargs")

    def test_shape_with_varargs(self):
        def sample_varargs(self, *args, x=None):
            pass

        self.assertEqual(contract.shape(sample_varargs), "self, *args, x=...")

    def test_pinned_defaults_table_validates(self):
        # Verify that all entries in PINNED_DEFAULTS are well-formed tuples
        for key, val in contract.PINNED_DEFAULTS.items():
            self.assertIsInstance(key, tuple)
            self.assertEqual(len(key), 2)
            label, param_name = key
            self.assertIn(label, contract.UPSTREAM_SIGNATURES)
            self.assertIn(f"{param_name}=...", contract.UPSTREAM_SIGNATURES[label])


if __name__ == "__main__":
    unittest.main()
