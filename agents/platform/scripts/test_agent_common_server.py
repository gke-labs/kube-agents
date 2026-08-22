"""Credential handling in agent_common_server.

The module's one tool, `call_agent`, is no longer mounted — no profile declares
the `agent_common` MCP server (see the note above `mcp_servers` in
deploy/shared/defaults/config.yaml). The module itself stays, because
platform_mcp_server and session_kv_server import helpers from it, and so does
this contract: resolve_agent_credentials must fail closed on an unconfigured
key rather than authenticate as a guessable literal. That is the property any
future synchronous delegation path would have to keep, and the reason the tool
failed the way it did rather than reaching the network with a bad key.
"""

import importlib
import importlib.metadata
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

# Add the directory containing agent_common_server.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from session_manager import SessionManager


def _mcp_distribution_installed():
    try:
        importlib.metadata.distribution("mcp")
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _load_agent_common_server():
    """Import the module under test.

    These tests run in CI via `make test-python`, and the credential logic under
    test (resolve_agent_credentials) depends only on the stdlib. When the hermes
    runtime deps (FastMCP / pydantic / session_manager) aren't installed at all,
    fall back to minimal stubs so the module still imports in a bare checkout.
    Each stub package sets __path__ so it is treated as a real package.

    ABSENT is not BROKEN: stub only when no mcp distribution is installed, and
    ask importlib.metadata rather than importlib.util.find_spec. Why, and what
    an installed-but-incompatible mcp means, is in test_mcp_package_contract.py.
    """
    try:
        return importlib.import_module("agent_common_server")
    except Exception:
        if _mcp_distribution_installed():
            raise

        import session_manager as real_session_manager

        def _stub_if_missing(name, module):
            # Stub only a module that really cannot be imported. These entries
            # outlive this file: unittest discovery imports every test module
            # into one process, and a fake pydantic left here (a ModuleType
            # bearing nothing but Field) is what fastapi finds when
            # test_session_kv_server imports it seventeen modules later.
            try:
                importlib.import_module(name)
            except Exception:
                sys.modules[name] = module

        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        _stub_if_missing("mcp", mcp)
        _stub_if_missing("mcp.server", mcp_server)
        _stub_if_missing("mcp.server.fastmcp", fastmcp)
        _stub_if_missing("pydantic", pydantic)
        sys.modules["session_manager"] = real_session_manager
        return importlib.import_module("agent_common_server")


_agent_common_server = _load_agent_common_server()
resolve_agent_credentials = _agent_common_server.resolve_agent_credentials


class TestNoSubprocessAtImport(unittest.TestCase):
    """Importing this module must not spawn a process.

    platform_mcp_server imports agent_common_server, so module scope runs twice
    per platform-profile kanban worker spawn (the agent_common and
    platform_control MCP children) and once more per pod in session_kv_server.
    A spawn from here costs an extra sandboxed interpreter start on every
    worker. See the note in agent_common_server.py for why the load_slack_token()
    shell-out this pins against could not have worked on either path.
    """

    def test_import_spawns_no_subprocess(self):
        import subprocess

        # The helper this pins against was guarded by
        # `if "SLACK_BOT_TOKEN" not in os.environ`, so without clearing that the
        # reload short-circuits and the assertions pass vacuously on any machine
        # where the variable is already set — including one where an earlier,
        # unmocked import populated it. subprocess.run/call/check_call/
        # check_output all route through Popen; os.system is patched separately
        # because it does not.
        with mock.patch.dict(os.environ):
            os.environ.pop("SLACK_BOT_TOKEN", None)
            with mock.patch.object(subprocess, "Popen") as popen, \
                    mock.patch.object(os, "system") as system:
                importlib.reload(_agent_common_server)

        popen.assert_not_called()
        system.assert_not_called()

    def test_load_slack_token_is_gone(self):
        """Regression pin: reintroducing the helper reintroduces the cost on
        every worker spawn, and it never worked. Named separately from the
        subprocess pin because it catches a reintroduction whose own guard
        would short-circuit the reload."""
        self.assertFalse(hasattr(_agent_common_server, "load_slack_token"))


class TestResolveAgentCredentials(unittest.TestCase):
    """API_SERVER_KEY must fail closed — never silently authenticate as a
    guessable literal when the shared secret is unconfigured (MCP-001)."""

    def setUp(self):
        self._saved = os.environ.get("API_SERVER_KEY")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("API_SERVER_KEY", None)
        else:
            os.environ["API_SERVER_KEY"] = self._saved

    def test_raises_when_key_unset(self):
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_empty(self):
        os.environ["API_SERVER_KEY"] = ""
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_whitespace(self):
        os.environ["API_SERVER_KEY"] = "   "
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_never_falls_back_to_none_literal(self):
        """Regression pin: an unconfigured key must fail closed — raise, never
        yield the guessable literal 'none'."""
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError) as ctx:
            resolve_agent_credentials("platform")
        self.assertIn("API_SERVER_KEY is not configured", str(ctx.exception))

    def test_returns_endpoint_and_key_when_set(self):
        os.environ["API_SERVER_KEY"] = "s3cret"
        endpoint, api_key = resolve_agent_credentials("platform")
        self.assertEqual(api_key, "s3cret")
        self.assertIn("8642", endpoint)
        self.assertTrue(endpoint.startswith("https://"))


