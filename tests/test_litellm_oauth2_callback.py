"""The gateway's OAuth2 client-credentials hook, and the chart's shipping of it.

`charts/kube-agents/files/litellm_oauth2_callback.py` exchanges a client id
and secret at a token URL for a bearer token and sets it as the `api_key` of
every request bound for a hosted_vllm deployment, the handler the chart uses
for `openai_compatible`. These tests drive it against a token endpoint served
from this process: the first request fetches, the second reuses, a token near
its expiry is renewed, a refused exchange fails the request once and the next
request tries again, and without `OPENAI_COMPATIBLE_OAUTH_TOKEN_URL` the hook
touches nothing. The render cases need `helm` and skip without it.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import importlib.util
import json
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse
from unittest import mock

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "litellm_oauth2_callback.py"
_CALLBACK = "litellm_oauth2_callback.proxy_handler_instance"
_KEY = "litellm_oauth2_callback.py"
_MOUNT = "/app/litellm_oauth2_callback.py"
_HELM_BASE_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
    "-s",
    "templates/litellm.yaml",
]
_OPENAI_COMPATIBLE_ARGS = [
    "--set",
    "litellm.modelProvider=openai_compatible",
    "--set",
    "litellm.modelDefaultName=some-org/some-model",
    "--set",
    "litellm.openaiCompatible.apiBase=https://ai-gateway.example.com/v1",
]


def _load_hook():
    """Import the hook with a stand-in for LiteLLM's CustomLogger base class."""
    if "litellm" not in sys.modules and importlib.util.find_spec("litellm") is None:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")
        custom_logger.CustomLogger = type("CustomLogger", (), {})
        sys.modules.update(
            {
                "litellm": litellm,
                "litellm.integrations": integrations,
                "litellm.integrations.custom_logger": custom_logger,
            }
        )
    spec = importlib.util.spec_from_file_location("litellm_oauth2_callback", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _throwaway_ca(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path] | None:
    """A self-signed certificate for 127.0.0.1 and its key, or None without openssl."""
    if not shutil.which("openssl"):
        return None
    cert, key = directory / "ca.pem", directory / "key.pem"
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key),
         "-out", str(cert), "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        capture_output=True, text=True, check=False,
    )
    return (cert, key) if proc.returncode == 0 else None


class _TokenEndpoint:
    """A client_credentials token endpoint on localhost that records what it was asked."""

    def __init__(self, client_id="svc", client_secret="s3cr3t", expires_in=3600, status=200, tls=None):
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/oauth/token")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                auth = self.headers.get("Authorization", "")
                outer.requests.append({"path": self.path, "form": form, "authorization": auth,
                                       "content_type": self.headers.get("Content-Type", "")})
                basic = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                posted = form.get("client_id") == [client_id] and form.get("client_secret") == [client_secret]
                if status != 200 or not (auth == basic or posted):
                    self.send_response(status if status != 200 else 401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"invalid_client"}')
                    return
                body = {"access_token": f"tok-{len(outer.requests)}", "token_type": "Bearer"}
                if expires_in is not None:
                    body["expires_in"] = expires_in
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        scheme = "http"
        if tls is not None:
            cert, key = tls
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            scheme = "https"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"{scheme}://127.0.0.1:{self.server.server_address[1]}/oauth/token"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Router:
    def __init__(self, models: dict[str, list[str]], ssl_verify: dict[str, str] | None = None):
        self.models = models
        self.ssl_verify = ssl_verify or {}

    def get_model_list(self, model_name: str):
        params = [{"litellm_params": {"model": m}} for m in self.models.get(model_name, [])]
        for p in params:
            if model_name in self.ssl_verify:
                p["litellm_params"]["ssl_verify"] = self.ssl_verify[model_name]
        return params


