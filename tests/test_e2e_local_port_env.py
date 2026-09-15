"""Unit tests for the agent-tunnel local port in tests/e2e/conftest.py.

bench and this suite both reached for AGENT_LOCAL_PORT and meant different
things by it (#971, finding 7): bench defaults it to a fixed 8642 and dials the
URL it builds from the number, while this suite defaults it to 0 because a fixed
port turns a listener left behind by an earlier run into "address already in
use". The mitigation was a warning in tests/e2e/.env.example telling the reader
not to set it to 0; the suite now reads E2E_AGENT_LOCAL_PORT instead, and the
warning is gone because the name is no longer shared.

What is worth a regression test is the part a rename usually gets wrong: what
happens to the configuration that used to work. AGENT_LOCAL_PORT is not read,
and the run says so rather than quietly binding somewhere else.

Run under discovery from tests/, which is what the Makefile sweep does and what
puts the sibling module below on the path:

    python3 -m unittest discover -s tests -p test_e2e_local_port_env.py
"""

import os
import pathlib
import unittest
import warnings
from unittest import mock

from test_e2e_github_repo_resolution import _load_conftest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CONFTEST = _REPO_ROOT / "tests" / "e2e" / "conftest.py"
_ENV_EXAMPLE = _REPO_ROOT / "tests" / "e2e" / ".env.example"


class LocalPortTest(unittest.TestCase):
    """_local_port reads the suite's own variable and reports the shared one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conftest = _load_conftest()

    def _resolve(self, env):
        """Returns (port, warning texts). The environment is cleared, not extended."""
        with mock.patch.dict(os.environ, env, clear=True):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                port = self.conftest._local_port()
        return port, [str(entry.message) for entry in caught]

    def test_unset_takes_an_ephemeral_port(self):
        port, warned = self._resolve({})
        self.assertEqual(port, 0)
        self.assertEqual(warned, [])

    def test_suite_variable_pins_the_port(self):
        port, warned = self._resolve({"E2E_AGENT_LOCAL_PORT": "18642"})
        self.assertEqual(port, 18642)
        self.assertEqual(warned, [])

    def test_bench_variable_is_reported_and_not_honoured(self):
        """The rename's whole risk: an AGENT_LOCAL_PORT that used to be read."""
        port, warned = self._resolve({"AGENT_LOCAL_PORT": "8642"})
        self.assertEqual(port, 0)
        self.assertEqual(len(warned), 1)
        self.assertIn("AGENT_LOCAL_PORT", warned[0])
        self.assertIn("E2E_AGENT_LOCAL_PORT", warned[0])

    def test_no_warning_when_the_suite_variable_is_also_set(self):
        """Both set is a deliberate configuration, not a leftover."""
        port, warned = self._resolve(
            {"AGENT_LOCAL_PORT": "8642", "E2E_AGENT_LOCAL_PORT": "18642"}
        )
        self.assertEqual(port, 18642)
        self.assertEqual(warned, [])

    def test_empty_suite_variable_falls_back_and_still_warns(self):
        """An empty k8s env value, which _numeric_env already treats as absent."""
        port, warned = self._resolve(
            {"AGENT_LOCAL_PORT": "8642", "E2E_AGENT_LOCAL_PORT": ""}
        )
        self.assertEqual(port, 0)
        self.assertEqual(len(warned), 1)

    def test_non_numeric_names_the_suite_variable(self):
        with self.assertRaises(ValueError) as caught:
            self._resolve({"E2E_AGENT_LOCAL_PORT": "not-a-port"})
        self.assertIn("E2E_AGENT_LOCAL_PORT", str(caught.exception))


class WiringTest(unittest.TestCase):
    """The helper above is only worth testing if the fixture goes through it.

    Asserted on source text, the idiom tests/test_execute_e2e_tests.py uses on
    the same file for the same reason: the fixture needs a live cluster, so no
    unit test can call it and watch which variable it reads.
    """

    def test_the_fixture_takes_its_port_from_the_helper(self):
        source = _CONFTEST.read_text(encoding="utf-8")
        self.assertIn(
            "local_port = _local_port()",
            source,
            "port_forward_agent must resolve its local port through _local_port, "
            "or the tests above cover a helper nothing calls",
        )
        self.assertNotIn(
            "local_port = _numeric_env",
            source,
            "the local port must come from _local_port, whichever variable name a "
            "direct _numeric_env read would pass -- a sentinel left in a comment "
            "would otherwise satisfy the assertion above",
        )
        self.assertNotIn(
            '_numeric_env("AGENT_LOCAL_PORT"',
            source,
            "the suite must not read the name bench owns; _LOCAL_PORT_ENV is the "
            "one it reads and _LEGACY_LOCAL_PORT_ENV the one it only reports",
        )


class EnvExampleTest(unittest.TestCase):
    """The template a reader copies documents the name the suite reads."""

    def test_example_documents_the_suite_variable(self):
        example = _ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("# E2E_AGENT_LOCAL_PORT=", example)
        self.assertNotIn("# AGENT_LOCAL_PORT=", example)


if __name__ == "__main__":
    unittest.main()
