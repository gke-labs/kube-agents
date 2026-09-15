"""Unit tests for the gateway pre-flight in tests/e2e/gchat_agent_test.py.

The pre-flight decides whether the suite may post its prompt yet. Three of its four
outcomes are "carry on": no kubectl on PATH, no Deployment on the cluster, and a rollout
that completed. Only a rollout that timed out or blew its progress deadline is a failure,
because that is the one where the agent that would answer the prompt is not the agent the
install just deployed. Getting that split wrong is invisible -- a pre-flight that fails
open turns a broken gateway into a flaky assertion further down the suite, and one that
fails closed reds a nightly run over a cluster nobody was deploying to.

The module under test imports pytest at module scope, which nothing in
requirements-test.txt installs, so it is loaded here with a stub in its place. The stub is
forced even where the real package exists, for the reason
tests/test_e2e_github_repo_resolution.py gives: it keeps the module the same shape under
`make test-python` as in a checkout with tests/e2e/requirements.txt installed.
"""

import importlib.util
import os
import pathlib
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GCHAT_AGENT_TEST = _REPO_ROOT / "tests" / "e2e" / "gchat_agent_test.py"
_MODULE_NAME = "e2e_gchat_agent_test"

_NAMESPACE = "kubeagents-system"
_DEPLOYMENT_NOT_FOUND = 'Error from server (NotFound): deployments.apps "platform-agent-gateway" not found'
_ROLLOUT_TIMED_OUT = "error: timed out waiting for the condition"
_PROGRESS_DEADLINE_EXCEEDED = 'error: deployment "platform-agent-gateway" exceeded its progress deadline'

# The environment the module is loaded under. Three of these reach `int()` at module
# scope, so a developer's shell holding a non-integer would error every test in the class
# rather than fail one; the rest are pinned so the import does not vary by machine.
_IMPORT_ENV = {
    "GATEWAY_ROLLOUT_TIMEOUT_SEC": "900",
    "TEST_TIMEOUT_SEC": "120",
    "POLL_INTERVAL_SEC": "5",
    "AGENT_NAMESPACE": _NAMESPACE,
}


class _Failed(BaseException):
    """Stands in for pytest's Failed.

    It derives from BaseException, as the real one does, so a bare `except Exception`
    anywhere between pytest.fail and the test runner cannot turn a failure into a pass.
    """


def _pytest_stub() -> types.ModuleType:
    """A stand-in for pytest, which requirements-test.txt does not install.

    gchat_agent_test.py touches two pytest attributes at import time -- the `fixture`
    decorator on four module-scope fixtures, and `fail` inside the pre-flight. `main` is
    reached only under `if __name__ == "__main__"`, so importing the module never calls
    it. A third attribute added at module scope later fails the import naming it.
    """
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *args, **kwargs: (lambda func: func)
    stub.fail = _fail
    stub.Failed = _Failed
    return stub


def _fail(msg: str) -> None:
    raise _Failed(msg)


def _load_gchat_agent_test() -> types.ModuleType:
    """Imports the e2e module by path -- tests/e2e is not an importable package.

    os.environ is replaced rather than merged: the module reads its coordinates at import
    time, and a value inherited from the developer's shell would otherwise decide what the
    loaded module contains. sys.modules is restored so nothing that runs afterwards picks
    the stub up as pytest.
    """
    with mock.patch.dict(sys.modules, {"pytest": _pytest_stub()}), mock.patch.dict(
        os.environ, _IMPORT_ENV, clear=True
    ):
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _GCHAT_AGENT_TEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class GchatAgentPreflightTest(unittest.TestCase):
    """wait_for_gateway_deployment_ready: which rollout outcomes stop the suite."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gchat = _load_gchat_agent_test()

    def _preflight(self, **run_kwargs):
        """Runs the pre-flight with `subprocess.run` pinned, and hands back the mock."""
        with mock.patch.object(self.gchat.subprocess, "run", **run_kwargs) as mock_run:
            self.gchat.wait_for_gateway_deployment_ready(namespace=_NAMESPACE)
            return mock_run

    def _preflight_failure(self, stderr: str) -> str:
        """Runs the pre-flight expecting it to fail, and hands back the message."""
        result = mock.MagicMock(returncode=1, stdout="", stderr=stderr)
        with mock.patch.object(self.gchat.subprocess, "run", return_value=result):
            with self.assertRaises(_Failed) as ctx:
                self.gchat.wait_for_gateway_deployment_ready(namespace=_NAMESPACE)
        return str(ctx.exception)

    def test_skips_when_kubectl_not_found(self):
        """No kubectl on PATH is a local run, not a broken gateway."""
        self._preflight(side_effect=FileNotFoundError)

    def test_skips_when_the_rollout_wait_times_out_in_the_subprocess(self):
        """The subprocess timeout fires before kubectl's own --timeout reports anything."""
        self._preflight(side_effect=self.gchat.subprocess.TimeoutExpired(cmd="kubectl", timeout=1))

    def test_skips_when_deployment_not_found(self):
        """A missing Deployment is the suite's own assertions' business, not the pre-flight's."""
        self._preflight(return_value=mock.MagicMock(returncode=1, stdout="", stderr=_DEPLOYMENT_NOT_FOUND))

    def test_succeeds_when_rollout_passes(self):
        mock_run = self._preflight(
            return_value=mock.MagicMock(returncode=0, stdout="deployment successfully rolled out", stderr="")
        )
        self.assertEqual(mock_run.call_count, 1)
        self.assertIn("rollout", mock_run.call_args[0][0])

    def test_fails_when_rollout_times_out(self):
        self.assertIn("Gateway deployment rollout failed", self._preflight_failure(_ROLLOUT_TIMED_OUT))

    def test_fails_when_the_deployment_blows_its_progress_deadline(self):
        """The other stall kubectl reports, and the one a crash-looping gateway produces."""
        self.assertIn(
            "Gateway deployment rollout failed",
            self._preflight_failure(_PROGRESS_DEADLINE_EXCEEDED),
        )

    def test_failure_is_not_catchable_as_an_ordinary_exception(self):
        """pytest.fail raises out of BaseException; an AssertionError would be swallowed
        by any `except Exception` between here and the runner."""
        result = mock.MagicMock(returncode=1, stdout="", stderr=_ROLLOUT_TIMED_OUT)
        with mock.patch.object(self.gchat.subprocess, "run", return_value=result):
            with self.assertRaises(_Failed):
                try:
                    self.gchat.wait_for_gateway_deployment_ready(namespace=_NAMESPACE)
                except Exception:  # noqa: BLE001 - the point of the test
                    self.fail("the pre-flight's failure was catchable as an Exception")


if __name__ == "__main__":
    unittest.main()