_CA_PATH = "/etc/litellm/openai-compatible-ca/ca.crt"
_ROUTER = _Router({
    "model-default": ["hosted_vllm/some-org/some-model"],
    "hermes-agent": ["hosted_vllm/some-org/some-model"],
    "private-ca": ["hosted_vllm/some-org/some-model"],
    "gemini": ["gemini/gemini-3.5-flash"],
    "mixed": ["hosted_vllm/x", "openai/gpt-5.4"],
}, ssl_verify={"private-ca": _CA_PATH})


class TestHook(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()
        self.endpoint = _TokenEndpoint()
        self.addCleanup(self.endpoint.close)
        self.env = {
            self.hook.TOKEN_URL_ENV: self.endpoint.url,
            self.hook.CLIENT_ID_ENV: "svc",
            self.hook.CLIENT_CREDENTIAL_ENV: "s3cr3t",
        }

    def _run(self, data: dict, env: dict | None = None):
        instance = self.hook.proxy_handler_instance
        with mock.patch.dict(self.hook.os.environ, self.env if env is None else env, clear=False), \
                mock.patch.object(self.hook, "_router", return_value=_ROUTER):
            return asyncio.run(instance.async_pre_call_hook(None, None, data, "completion"))

    def test_without_a_token_url_the_request_is_untouched(self):
        data = {"model": "model-default", "messages": []}
        with mock.patch.dict(self.hook.os.environ, {}, clear=True):
            out = self._run(data, env={})
        self.assertEqual(out, {"model": "model-default", "messages": []})
        self.assertEqual(self.endpoint.requests, [])

    def test_the_first_request_fetches_with_basic_auth_and_the_second_reuses(self):
        first = self._run({"model": "model-default", "messages": []})
        second = self._run({"model": "hermes-agent", "messages": []})
        self.assertEqual(first["api_key"], "tok-1")
        self.assertEqual(second["api_key"], "tok-1")
        self.assertEqual(len(self.endpoint.requests), 1)
        request = self.endpoint.requests[0]
        self.assertEqual(request["form"], {"grant_type": ["client_credentials"]})
        self.assertTrue(request["authorization"].startswith("Basic "))
        self.assertIn("application/x-www-form-urlencoded", request["content_type"])

    def test_post_auth_puts_the_credentials_in_the_form_with_the_scope(self):
        env = dict(self.env, **{self.hook.CLIENT_AUTH_ENV: "post", self.hook.SCOPE_ENV: "models.read"})
        out = self._run({"model": "model-default"}, env=env)
        self.assertEqual(out["api_key"], "tok-1")
        request = self.endpoint.requests[0]
        self.assertEqual(request["authorization"], "")
        self.assertEqual(request["form"]["client_id"], ["svc"])
        self.assertEqual(request["form"]["scope"], ["models.read"])

    def test_a_token_inside_the_renewal_margin_is_renewed(self):
        self._run({"model": "model-default"})
        cache = self.hook._cache
        cache.expires_at = self.hook.time.monotonic() + self.hook.REFRESH_MARGIN_SECONDS - 1
        out = self._run({"model": "model-default"})
        self.assertEqual(out["api_key"], "tok-2")
        self.assertEqual(len(self.endpoint.requests), 2)

    def test_a_response_without_a_usable_expires_in_takes_the_default_lifetime(self):
        # Absent, and the values json or float() admit that are not a finite
        # positive number: NaN would make the token never usable and refetch on
        # every request, Infinity would pin it forever.
        for expires_in in (None, "nan", "inf", float("inf"), -5, 0, "soon"):
            with self.subTest(expires_in=expires_in):
                hook = _load_hook()
                endpoint = _TokenEndpoint(expires_in=expires_in)
                self.addCleanup(endpoint.close)
                env = dict(self.env, **{hook.TOKEN_URL_ENV: endpoint.url})
                before = hook.time.monotonic()
                with mock.patch.dict(hook.os.environ, env), mock.patch.object(hook, "_router", return_value=_ROUTER):
                    out = asyncio.run(hook.proxy_handler_instance.async_pre_call_hook(None, None, {"model": "model-default"}, "completion"))
                    again = asyncio.run(hook.proxy_handler_instance.async_pre_call_hook(None, None, {"model": "model-default"}, "completion"))
                self.assertEqual(out["api_key"], "tok-1")
                self.assertEqual(again["api_key"], "tok-1", "the token must be cached, not refetched")
                lifetime = hook._cache.expires_at - before
                self.assertGreater(lifetime, hook.DEFAULT_TOKEN_LIFETIME_SECONDS - 5)
                self.assertLessEqual(lifetime, hook.DEFAULT_TOKEN_LIFETIME_SECONDS + 1)

    def test_a_refused_exchange_fails_that_request_and_the_next_one_tries_again(self):
        bad = dict(self.env, **{self.hook.CLIENT_CREDENTIAL_ENV: "wrong"})
        with self.assertRaises(RuntimeError) as refused:
            self._run({"model": "model-default"}, env=bad)
        self.assertIn("401", str(refused.exception))
        self.assertNotIn("wrong", str(refused.exception))
        out = self._run({"model": "model-default"})
        self.assertEqual(out["api_key"], "tok-2")

    def test_only_an_alias_served_wholly_by_hosted_vllm_gets_the_bearer(self):
        # An alias the router does not serve is left alone without a warning:
        # the proxy refuses it next, and the one-shot "blind" warning must stay
        # available for a router the hook really cannot read.
        for alias in ("gemini", "mixed", "unknown", ""):
            with self.subTest(alias=alias), self.assertNoLogs(self.hook.logger, level="WARNING"):
                out = self._run({"model": alias})
                self.assertNotIn("api_key", out)
        self.assertEqual(self.endpoint.requests, [])
        self.assertFalse(self.hook._blind_warned)

    def test_a_hook_that_cannot_see_the_router_leaves_the_request_and_says_so_once(self):
        instance = self.hook.proxy_handler_instance
        with mock.patch.dict(self.hook.os.environ, self.env), \
                mock.patch.object(self.hook, "_router", return_value=None), \
                self.assertLogs(self.hook.logger, level="WARNING") as logs:
            out = asyncio.run(instance.async_pre_call_hook(None, None, {"model": "model-default"}, "completion"))
            asyncio.run(instance.async_pre_call_hook(None, None, {"model": "model-default"}, "completion"))
        self.assertNotIn("api_key", out)
        self.assertEqual(len([line for line in logs.output if "inactive" in line]), 1)

    def test_a_server_behind_a_private_ca_adds_its_bundle_to_the_token_endpoint_roots(self):
        # The chart sets ssl_verify on the alias's deployments for a private
        # CA; the hook trusts that file beside the system roots, so a public
        # identity provider still verifies. The bundle is loaded into the
        # context rather than given as cafile, which would drop the roots; a
        # missing file is a token-request failure before anything is sent.
        self.assertEqual(self.hook.ca_bundle_of(self.hook.hosted_vllm_deployments("private-ca", _ROUTER)), _CA_PATH)
        self.assertEqual(self.hook.ca_bundle_of(self.hook.hosted_vllm_deployments("model-default", _ROUTER)), "")
        self.assertEqual(self.hook.ca_bundle_of([{"ssl_verify": True}, {"ssl_verify": False}]), "")
        real = self.hook.ssl.create_default_context
        contexts = []

        def building(*args, **kwargs):
            context = real(*args, **kwargs)
            contexts.append((kwargs, context))
            return context

        with mock.patch.object(self.hook.ssl, "create_default_context", side_effect=building):
            with self.assertRaises(RuntimeError) as failed:
                self._run({"model": "private-ca"})
        self.assertIn("token request failed", str(failed.exception))
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0][0], {}, "the bundle must not be passed as cafile, which replaces the system roots")
        self.assertEqual(self.endpoint.requests, [], "a context that could not be built must not reach the endpoint")

    def test_a_private_ca_token_endpoint_verifies_with_the_bundle_and_the_system_roots_stay(self):
        # The positive half of the CA change, over real TLS: the token endpoint
        # on a throwaway CA verifies when the alias's ssl_verify names that CA,
        # fails without it, and the context that verified it still carries the
        # system roots (one more certificate than a default context, whatever
        # that count is on this machine), so a public identity provider behind
        # a private-CA gateway keeps working.
        with tempfile.TemporaryDirectory() as tmp:
            ca = _throwaway_ca(pathlib.Path(tmp))
            if ca is None:
                self.skipTest("openssl is not available to mint a throwaway CA")
            endpoint = _TokenEndpoint(tls=ca)
            self.addCleanup(endpoint.close)
            env = dict(self.env, **{self.hook.TOKEN_URL_ENV: endpoint.url})
            router = _Router({"model-default": ["hosted_vllm/m"], "private-ca": ["hosted_vllm/m"]},
                             ssl_verify={"private-ca": str(ca[0])})
            real = self.hook.ssl.create_default_context
            contexts = []

            def building(*args, **kwargs):
                context = real(*args, **kwargs)
                contexts.append(context)
                return context

            instance = self.hook.proxy_handler_instance
            with mock.patch.dict(self.hook.os.environ, env), mock.patch.object(self.hook, "_router", return_value=router), \
                    mock.patch.object(self.hook.ssl, "create_default_context", side_effect=building):
                with self.assertRaises(RuntimeError) as unverified:
                    asyncio.run(instance.async_pre_call_hook(None, None, {"model": "model-default"}, "completion"))
                verified = asyncio.run(instance.async_pre_call_hook(None, None, {"model": "private-ca"}, "completion"))
            self.assertIn("CERTIFICATE_VERIFY_FAILED", str(unverified.exception))
            self.assertEqual(verified["api_key"], "tok-1")
            self.assertEqual(len(contexts), 1)
            # cert_store_stats counts every loaded certificate; get_ca_certs
            # would skip a throwaway certificate minted without the CA flag.
            self.assertEqual(contexts[0].cert_store_stats()["x509"], real().cert_store_stats()["x509"] + 1)

    def test_a_redirecting_token_url_is_refused_with_the_redirect_status(self):
        # urllib would follow a 302 with a GET, dropping the form and carrying
        # the Basic header to the new location; the hook refuses instead and
        # the credentials never reach the redirect target.
        env = dict(self.env, **{self.hook.TOKEN_URL_ENV: self.endpoint.url.replace("/oauth/token", "/redirect")})
        with self.assertRaises(RuntimeError) as refused:
            self._run({"model": "model-default"}, env=env)
        self.assertIn("302", str(refused.exception))
        self.assertEqual(self.endpoint.requests, [], "the credentials must not reach the redirect target")

    def test_a_401_from_the_server_drops_the_cached_token(self):
        # A token the server stops honouring early (revoked, signing key
        # rotated) must not be sent until its stated expiry: the failure hook
        # forgets it, and the next request fetches a new one.
        instance = self.hook.proxy_handler_instance
        self.assertEqual(self._run({"model": "model-default"})["api_key"], "tok-1")

        class _Unauthorized(Exception):
            status_code = 401

        class _Throttled(Exception):
            status_code = 429

        with mock.patch.object(self.hook, "_router", return_value=_ROUTER):
            asyncio.run(instance.async_post_call_failure_hook({"model": "model-default", "api_key": "tok-1"}, _Throttled(), None))
            self.assertTrue(self.hook._cache.usable(), "a 429 is not a verdict on the token")
            asyncio.run(instance.async_post_call_failure_hook({"model": "gemini", "api_key": "tok-1"}, _Unauthorized(), None))
            self.assertTrue(self.hook._cache.usable(), "another provider's 401 is not about this token")
            asyncio.run(instance.async_post_call_failure_hook({"model": "model-default", "api_key": "tok-0"}, _Unauthorized(), None))
            self.assertTrue(self.hook._cache.usable(), "a late 401 for a token already replaced must not drop its successor")
            with self.assertLogs(self.hook.logger, level="WARNING"):
                asyncio.run(instance.async_post_call_failure_hook({"model": "model-default", "api_key": "tok-1"}, _Unauthorized(), None))
        self.assertFalse(self.hook._cache.usable())
        self.assertEqual(self._run({"model": "model-default"})["api_key"], "tok-2")

    def test_a_token_url_without_client_credentials_is_a_configuration_error(self):
        with self.assertRaises(ValueError) as err:
            self._run({"model": "model-default"}, env={self.hook.TOKEN_URL_ENV: self.endpoint.url})
        self.assertIn(self.hook.CLIENT_ID_ENV, str(err.exception))

    def test_the_credential_never_reaches_the_log(self):
        with self.assertLogs(self.hook.logger, level="INFO") as logs:
            self._run({"model": "model-default"})
        self.assertFalse(any("s3cr3t" in line for line in logs.output), logs.output)


