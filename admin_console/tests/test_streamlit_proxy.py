from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
