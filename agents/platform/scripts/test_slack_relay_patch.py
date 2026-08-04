import asyncio
import json
import os
import sys
import types
import unittest
import urllib.error
from email.message import Message
from unittest import mock


def _register_fake_modules() -> None:
    class FakeAsyncWebClient:
        def __init__(self, token=None, **kwargs):
            self.token = token
            self.base_url = kwargs.get("base_url", "https://slack.com/api/")

        async def api_call(self, *_args, **_kwargs):
            raise AssertionError("the real Slack client must never be used")

    class FakeAsyncApp:
        def __init__(self, client=None, **kwargs):
            self.client = client
            self.kwargs = kwargs

    class FakeSlackResponse:
        def __init__(
            self,
            *,
            client=None,
            http_verb=None,
            api_url=None,
            req_args=None,
            data=None,
            headers=None,
            status_code=None,
            **_kwargs,
        ):
            self.client = client
            self.http_verb = http_verb
            self.api_url = api_url
            self.req_args = req_args
            self.data = data
            self.headers = headers
            self.status_code = status_code

    class FakeSocketModeRequest:
        def __init__(self, type, envelope_id, payload):
            self.type = type
            self.envelope_id = envelope_id
            self.payload = payload

    async def fake_run_async_bolt_app(app, request):
        return None

    def fake_cache_audio_from_bytes(content, ext):
        return f"cached_audio.{ext}"

    def fake_cache_image_from_bytes(content, ext):
        return f"cached_image.{ext}"

    class PlatformRegistry:
        def create_adapter(self, name, config):
            return None

    bolt_async_app = types.ModuleType("slack_bolt.app.async_app")
    bolt_async_app.AsyncWebClient = FakeAsyncWebClient
    bolt_async_context = types.ModuleType("slack_bolt.context.async_context")
    bolt_async_context.AsyncWebClient = FakeAsyncWebClient
    bolt_internals = types.ModuleType(
        "slack_bolt.adapter.socket_mode.async_internals"
    )
    bolt_internals.run_async_bolt_app = fake_run_async_bolt_app

    bolt_socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    bolt_socket_mode.async_internals = bolt_internals
    bolt_adapter = types.ModuleType("slack_bolt.adapter")
    bolt_adapter.socket_mode = bolt_socket_mode

    bolt_package = types.ModuleType("slack_bolt")
    bolt_app_package = types.ModuleType("slack_bolt.app")
    bolt_context_package = types.ModuleType("slack_bolt.context")
    bolt_package.app = bolt_app_package
    bolt_package.context = bolt_context_package
    bolt_package.adapter = bolt_adapter
    bolt_app_package.async_app = bolt_async_app
    bolt_context_package.async_context = bolt_async_context

    slack_sdk_module = types.ModuleType("slack_sdk")
    slack_web_module = types.ModuleType("slack_sdk.web")
    slack_response_module = types.ModuleType("slack_sdk.web.slack_response")
    slack_response_module.SlackResponse = FakeSlackResponse
    slack_async_response_module = types.ModuleType("slack_sdk.web.async_slack_response")
    slack_async_response_module.AsyncSlackResponse = FakeSlackResponse
    slack_socket_mode_module = types.ModuleType("slack_sdk.socket_mode")
    slack_socket_mode_request_module = types.ModuleType(
        "slack_sdk.socket_mode.request"
    )
    slack_socket_mode_request_module.SocketModeRequest = FakeSocketModeRequest
    slack_sdk_module.web = slack_web_module
    slack_sdk_module.socket_mode = slack_socket_mode_module
    slack_web_module.slack_response = slack_response_module
    slack_socket_mode_module.request = slack_socket_mode_request_module

    registry_module = types.ModuleType("gateway.platform_registry")
    registry_module.PlatformRegistry = PlatformRegistry
    platforms_base_module = types.ModuleType("gateway.platforms.base")
    platforms_base_module.cache_audio_from_bytes = fake_cache_audio_from_bytes
    platforms_base_module.cache_image_from_bytes = fake_cache_image_from_bytes
    platforms_module = types.ModuleType("gateway.platforms")
    platforms_module.base = platforms_base_module
    gateway_module = types.ModuleType("gateway")
    gateway_module.platform_registry = registry_module
    gateway_module.platforms = platforms_module

    for name, mod in [
        ("slack_bolt", bolt_package),
        ("slack_bolt.app", bolt_app_package),
        ("slack_bolt.app.async_app", bolt_async_app),
        ("slack_bolt.context", bolt_context_package),
        ("slack_bolt.context.async_context", bolt_async_context),
        ("slack_bolt.adapter", bolt_adapter),
        ("slack_bolt.adapter.socket_mode", bolt_socket_mode),
        ("slack_bolt.adapter.socket_mode.async_internals", bolt_internals),
        ("slack_sdk", slack_sdk_module),
        ("slack_sdk.web", slack_web_module),
        ("slack_sdk.web.slack_response", slack_response_module),
        ("slack_sdk.web.async_slack_response", slack_async_response_module),
        ("slack_sdk.socket_mode", slack_socket_mode_module),
        ("slack_sdk.socket_mode.request", slack_socket_mode_request_module),
        ("gateway", gateway_module),
        ("gateway.platform_registry", registry_module),
        ("gateway.platforms", platforms_module),
        ("gateway.platforms.base", platforms_base_module),
    ]:
        sys.modules.setdefault(name, mod)