@unittest.skipUnless(importlib.util.find_spec("litellm"), "litellm is not installed")
class TestWithLiteLLM(unittest.TestCase):
    def test_the_proxy_loads_it_by_its_callbacks_string_and_it_sets_the_key_on_the_request(self):
        # The hand-through itself (route_request -> Router.acompletion with
        # the request's api_key winning over the deployment's) is LiteLLM's
        # code, read at the pinned tag rather than driven here.
        import litellm.proxy.proxy_server as proxy_server
        from litellm import Router
        from litellm.proxy.types_utils.utils import get_instance_fn

        endpoint = _TokenEndpoint()
        self.addCleanup(endpoint.close)
        instance = get_instance_fn(_CALLBACK, config_file_path=str(_CHART_FILE.parent / "config.yaml"))
        hook = sys.modules["litellm_oauth2_callback"]
        router = Router(model_list=[{"model_name": "model-default",
                                     "litellm_params": {"model": "hosted_vllm/some-org/some-model",
                                                        "api_base": "https://ai-gateway.example.com/v1"}}])
        previous = proxy_server.llm_router
        proxy_server.llm_router = router
        env = {hook.TOKEN_URL_ENV: endpoint.url, hook.CLIENT_ID_ENV: "svc", hook.CLIENT_CREDENTIAL_ENV: "s3cr3t"}
        try:
            with mock.patch.dict(hook.os.environ, env):
                data = asyncio.run(instance.async_pre_call_hook(None, None, {"model": "model-default", "messages": []}, "completion"))
        finally:
            proxy_server.llm_router = previous
        self.assertEqual(data["api_key"], "tok-1")


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    def _documents(self, extra_args: list[str]) -> list[dict]:
        proc = subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    def _configmap_and_container(self, extra_args):
        docs = self._documents(extra_args)
        configmap = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        return configmap, container

    def test_openai_compatible_with_an_env_secret_ships_registers_and_mounts_it(self):
        configmap, container = self._configmap_and_container(_OPENAI_COMPATIBLE_ARGS + ["--set", "litellm.envFromSecret=ai-gateway-oauth"])
        self.assertEqual(configmap["data"][_KEY], _CHART_FILE.read_text())
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        self.assertIn(_CALLBACK, config["litellm_settings"]["callbacks"])
        self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, container["volumeMounts"])

    def test_without_the_env_secret_or_for_another_provider_nothing_ships(self):
        for args in (_OPENAI_COMPATIBLE_ARGS, ["--set", "litellm.modelProvider=gemini", "--set", "litellm.envFromSecret=x"]):
            with self.subTest(args=args):
                configmap, container = self._configmap_and_container(args)
                self.assertNotIn(_KEY, configmap["data"])
                self.assertNotIn(_CALLBACK, yaml.safe_load(configmap["data"]["config.yaml"])["litellm_settings"]["callbacks"])
                self.assertFalse(any(m.get("subPath") == _KEY for m in container["volumeMounts"]))


if __name__ == "__main__":
    unittest.main()
