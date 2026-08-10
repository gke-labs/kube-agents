"""Single-port FastAPI parent and private Streamlit reverse proxy."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from websockets.exceptions import ConnectionClosed, WebSocketException

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
DECODED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding", "content-length"}
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PACKAGE_ROOT / "app.py"


def streamlit_port() -> int:
    raw = os.environ.get("ADMIN_PORTAL_STREAMLIT_PORT", "8502")
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("ADMIN_PORTAL_STREAMLIT_PORT must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise RuntimeError("ADMIN_PORTAL_STREAMLIT_PORT must be between 1024 and 65535")
    return port


def upstream_http() -> str:
    return f"http://127.0.0.1:{streamlit_port()}"


async def _wait_for_streamlit(
    process: subprocess.Popen,
    client: httpx.AsyncClient,
    *,
    timeout: float = 30,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Streamlit stopped during startup with exit code {process.returncode}."
            )
        try:
            response = await client.get("/_stcore/health", timeout=1)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("Streamlit did not become ready within 30 seconds.")


@asynccontextmanager
async def portal_lifespan(app: FastAPI):
    environment = os.environ.copy()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP_PATH),
            "--server.address=127.0.0.1",
            f"--server.port={streamlit_port()}",
            "--server.headless=true",
            "--server.enableXsrfProtection=true",
            "--server.enableCORS=true",
            "--browser.gatherUsageStats=false",
        ],
        cwd=PACKAGE_ROOT.parent,
        env=environment,
    )
    client = httpx.AsyncClient(base_url=upstream_http(), follow_redirects=False)
    app.state.streamlit_process = process
    app.state.streamlit_client = client
    try:
        await _wait_for_streamlit(process, client)
        app.state.streamlit_ready = True
        yield
    finally:
        app.state.streamlit_ready = False
        await client.aclose()
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            except TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)


def _request_headers(headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP
        and name.lower() not in {"host", "origin", "x-kube-agents-user"}
        and not name.lower().startswith("sec-websocket-")
    }


def register_streamlit_proxy(app: FastAPI) -> None:
    """Register catch-all routes after every API route."""

    @app.websocket("/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str) -> None:
        query = websocket.url.query
        upstream_url = f"ws://127.0.0.1:{streamlit_port()}/{path}"
        if query:
            upstream_url += f"?{query}"
        headers = _request_headers(websocket.headers)
        requested_protocols = [
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        ]
        try:
            async with websockets.connect(
                upstream_url,
                additional_headers=headers,
                origin=upstream_http(),
                subprotocols=requested_protocols or None,
                max_size=None,
            ) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)

                async def browser_to_streamlit() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def streamlit_to_browser() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                first = asyncio.create_task(browser_to_streamlit())
                second = asyncio.create_task(streamlit_to_browser())
                done, pending = await asyncio.wait(
                    {first, second},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
        except (WebSocketDisconnect, ConnectionClosed):
            return
        except (OSError, WebSocketException):
            await websocket.close(code=1011)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def proxy_http(request: Request, path: str) -> Response:
        if not getattr(app.state, "streamlit_ready", False):
            return Response("Streamlit is starting.", status_code=503)
        query = request.url.query
        upstream_path = f"/{path}" + (f"?{query}" if query else "")
        response = await app.state.streamlit_client.request(
            request.method,
            upstream_path,
            headers=_request_headers(request.headers),
            content=await request.body(),
        )
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in DECODED_RESPONSE_HEADERS
        }
        location = headers.get("location")
        if location:
            parsed = urlsplit(location)
            if parsed.hostname in {"127.0.0.1", "localhost"}:
                headers["location"] = parsed.path + (
                    f"?{parsed.query}" if parsed.query else ""
                )
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=headers,
            media_type=response.headers.get("content-type"),
        )