class TestRunEnvInheritanceContract(unittest.TestCase):
    """`_run_env` hands a child the caller's whole environment.

    Sound only while the sandbox container holds no credentials worth passing
    on. The canonical statement of why that holds is
    docs/credential-isolation-design.md, "The loopback-only exception".

    The Go side already guards the obvious version of this: TestBuildDeployment
    in platformagent_manifests_test.go walks the sandbox container's `env` and
    fails on any entry whose `valueFrom` names a secretKeyRef outside the
    two-name allowlist, and TestAgentsGolden fails alongside it. This is not a
    substitute for either. It buys two narrower things:

    - `envFrom.secretRef` bulk-mounts an entire Secret and is invisible to that
      loop, which only walks `env`. TestAgentsGolden does catch the render
      diff -- but as a golden mismatch, which `go test ./internal/testing
      -update` absorbs. Add one line to the operator, regenerate, and every key
      of platform-agent-secrets is in the sandbox with the whole Go suite green.
      That is the hole checked below.
    - When someone does widen the allowlist deliberately -- change the
      operator, update the Go list, regenerate the golden with
      `go test ./internal/testing -update` -- three green edits currently leave
      no signal beside the Python that depends on the invariant. This fails
      there, next to `_run_env`, and says what to do about it.

    Widening is allowed. It is a decision, and this makes someone take it
    where the call sites are.
    """

    # Both are pod-scoped: one authenticates callers of the Session KV server
    # on this pod's loopback, the other is the HMAC salt for pseudonymising
    # chat identities, which has to be here because the hashing is here.
    # Neither grants access to any external system.
    EXPECTED = {"SESSION_KV_API_KEY", "SESSION_KV_SALT"}
    SANDBOX_CONTAINER = "platform-agent"
    PROXY_CONTAINER = "envoy-credential-proxy"
    # A credential the proxy holds and the sandbox must never see. Named
    # rather than counted: SESSION_KV_API_KEY is on both containers, so
    # "the proxy has some Secret-backed env" is satisfied by a pod-scoped
    # value and would stay true after the last real credential left.
    PROXY_CREDENTIAL = "API_SERVER_EXTERNAL_KEY"
    GOLDEN = (
        Path(__file__).resolve().parents[3]
        / "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml"
    )

    def _container(self, name):
        if not self.GOLDEN.exists():
            self.fail(
                f"golden manifest not found at {self.GOLDEN}. This test reads the "
                "operator's testdata from Python; if that tree moved, update GOLDEN "
                "— do not delete this test.")
        with self.GOLDEN.open() as handle:
            docs = [d for d in yaml.safe_load_all(handle) if d]
        deployments = [d for d in docs if d.get("kind") == "Deployment"]
        self.assertEqual(
            len(deployments), 1,
            f"expected one Deployment in {self.GOLDEN.name}, found {len(deployments)}")
        pod = deployments[0]["spec"]["template"]["spec"]
        # Both lists: the credential proxy is a native sidecar, so it lives in
        # initContainers with restartPolicy: Always rather than in containers.
        containers = pod.get("containers", []) + pod.get("initContainers", [])
        for container in containers:
            if container.get("name") == name:
                return container
        self.fail(
            f"no container named {name!r} in the golden; found "
            f"{[c.get('name') for c in containers]}. If it was renamed, update the "
            "constant on this class — do not delete this test.")

    @staticmethod
    def _secret_backed(container):
        return {
            env["name"]
            for env in container.get("env", [])
            if (env.get("valueFrom") or {}).get("secretKeyRef")
        }

    def test_the_sandbox_holds_only_the_two_pod_scoped_secrets(self):
        self.assertEqual(
            self._secret_backed(self._container(self.SANDBOX_CONTAINER)),
            self.EXPECTED,
            "The sandbox container's Secret-backed environment changed. "
            "_run_env in agent_common_server.py passes the whole environment to "
            "every gcloud/kubectl/hermes child it spawns, and its docstring "
            "cites this exact set as the reason that is safe. If the new "
            "variable is genuinely pod-scoped, add it to EXPECTED here and to "
            "the allowlist in platformagent_manifests_test.go. If it is a real "
            "credential, it belongs in the credential-proxy container, or "
            "_run_env's call sites need an explicit allowlist.")

    def test_the_sandbox_bulk_mounts_no_secret(self):
        # The half the Go allowlist loop cannot see: it walks `env` only, so an
        # `envFrom.secretRef` puts every key of a Secret into this container
        # with TestBuildDeployment still green.
        bulk = [
            source for source in self._container(self.SANDBOX_CONTAINER).get("envFrom", [])
            if source.get("secretRef")
        ]
        self.assertEqual(
            bulk, [],
            "The sandbox container bulk-mounts a Secret through envFrom, which "
            "puts every key in it into the environment _run_env hands to each "
            "child. Name the variables individually under `env` so the allowlist "
            "above and the Go one both see them.")

    def test_the_credential_proxy_is_where_real_credentials_live(self):
        # Keeps the test above from passing for the wrong reason. If a refactor
        # moved credentials out of the proxy, the next question is whether they
        # landed in the sandbox, and the sandbox assertion alone reads the same
        # either way.
        secret_backed = self._secret_backed(self._container(self.PROXY_CONTAINER))
        self.assertIn(
            self.PROXY_CREDENTIAL, secret_backed,
            f"{self.PROXY_CREDENTIAL} is no longer Secret-backed on the "
            f"{self.PROXY_CONTAINER} container (found {sorted(secret_backed)}). "
            "If credentials moved, they must not have moved into the sandbox; "
            "update PROXY_CREDENTIAL to whichever one now anchors this.")


if __name__ == "__main__":
    unittest.main()
