import asyncio
import dataclasses
import io
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

    class FakeSlackApiError(Exception):
        """Mirrors slack_sdk.errors.SlackApiError: a message plus .response."""

        def __init__(self, message, response=None):
            super().__init__(message)
            self.response = response

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

        def validate(self):
            """Mirrors SlackResponse.validate: raise unless 200 and ok.

            Kept faithful because a response the relay hands back gets
            re-validated by callers that hold on to it, and the pair it keys
            on is exactly what the relay has to reconstruct.
            """
            if self.status_code == 200 and (self.data or {}).get("ok"):
                return self
            raise FakeSlackApiError(
                "The request to the Slack API failed. "
                f"(url: {self.api_url}, status: {self.status_code})",
                self,
            )

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

        # Upstream made ``scope`` keyword-only in Hermes v2026.8.13. This
        # stand-in is what install() captures as the original wherever a test
        # does not install its own, so it has to accept what the real registry
        # accepts -- a fake stuck on the old signature agrees with a wrapper
        # stuck on the old signature, and #718 shipped a gateway with no chat
        # adapters through exactly that blind spot. The forwarding itself is
        # exercised through _register in SlackStandaloneRelaySendTest.setUp.
        def register(self, entry, *, scope=None):
            self.registered = entry

    @dataclasses.dataclass
    class PlatformEntry:
        """The mutable fields the patch reaches for on a registry entry.

        Deliberately not frozen: the real ``PlatformEntry`` is a plain
        dataclass whose ``standalone_sender_fn`` and ``is_connected`` are
        assignable, and the patch depends on that.
        """

        name: str
        standalone_sender_fn: object = None
        is_connected: object = None
        max_message_length: int = 40000

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
    slack_errors_module = types.ModuleType("slack_sdk.errors")
    slack_errors_module.SlackApiError = FakeSlackApiError
    slack_sdk_module.web = slack_web_module
    slack_sdk_module.socket_mode = slack_socket_mode_module
    slack_sdk_module.errors = slack_errors_module
    slack_web_module.slack_response = slack_response_module
    slack_socket_mode_module.request = slack_socket_mode_request_module

    registry_module = types.ModuleType("gateway.platform_registry")
    registry_module.PlatformRegistry = PlatformRegistry
    registry_module.PlatformEntry = PlatformEntry
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
        ("slack_sdk.errors", slack_errors_module),
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
# install() imports this from slack_sdk.errors, so tests have to assert against
# whichever class won registration above — the fake, or the real one if the SDK
# is installed in this environment.
SlackApiError = sys.modules["slack_sdk.errors"].SlackApiError