_register_fake_modules()

import slack_relay_patch


RELAY_URL = "http://127.0.0.1:8765"
ADAPTER_MODULE_NAME = "fake_slack_adapter_module"


class FakeHTTPResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self, *_args):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False





class SlackRelayPatchTest(unittest.TestCase):
    """Exercise install() against a fake Hermes adapter and fake slack_bolt.

    slack_bolt >= 1.15 constructs a new plain AsyncWebClient per dispatched
    request (AsyncApp._init_context), so the patch must rebind the client
    symbol inside slack_bolt's own modules — not only the adapter module —
    or bolt's authorization middleware calls slack.com directly with the
    "relay:<teamId>" placeholder token and rejects every inbound event.
    """

    def setUp(self):
        self._saved_environ = {
            name: os.environ.get(name)
            for name in (
                "SLACK_RELAY_URL",
                "SLACK_APP_TOKEN",
                "SLACK_RELAY_BOOTSTRAP_WAIT_SECONDS",
            )
        }
        os.environ["SLACK_RELAY_URL"] = RELAY_URL

        class FakeAsyncWebClient:
            def __init__(self, token=None, **kwargs):
                self.token = token
                self.base_url = kwargs.get("base_url", "https://slack.com/api/")

            async def api_call(self, *_args, **_kwargs):
                raise AssertionError("the real Slack client must never be used")

        class FakeAsyncApp:
            def __init__(self, client=None, **kwargs):
                self.client = client
                self.kwargs = kwargs

        adapter_module = types.ModuleType(ADAPTER_MODULE_NAME)
        adapter_module.AsyncApp = FakeAsyncApp
        adapter_module.AsyncWebClient = FakeAsyncWebClient

        class FakeAdapter:
            def __init__(self, config):
                self.config = config
                self.connect_results = [True]

            async def connect(self, *, is_reconnect=False):
                return self.connect_results.pop(0)

            async def disconnect(self):
                return None

        FakeAdapter.__module__ = ADAPTER_MODULE_NAME
        adapter_module.FakeAdapter = FakeAdapter
        sys.modules[ADAPTER_MODULE_NAME] = adapter_module
        self.adapter_module = adapter_module
        self.fake_real_client = FakeAsyncWebClient
        self.fake_real_app = FakeAsyncApp

        self.bolt_async_app = sys.modules["slack_bolt.app.async_app"]
        self._saved_bolt_app_client = self.bolt_async_app.AsyncWebClient
        self.bolt_async_app.AsyncWebClient = FakeAsyncWebClient
        self.bolt_async_context = sys.modules["slack_bolt.context.async_context"]
        self._saved_bolt_context_client = self.bolt_async_context.AsyncWebClient
        self.bolt_async_context.AsyncWebClient = FakeAsyncWebClient

        self.registry_class = sys.modules["gateway.platform_registry"].PlatformRegistry
        self._saved_registry_create = self.registry_class.create_adapter
        if hasattr(self.registry_class, "_slack_credential_proxy_relay_patched"):
            delattr(self.registry_class, "_slack_credential_proxy_relay_patched")

        def _create_adapter(self_reg, name, config):
            if name == "slack":
                return FakeAdapter(config)
            return None

        self.registry_class.create_adapter = _create_adapter

        slack_relay_patch.install()

    def tearDown(self):
        for name, value in self._saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.bolt_async_app.AsyncWebClient = self._saved_bolt_app_client
        self.bolt_async_context.AsyncWebClient = self._saved_bolt_context_client
        self.registry_class.create_adapter = self._saved_registry_create
        sys.modules.pop(ADAPTER_MODULE_NAME, None)

    def _create_adapter(self, token=""):
        config = types.SimpleNamespace(token=token)
        return self.registry_class().create_adapter("slack", config)

    def test_bolt_per_request_client_symbol_is_relay_backed(self):
        adapter = self._create_adapter()
        self.assertIsNotNone(adapter)
        patched = self.bolt_async_app.AsyncWebClient
        self.assertIsNot(patched, self.fake_real_client)
        self.assertTrue(issubclass(patched, self.fake_real_client))
        self.assertIs(self.bolt_async_context.AsyncWebClient, patched)

        # bolt's AsyncApp._init_context constructor call shape must be accepted
        # and the resulting client must send API calls to the relay.
        client = patched(
            token="relay:T123",
            base_url="https://slack.com/api/",
            timeout=30,
            ssl=None,
            proxy=None,
            session=None,
            trust_env_in_session=False,
            headers=None,
            team_id="T123",
            logger=None,
            retry_handlers=None,
        )
        self.assertEqual(client.team_id, "T123")

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeHTTPResponse({"response": {"ok": True, "team_id": "T123"}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = asyncio.run(client.api_call("auth.test"))

        self.assertEqual(dict(result.data), {"ok": True, "team_id": "T123"})
        self.assertEqual(getattr(result, "headers", None), {})
        self.assertEqual(captured["url"], RELAY_URL + "/v1/chat/slack/api")
        self.assertEqual(captured["payload"]["teamId"], "T123")
        self.assertEqual(captured["payload"]["method"], "auth.test")

        def fake_urlopen_with_headers(req, timeout=None):
            return FakeHTTPResponse({
                "response": {
                    "ok": True,
                    "team_id": "T123",
                    "__headers": {"x-oauth-scopes": "chat:write"}
                }
            })

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen_with_headers):
            result = asyncio.run(client.api_call("auth.test"))

        self.assertEqual(dict(result.data), {"ok": True, "team_id": "T123"})
        self.assertEqual(getattr(result, "headers", None), {"x-oauth-scopes": "chat:write"})

    def test_the_team_bolt_resolved_wins_over_the_joined_token(self):
        """Across workspaces the token lists every team, so it cannot be split.

        connect() joins one "relay:<teamId>" per authenticated workspace into
        config.token. Deriving team_id from that yields "T1,relay:T2" for the
        first team and relays every call to the wrong workspace. Bolt resolves
        the team from the inbound event and passes it per request; prefer it.
        """
        self._create_adapter()
        patched = self.bolt_async_app.AsyncWebClient

        client = patched(token="relay:T1,relay:T2", team_id="T2")
        self.assertEqual("T2", client.team_id)

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeHTTPResponse({"response": {"ok": True}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            asyncio.run(client.api_call("auth.test"))

        self.assertEqual("T2", captured["payload"]["teamId"])

    def test_a_single_workspace_token_still_yields_its_team(self):
        """With no team_id from bolt, the placeholder token remains the source."""
        self._create_adapter()
        patched = self.bolt_async_app.AsyncWebClient

        self.assertEqual("T1", patched(token="relay:T1").team_id)

    def test_connect_waits_for_relay_readiness(self):
        adapter = self._create_adapter()
        attempts = {"count": 0}

        def fake_urlopen(req, timeout=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            if attempts["count"] == 2:
                raise urllib.error.HTTPError(
                    req.full_url, 503, "Service Unavailable", Message(), None
                )
            return FakeHTTPResponse({"workspaces": [{"teamId": "T123"}]})

        async def fast_sleep(_seconds):
            return None

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch(
            "asyncio.sleep", side_effect=fast_sleep
        ):
            connected = asyncio.run(adapter.connect())

        self.assertTrue(connected)
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(adapter.config.token, "relay:T123")
        self.assertEqual(os.environ.get("SLACK_APP_TOKEN"), "relay")

    def test_failed_bootstrap_keeps_placeholder_credential(self):
        # Hermes removes credential-less platforms from its reconnect queue;
        # a connect that fails before the relay is up must leave a non-empty
        # placeholder token behind so Slack stays eligible for retries.
        os.environ["SLACK_RELAY_BOOTSTRAP_WAIT_SECONDS"] = "0"
        adapter = self._create_adapter(token="")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            connected = asyncio.run(adapter.connect())

        self.assertFalse(connected)
        self.assertEqual(adapter.config.token, "relay:")


if __name__ == "__main__":
    unittest.main()
