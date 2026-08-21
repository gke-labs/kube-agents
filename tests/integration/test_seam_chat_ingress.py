"""Seam: Google Chat Pub/Sub ingress — relay patch ↔ credential proxy, real HTTP.

The product's front door: Pub/Sub events reach the credential proxy's
GoogleChatRelay, and the gateway pulls them through loopback HTTP via
`google_chat_relay_patch`. Until this file the patch had no test of any kind.
Here the real patch closures drive the real `CredentialProxyHandler` HTTP
endpoints over a socket; the fakes sit exactly at the seam's two ends — a
queue-backed relay standing in for Pub/Sub, and a minimal adapter standing in
for the hermes gateway (the component this repo does not own).

The expectedFailure at the bottom pins the known malformed-payload hole: a
non-base64 `data` raises inside RelayMessage.__init__ before the receipt is
captured, so the event is never nacked and Pub/Sub redelivers it forever —
there is no DLQ. The desired contract is a nack (or any settle) for every
pulled event, malformed included.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import threading
import types
import unittest
from http.server import ThreadingHTTPServer

from _seams import SCRIPTS_DIR

sys.path.insert(0, str(SCRIPTS_DIR))


class FakeChatRelay:
    """Queue-backed stand-in for the Pub/Sub side of GoogleChatRelay."""

    def __init__(self):
        self.events = []
        self.settled = []  # (receipt, acknowledged)
        self.api_calls = []  # (resource, method, arguments)
        self._lock = threading.Lock()

    def pull(self):
        with self._lock:
            return self.events.pop(0) if self.events else None

    def settle(self, receipt, acknowledge):
        self.settled.append((receipt, acknowledge))
        return True

    def api_call(self, resource, method, arguments):
        self.api_calls.append((resource, method, arguments))
        return {"name": "spaces/AAA/messages/m1"}


def make_adapter_class():
    """A fresh minimal adapter class per test.

    Fresh per test because `patch_adapter_class` marks the class it patched and
    returns early on the next sight of it; a module-level class would keep the
    first test's closures — and with them the first test's closed proxy port —
    for every test after it.
    """

    class MinimalAdapter:
        """Just enough gateway-adapter surface for the patched connect()/loop."""

        def __init__(self):
            self.received = []
            self.on_message_error = None
            self._shutting_down = False

            class _Store:
                def load(self):
                    return None

            self._thread_count_store = _Store()
            self.connected = False

        def _load_cached_bot_id(self):
            return "bot-user"

        def _mark_connected(self):
            self.connected = True

        def _mark_disconnected(self):
            self.connected = False

        def _on_pubsub_message(self, message):
            if self.on_message_error is not None:
                raise self.on_message_error
            self.received.append(message)
            message.ack()

    return MinimalAdapter


class ChatIngressSeamTest(unittest.TestCase):
    def setUp(self):
        adapter_class = make_adapter_class()

        # The patch imports gateway.platform_registry, a hermes module this
        # repository does not ship; a stub registry is the fake-agent line of
        # this tier. Everything the patch does is real.
        registry_module = types.ModuleType("gateway.platform_registry")

        class PlatformRegistry:
            # install() wraps this method; returning a real adapter object for
            # google_chat is what makes the wrapper patch the adapter class —
            # the same registration path production takes.
            def create_adapter(self, name, *args, **kwargs):
                return adapter_class() if name == "google_chat" else None

        registry_module.PlatformRegistry = PlatformRegistry
        gateway_pkg = types.ModuleType("gateway")
        gateway_pkg.platform_registry = registry_module
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in (
                "gateway",
                "gateway.platform_registry",
                "google_chat_relay_patch",
            )
        }
        sys.modules["gateway"] = gateway_pkg
        sys.modules["gateway.platform_registry"] = registry_module

        from credential_proxy import CredentialProxyHandler

        self.relay = FakeChatRelay()
        CredentialProxyHandler.chat_relay = self.relay
        # serve() sets this in production; the class only annotates it.
        CredentialProxyHandler.max_request_bytes = 65536
        self.addCleanup(setattr, CredentialProxyHandler, "chat_relay", None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        os.environ["GOOGLE_CHAT_RELAY_URL"] = (
            f"http://127.0.0.1:{self.server.server_port}"
        )
        self.addCleanup(os.environ.pop, "GOOGLE_CHAT_RELAY_URL", None)

        sys.modules.pop("google_chat_relay_patch", None)
        import google_chat_relay_patch

        google_chat_relay_patch.install()
        registry = PlatformRegistry()
        self.adapter = registry.create_adapter("google_chat")
        self.assertIsInstance(self.adapter, adapter_class)
        self.assertTrue(
            getattr(adapter_class, "_credential_proxy_relay_patched", False),
            "install() must patch the adapter class through the registry path",
        )

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _run_adapter(self, events, on_message_error=None, run_for=6.0, until=None):
        """connect() the patched adapter, feed events, wait, disconnect."""
        adapter = self.adapter
        adapter.on_message_error = on_message_error
        self.relay.events.extend(events)

        async def drive():
            ok = await type(adapter).connect(adapter)
            assert ok
            loop = asyncio.get_event_loop()
            deadline = loop.time() + run_for
            while loop.time() < deadline:
                if until is not None and until():
                    break
                await asyncio.sleep(0.05)
            await type(adapter).disconnect(adapter)

        asyncio.run(drive())
        return adapter

    def _event(self, receipt="r-1", data=b'{"kind":"chat"}', message_id="m-1"):
        return {
            "data": base64.b64encode(data).decode("ascii"),
            "attributes": {"origin": "pubsub"},
            "messageId": message_id,
            "receipt": receipt,
        }

    def test_a_pulled_event_is_decoded_delivered_and_acked_with_its_receipt(self):
        adapter = self._run_adapter([self._event()], until=lambda: self.relay.settled)
        self.assertEqual(1, len(adapter.received))
        message = adapter.received[0]
        self.assertEqual(b'{"kind":"chat"}', message.data)
        self.assertEqual("m-1", message.message_id)
        self.assertEqual([("r-1", True)], self.relay.settled)

    def test_a_handler_exception_nacks_the_same_receipt(self):
        self._run_adapter(
            [self._event(receipt="r-crash")],
            on_message_error=RuntimeError("handler blew up"),
            until=lambda: self.relay.settled,
        )
        self.assertEqual([("r-crash", False)], self.relay.settled)

    def test_the_chat_api_facade_routes_message_create_through_the_proxy(self):
        adapter = self.adapter

        async def call():
            ok = await type(adapter).connect(adapter)
            assert ok
            request = adapter._chat_api.spaces().messages().create(
                parent="spaces/AAA", body={"text": "hello"}
            )
            result = await asyncio.to_thread(request.execute)
            await type(adapter).disconnect(adapter)
            return result

        result = asyncio.run(call())
        self.assertEqual({"name": "spaces/AAA/messages/m1"}, result)
        self.assertEqual(1, len(self.relay.api_calls))
        resource, method, arguments = self.relay.api_calls[0]
        self.assertEqual(["spaces", "messages"], resource)
        self.assertEqual("create", method)
        self.assertEqual("spaces/AAA", arguments["parent"])

    @unittest.expectedFailure
    def test_a_malformed_event_is_settled_rather_than_redelivered_forever(self):
        """DESIRED, not current, behaviour — the infinite-redelivery hole.

        A non-base64 `data` raises in RelayMessage.__init__ before the receipt
        is captured; `message` is still None in the loop's except, so nothing
        nacks, the receipt stays outstanding, and Pub/Sub redelivers the same
        poison event forever. There is no DLQ. The contract this pins: every
        pulled event settles — malformed ones nacked (or dead-lettered), so
        one bad payload cannot occupy the front door indefinitely.
        """
        poison = {
            "data": "!!!not-base64!!!",
            "attributes": {},
            "messageId": "m-poison",
            "receipt": "r-poison",
        }
        self._run_adapter([poison], until=lambda: self.relay.settled, run_for=4.0)
        # The contract is "every pulled event settles" — a nack is the obvious
        # implementation, but an ack after dead-lettering satisfies it too, so
        # the assertion is on the receipt being settled at all.
        self.assertTrue(
            any(receipt == "r-poison" for receipt, _ in self.relay.settled),
            "a malformed event must be settled, never silently redelivered",
        )


if __name__ == "__main__":
    unittest.main()