def relay_error(body: object) -> urllib.error.HTTPError:
    """Build the 502 the credential proxy answers when a relayed call fails."""
    return urllib.error.HTTPError(
        RELAY_URL + "/v1/chat/slack/api",
        502,
        "Bad Gateway",
        Message(),
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


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
                "SLACK_BOT_TOKEN",
                "SLACK_RELAY_BOOTSTRAP_WAIT_SECONDS",
            )
        }
        os.environ["SLACK_RELAY_URL"] = RELAY_URL
        os.environ.pop("SLACK_BOT_TOKEN", None)

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
        self._saved_registry_register = self.registry_class.register
        # Both sentinels have to go. Leave either one set and install() skips
        # the wrapper it guards, and every test after the first exercises the
        # previous test's patch — passing for the wrong reason.
        for sentinel in (
            "_slack_credential_proxy_relay_patched",
            "_slack_standalone_relay_patched",
        ):
            if hasattr(self.registry_class, sentinel):
                delattr(self.registry_class, sentinel)

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
        self.registry_class.register = self._saved_registry_register
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

    def test_a_slack_rejection_surfaces_as_the_sdk_exception(self):
        """An ``ok: false`` has to reach callers as a raised SlackApiError.

        Nothing about the relay is visible to the code that calls the client:
        Bolt listeners and Hermes' own adapter are written against the real
        AsyncWebClient, which raises SlackApiError and carries the cause in
        ``response["error"]``. The proxy-side client already validated the
        response, so the rejection arrives here as a 502 rather than as a 200
        payload — translate it back or every rejection is indistinguishable
        from the relay falling over.
        """
        self._create_adapter()
        client = self.bolt_async_app.AsyncWebClient(token="relay:T123")

        def fake_urlopen(req, timeout=None):
            raise relay_error(
                {
                    "error": "Slack operation failed",
                    "slack": {"ok": False, "error": "channel_not_found"},
                }
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(SlackApiError) as caught:
                asyncio.run(client.api_call("chat.postMessage"))

        response = caught.exception.response
        self.assertEqual(response.data["error"], "channel_not_found")
        self.assertIs(response.data["ok"], False)
        self.assertEqual(response.api_url, "chat.postMessage")
        self.assertEqual(
            "The request to the Slack API failed. "
            "(url: chat.postMessage, status: 200)",
            str(caught.exception),
        )
        # validate() raises on (status_code != 200 or not ok). The status here
        # is the Slack call's, not the relay's 502 — Slack answered 200 with
        # ok:false, and a caller holding this response and re-validating it
        # has to see that same pair.
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(SlackApiError):
            response.validate()

    def test_a_relay_failure_stays_a_relay_failure(self):
        """A broken relay must not be dressed up as a rejected API call.

        The proxy answers 502 for everything that goes wrong behind it, so the
        status alone says nothing. Only a body carrying ``slack`` came from
        Slack; anything else propagates untouched, with its diagnostics still
        readable — relayed_slack_error consumes the one-shot file object to
        look, and has to put the bytes back.
        """
        self._create_adapter()
        client = self.bolt_async_app.AsyncWebClient(token="relay:T123")

        def fake_urlopen(req, timeout=None):
            raise relay_error({"error": "Slack relay unavailable"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                asyncio.run(client.api_call("chat.postMessage"))

        self.assertNotIsInstance(caught.exception, SlackApiError)
        self.assertEqual(
            json.loads(caught.exception.read().decode("utf-8")),
            {"error": "Slack relay unavailable"},
        )

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


class SlackStandaloneRelaySendTest(unittest.TestCase):
    """The tokenless cron path: what gets patched, and what it sends.

    A cron tick runs in a child interpreter with no adapter and no event
    loop, so none of this goes through ``create_adapter``. Delivery reaches
    Slack through the registry entry's ``standalone_sender_fn`` — and Hermes
    only consults that at all if the entry's ``is_connected`` agrees Slack is
    reachable, which tokenless it used to deny.
    """

    def setUp(self):
        self._saved_environ = {
            name: os.environ.get(name)
            for name in ("SLACK_RELAY_URL", "SLACK_BOT_TOKEN")
        }
        os.environ["SLACK_RELAY_URL"] = RELAY_URL
        os.environ.pop("SLACK_BOT_TOKEN", None)
        self.secret_token = ""

        class FakeAsyncWebClient:
            def __init__(self, token=None, **kwargs):
                self.token = token

            async def api_call(self, *_args, **_kwargs):
                raise AssertionError("the real Slack client must never be used")

        class FakeAsyncApp:
            def __init__(self, client=None, **kwargs):
                self.client = client

        class SlackAdapter:
            def format_message(self, text):
                return "<mrkdwn>" + text

        adapter_module = types.ModuleType(ADAPTER_MODULE_NAME)
        adapter_module.AsyncApp = FakeAsyncApp
        adapter_module.AsyncWebClient = FakeAsyncWebClient
        adapter_module.SlackAdapter = SlackAdapter
        adapter_module.get_secret = lambda _name, _default="": self.secret_token
        sys.modules[ADAPTER_MODULE_NAME] = adapter_module

        self.original_calls = []

        async def original_standalone_send(pconfig, chat_id, message, **kwargs):
            self.original_calls.append((chat_id, message, kwargs))
            return {"success": True, "platform": "slack", "sender": "original"}

        # The patch resolves the adapter module from the sender's __module__
        # rather than by name, so this attribution is what wires up
        # format_message and get_secret above.
        original_standalone_send.__module__ = ADAPTER_MODULE_NAME
        self.original_standalone_send = original_standalone_send

        self.bolt_async_app = sys.modules["slack_bolt.app.async_app"]
        self._saved_bolt_app_client = self.bolt_async_app.AsyncWebClient
        self.bolt_async_context = sys.modules["slack_bolt.context.async_context"]
        self._saved_bolt_context_client = self.bolt_async_context.AsyncWebClient

        registry_module = sys.modules["gateway.platform_registry"]
        self.registry_class = registry_module.PlatformRegistry
        self.entry_class = registry_module.PlatformEntry
        self._saved_registry_create = self.registry_class.create_adapter
        self._saved_registry_register = self.registry_class.register
        for sentinel in (
            "_slack_credential_proxy_relay_patched",
            "_slack_standalone_relay_patched",
        ):
            if hasattr(self.registry_class, sentinel):
                delattr(self.registry_class, sentinel)

        self.registered = []
        self.register_scopes = []

        def _register(_self, entry, *, scope=None):
            self.registered.append(entry)
            self.register_scopes.append(scope)

        self.registry_class.register = _register

        slack_relay_patch.install()
        self.registry = self.registry_class()

    def tearDown(self):
        for name, value in self._saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.bolt_async_app.AsyncWebClient = self._saved_bolt_app_client
        self.bolt_async_context.AsyncWebClient = self._saved_bolt_context_client
        self.registry_class.create_adapter = self._saved_registry_create
        self.registry_class.register = self._saved_registry_register
        sys.modules.pop(ADAPTER_MODULE_NAME, None)

    def _register_slack(self, is_connected=None):
        entry = self.entry_class(
            name="slack",
            standalone_sender_fn=self.original_standalone_send,
            is_connected=is_connected,
        )
        self.registry.register(entry)
        return entry

    def _relay(self, responses):
        """Fake the relay transport, capturing every request it receives."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append((req.full_url, json.loads(req.data.decode("utf-8"))))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return FakeHTTPResponse({"response": result})

        return captured, mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)

    def _send(self, entry, message="brief", **kwargs):
        return asyncio.run(
            entry.standalone_sender_fn(
                types.SimpleNamespace(token=None), "D123", message, **kwargs
            )
        )

    def test_registering_slack_swaps_the_sender_and_the_status(self):
        entry = self._register_slack()
        self.assertIsNot(entry.standalone_sender_fn, self.original_standalone_send)
        # Tokenless, the probe has to say Slack is reachable — the relay is.
        self.assertTrue(entry.is_connected(types.SimpleNamespace(enabled=True)))
        # The entry still reaches the real registry, unswallowed.
        self.assertEqual([entry], self.registered)

    def test_a_non_slack_entry_is_left_alone(self):
        entry = self.entry_class(
            name="discord", standalone_sender_fn=self.original_standalone_send
        )
        self.registry.register(entry)
        self.assertIs(entry.standalone_sender_fn, self.original_standalone_send)
        self.assertIsNone(entry.is_connected)

    def test_a_scoped_registration_is_forwarded_untouched(self):
        entry = self.entry_class(
            name="slack", standalone_sender_fn=self.original_standalone_send
        )
        self.registry.register(entry, scope="plugin:example")
        self.assertEqual([entry], self.registered)
        self.assertEqual(["plugin:example"], self.register_scopes)

    def test_a_scoped_registration_of_another_platform_still_lands(self):
        """The failure mode that takes the whole gateway down.

        This wrapper is installed on the class, so a Slack patch that cannot
        accept the registry's current keywords does not merely lose Slack --
        every platform registers through it, and the gateway comes up with
        Google Chat and A2A missing too.
        """
        entry = self.entry_class(
            name="google_chat", standalone_sender_fn=self.original_standalone_send
        )
        self.registry.register(entry, scope="plugin:google_chat")
        self.assertEqual([entry], self.registered)
        self.assertEqual(["plugin:google_chat"], self.register_scopes)

    def test_an_existing_status_check_still_decides_when_a_token_exists(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-local"
        entry = self._register_slack(is_connected=lambda _config: False)
        self.assertFalse(entry.is_connected(types.SimpleNamespace(enabled=True)))

    def test_the_brief_is_relayed_as_chat_postmessage(self):
        entry = self._register_slack()
        captured, patched = self._relay([{"ok": True, "ts": "1786288500.359359"}])
        with patched:
            result = self._send(entry)

        self.assertEqual(
            {
                "success": True,
                "platform": "slack",
                "chat_id": "D123",
                "message_id": "1786288500.359359",
            },
            result,
        )
        self.assertEqual(1, len(captured))
        url, payload = captured[0]
        self.assertEqual(RELAY_URL + "/v1/chat/slack/api", url)
        self.assertEqual("chat.postMessage", payload["method"])
        # Empty team falls through to the proxy's primary client.
        self.assertEqual("", payload["teamId"])
        self.assertEqual(
            {"channel": "D123", "text": "<mrkdwn>brief", "mrkdwn": True},
            payload["arguments"]["json"],
        )

    def test_a_thread_id_rides_only_when_it_is_set(self):
        """Flat by default: an absent thread id must not become a thread_ts.

        Cron briefs are posted to the home channel, and a stale
        ``thread_id`` there buries every brief in one old thread.
        """
        entry = self._register_slack()
        captured, patched = self._relay(
            [{"ok": True, "ts": "1"}, {"ok": True, "ts": "2"}]
        )
        with patched:
            self._send(entry)
            self._send(entry, thread_id="1786287390.994569")

        self.assertNotIn("thread_ts", captured[0][1]["arguments"]["json"])
        self.assertEqual(
            "1786287390.994569", captured[1][1]["arguments"]["json"]["thread_ts"]
        )

    def test_an_empty_brief_short_circuits_without_touching_the_relay(self):
        """A [SILENT] run delivers empty text every minute; that is a success."""
        entry = self._register_slack()
        captured, patched = self._relay([])
        for label, message in (("empty", ""), ("whitespace", "   \n ")):
            with self.subTest(label):
                with patched:
                    result = self._send(entry, message=message)
                self.assertEqual(
                    {"success": True, "platform": "slack", "skipped": "empty_text"},
                    result,
                )
        self.assertEqual([], captured)

    def test_a_user_id_target_opens_a_dm_first(self):
        entry = self._register_slack()
        captured, patched = self._relay(
            [{"ok": True, "channel": {"id": "D999"}}, {"ok": True, "ts": "9"}]
        )
        with patched:
            result = asyncio.run(
                entry.standalone_sender_fn(
                    types.SimpleNamespace(token=None), "U0BKNNDJERG", "brief"
                )
            )

        self.assertEqual("conversations.open", captured[0][1]["method"])
        self.assertEqual(
            {"users": "U0BKNNDJERG"}, captured[0][1]["arguments"]["json"]
        )
        self.assertEqual("D999", captured[1][1]["arguments"]["json"]["channel"])
        self.assertEqual("D999", result["chat_id"])

    def test_a_slack_rejection_becomes_an_error_result(self):
        """The scheduler reads a dict; a raise here would kill the tick."""
        entry = self._register_slack()
        captured, patched = self._relay(
            [
                relay_error(
                    {
                        "error": "Slack operation failed",
                        "slack": {"ok": False, "error": "channel_not_found"},
                    }
                )
            ]
        )
        with patched:
            result = self._send(entry)

        self.assertEqual({"error": "Slack API error: channel_not_found"}, result)

    def test_a_broken_relay_is_reported_as_a_send_failure(self):
        entry = self._register_slack()
        captured, patched = self._relay(
            [relay_error({"error": "Slack relay unavailable"})]
        )
        with patched:
            result = self._send(entry)

        self.assertEqual({"error": "Slack send failed: relay error 502"}, result)

    def test_a_local_token_keeps_the_stock_sender(self):
        """A deployment that holds a token must behave exactly as before."""
        entry = self._register_slack()
        self.secret_token = "xoxb-local"
        captured, patched = self._relay([])
        with patched:
            result = self._send(entry, thread_id="T1")

        self.assertEqual("original", result["sender"])
        self.assertEqual([], captured)
        self.assertEqual(1, len(self.original_calls))
        chat_id, message, kwargs = self.original_calls[0]
        self.assertEqual(("D123", "brief"), (chat_id, message))
        self.assertEqual("T1", kwargs["thread_id"])

    def test_media_reports_the_relay_limitation_rather_than_dropping_it(self):
        entry = self._register_slack()
        captured, patched = self._relay([])
        with patched:
            result = self._send(entry, media_files=["/tmp/report.pdf"])

        self.assertIn("error", result)
        self.assertIn("files_upload_v2", result["error"])
        self.assertEqual([], captured)

    def test_registering_twice_swaps_once(self):
        entry = self._register_slack()
        swapped = entry.standalone_sender_fn
        original_is_connected = entry.is_connected
        self.registry.register(entry)
        self.assertIs(swapped, entry.standalone_sender_fn)
        self.assertIs(original_is_connected, entry.is_connected)


class SlackRegisteredBeforeInstallTest(unittest.TestCase):
    """The ordering the register() wrapper cannot cover.

    Every other test here registers Slack *after* install(), so the wrapper
    fires and the sweep of existing entries is dead code. If the plugin gets
    there first the wrapper never sees the entry, and the only thing that
    patches it is the sweep over the registry's live entries. That sweep reads
    a private ``_entries``, so it is exactly the part most likely to rot
    against a base-image bump — and it rots silently, into "cron briefs stopped
    arriving" with no error anywhere.
    """

    def setUp(self):
        self._saved_environ = {
            name: os.environ.get(name)
            for name in ("SLACK_RELAY_URL", "SLACK_BOT_TOKEN")
        }
        os.environ["SLACK_RELAY_URL"] = RELAY_URL
        os.environ.pop("SLACK_BOT_TOKEN", None)

        adapter_module = types.ModuleType(ADAPTER_MODULE_NAME)
        adapter_module.get_secret = lambda _name, _default="": ""
        sys.modules[ADAPTER_MODULE_NAME] = adapter_module

        async def original_standalone_send(pconfig, chat_id, message, **kwargs):
            return {"success": True, "sender": "original"}

        original_standalone_send.__module__ = ADAPTER_MODULE_NAME
        self.original_standalone_send = original_standalone_send

        self.registry_module = sys.modules["gateway.platform_registry"]
        self.registry_class = self.registry_module.PlatformRegistry
        self.entry_class = self.registry_module.PlatformEntry
        self._saved_create = self.registry_class.create_adapter
        self._saved_register = self.registry_class.register
        self._had_singleton = hasattr(self.registry_module, "platform_registry")
        self._saved_singleton = getattr(
            self.registry_module, "platform_registry", None
        )
        for sentinel in (
            "_slack_credential_proxy_relay_patched",
            "_slack_standalone_relay_patched",
        ):
            if hasattr(self.registry_class, sentinel):
                delattr(self.registry_class, sentinel)

    def tearDown(self):
        for name, value in self._saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.registry_class.create_adapter = self._saved_create
        self.registry_class.register = self._saved_register
        if self._had_singleton:
            self.registry_module.platform_registry = self._saved_singleton
        elif hasattr(self.registry_module, "platform_registry"):
            delattr(self.registry_module, "platform_registry")
        for sentinel in (
            "_slack_credential_proxy_relay_patched",
            "_slack_standalone_relay_patched",
        ):
            if hasattr(self.registry_class, sentinel):
                delattr(self.registry_class, sentinel)
        sys.modules.pop(ADAPTER_MODULE_NAME, None)

    def test_an_entry_registered_before_install_is_still_patched(self):
        entry = self.entry_class(
            name="slack", standalone_sender_fn=self.original_standalone_send
        )
        self.registry_module.platform_registry = types.SimpleNamespace(
            _entries={"slack": entry}
        )

        slack_relay_patch.install()

        self.assertIsNot(entry.standalone_sender_fn, self.original_standalone_send)
        self.assertTrue(entry.is_connected(types.SimpleNamespace(enabled=True)))

    def test_a_pre_registered_non_slack_entry_is_left_alone(self):
        entry = self.entry_class(
            name="discord", standalone_sender_fn=self.original_standalone_send
        )
        self.registry_module.platform_registry = types.SimpleNamespace(
            _entries={"discord": entry}
        )

        slack_relay_patch.install()

        self.assertIs(entry.standalone_sender_fn, self.original_standalone_send)

    def test_entries_that_cannot_be_read_are_warned_about(self):
        # The rot case: the registry is up, but `_entries` is not where this
        # looks. Silence here reads identically to "nothing was pre-registered".
        self.registry_module.platform_registry = types.SimpleNamespace()

        with self.assertLogs(slack_relay_patch.LOGGER, level="WARNING") as logs:
            slack_relay_patch.install()

        self.assertTrue(
            any("not introspectable" in line for line in logs.output), logs.output
        )

    def test_no_registry_yet_is_not_worth_a_warning(self):
        # install() routinely runs before the registry module has a singleton;
        # the wrapper covers that case, so warning would be noise on every boot.
        if hasattr(self.registry_module, "platform_registry"):
            delattr(self.registry_module, "platform_registry")

        with mock.patch.object(slack_relay_patch.LOGGER, "warning") as warned:
            slack_relay_patch.install()

        self.assertEqual([], warned.call_args_list)


class RelayWithoutTokenTest(unittest.TestCase):
    """When is "no bot token" the wrong answer to "is Slack connected?"."""

    def setUp(self):
        self._saved_environ = {
            name: os.environ.get(name)
            for name in ("SLACK_RELAY_URL", "SLACK_BOT_TOKEN")
        }

    def tearDown(self):
        for name, value in self._saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_relay_configured_and_no_token_means_slack_is_still_reachable(self):
        os.environ["SLACK_RELAY_URL"] = RELAY_URL
        os.environ.pop("SLACK_BOT_TOKEN", None)
        self.assertTrue(slack_relay_patch.relay_without_token())

    def test_a_local_token_means_the_stock_answer_stands(self):
        os.environ["SLACK_RELAY_URL"] = RELAY_URL
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-local"
        self.assertFalse(slack_relay_patch.relay_without_token())

    def test_no_relay_means_the_stock_answer_stands(self):
        os.environ.pop("SLACK_RELAY_URL", None)
        os.environ.pop("SLACK_BOT_TOKEN", None)
        self.assertFalse(slack_relay_patch.relay_without_token())

    def test_whitespace_is_not_configuration(self):
        os.environ["SLACK_RELAY_URL"] = "   "
        os.environ.pop("SLACK_BOT_TOKEN", None)
        self.assertFalse(slack_relay_patch.relay_without_token())


class RelayedSlackErrorTest(unittest.TestCase):
    """Which relay 502s describe a Slack rejection, and which do not."""

    def test_slack_fields_are_returned_as_a_failed_response_payload(self):
        exc = relay_error(
            {"slack": {"error": "missing_scope", "needed": "chat:write"}}
        )
        self.assertEqual(
            slack_relay_patch.relayed_slack_error(exc),
            {"ok": False, "error": "missing_scope", "needed": "chat:write"},
        )

    def test_a_body_without_usable_slack_fields_is_not_a_rejection(self):
        for label, raw in (
            ("not JSON", b"upstream connect error"),
            ("JSON, but not an object", b"[]"),
            ("no slack key", b'{"error": "Slack operation failed"}'),
            ("nothing worth relaying", b'{"slack": {}}'),
            ("wrong shape", b'{"slack": "channel_not_found"}'),
        ):
            with self.subTest(label):
                exc = urllib.error.HTTPError(
                    RELAY_URL, 502, "Bad Gateway", Message(), io.BytesIO(raw)
                )
                self.assertIsNone(slack_relay_patch.relayed_slack_error(exc))
                # Still readable, whichever way the inspection bailed out.
                self.assertEqual(exc.read(), raw)

    def test_an_unreadable_body_is_not_a_rejection(self):
        exc = urllib.error.HTTPError(RELAY_URL, 502, "Bad Gateway", Message(), None)
        self.assertIsNone(slack_relay_patch.relayed_slack_error(exc))


if __name__ == "__main__":
    unittest.main()
