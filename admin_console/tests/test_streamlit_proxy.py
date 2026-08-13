from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from admin_console.api.streamlit_proxy import (
    _same_websocket_origin,
    register_streamlit_proxy,
    upstream_http,
)


class StreamlitProxySecurityTest(unittest.TestCase):
    def test_same_origin_requires_matching_scheme_host_and_port(self) -> None:
        self.assertTrue(
            _same_websocket_origin(
                "http://127.0.0.1:8501",
                "ws://127.0.0.1:8501/_stcore/stream",
            )
        )
        self.assertTrue(
            _same_websocket_origin(
                "https://console.example",
                "wss://console.example/_stcore/stream",
            )
        )
        for origin in (
            "",
            "null",
            "https://attacker.example",
            "http://127.0.0.1:8502",
            "https://127.0.0.1:8501",
            "http://127.0.0.1:8501/untrusted",
        ):
            with self.subTest(origin=origin):
                self.assertFalse(
                    _same_websocket_origin(
                        origin,
                        "ws://127.0.0.1:8501/_stcore/stream",
                    )
                )

    def test_cross_origin_upgrade_never_reaches_streamlit(self) -> None:
        app = FastAPI()
        register_streamlit_proxy(app)

        with patch(
            "admin_console.api.streamlit_proxy.websockets.connect"
        ) as connect:
            with self.assertRaises(WebSocketDisconnect) as raised:
                with TestClient(app).websocket_connect(
                    "/_stcore/stream",
                    headers={"origin": "https://attacker.example"},
                ):
                    pass

        self.assertEqual(raised.exception.code, 1008)
        connect.assert_not_called()

    def test_same_origin_upgrade_reaches_private_streamlit(self) -> None:
        class EmptyUpstream:
            subprotocol = None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class UpstreamContext:
            async def __aenter__(self):
                return EmptyUpstream()

            async def __aexit__(self, *args):
                return None

        app = FastAPI()
        register_streamlit_proxy(app)
        with patch(
            "admin_console.api.streamlit_proxy.websockets.connect",
            return_value=UpstreamContext(),
        ) as connect:
            with TestClient(app).websocket_connect(
                "/_stcore/stream",
                headers={"origin": "http://testserver"},
            ):
                pass

        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["origin"], upstream_http())

    def test_dead_http_upstream_returns_service_unavailable(self) -> None:
        class DeadClient:
            async def request(self, *args, **kwargs):
                raise httpx.ConnectError("private Streamlit listener is down")

        app = FastAPI()
        app.state.streamlit_ready = True
        app.state.streamlit_client = DeadClient()
        register_streamlit_proxy(app)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/some-page")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.text, "Streamlit is unavailable.")

    def test_exited_streamlit_process_is_rechecked_before_proxying(self) -> None:
        class ExitedProcess:
            def poll(self):
                return 1

        class UnexpectedClient:
            async def request(self, *args, **kwargs):
                raise AssertionError("dead process must not be contacted")

        app = FastAPI()
        app.state.streamlit_ready = True
        app.state.streamlit_process = ExitedProcess()
        app.state.streamlit_client = UnexpectedClient()
        register_streamlit_proxy(app)

        with TestClient(app) as client:
            response = client.get("/some-page")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(app.state.streamlit_ready)


if __name__ == "__main__":
    unittest.main()
