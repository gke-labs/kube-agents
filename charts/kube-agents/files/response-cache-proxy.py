"""cached-response passthrough in front of the LiteLLM container (experiment).

Runs as a sidecar in the litellm pod when ``litellm.responseCache`` is set;
the Service's targetPort lands here and everything forwards to the LiteLLM
container on localhost. The one inference route is decorated with
``cached_llm_response`` configured the way the package's own
``configure-cached-response`` skill workflow arrived at against this
application's captured traffic:

* ``test_aliases`` maps the kanban task ids the platform mints per run
  (``t_<8 hex>``) to stable handles, keyed on the first id seen — unique per
  repetition, stable within one conversation. Thought-signature bytes are
  preserved by the package; a replayed turn re-serves the recorded signed
  tool call and the agent echoes it, so later turns keep matching.
* ``TestMetadata`` declares the kanban bookkeeping numerics that change
  every repetition (epochs, run ids, lease expiries, worker pids) and
  nothing else; ``learning=False`` keeps the judge out entirely, so a
  difference outside these declarations is a live call, never a model
  adjudication.

The task-id pattern and TestMetadata rows track the hermes kanban tool
schema (``kanban_create``/``kanban_show`` results) as shipped in the agent
image this chart deploys; a schema change there invalidates them silently,
so revisit this file when bumping the agent image.

``GET /__cache_stats`` reports hit/miss counters — content-free, safe to
archive; redacted recent-miss excerpts are included only with
``?include_misses=1`` and must stay out of public artifacts. The mode comes
from CACHED_RESPONSE_MODE (empty = disabled = plain passthrough) and the
store from CACHED_RESPONSE_PATH; both are set by the chart. A cached
completion measures the cache, not the model: no graded or production
install enables this.
"""

import contextlib
import json
import logging
import os
import re

import httpx
import uvicorn
from cached_response import (
    TestMetadata,
    cache_misses,
    cache_stats,
    cached_llm_response,
    configure,
)
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# Defaults mirror the chart's only deployment shape: the sidecar owns 8080
# (the port every NetworkPolicy admits) and the gateway container listens on
# localhost:4000.
UPSTREAM = os.environ.get("RESPONSE_CACHE_UPSTREAM", "http://127.0.0.1:4000")
LISTEN_PORT = int(os.environ.get("RESPONSE_CACHE_PORT", "8080"))
STORE_PATH = os.environ.get("CACHED_RESPONSE_PATH", "/tmp/response-cache-store")
STREAM_MIME = "text/event-stream"
# Hop-by-hop and length/encoding headers must not survive re-materialization:
# httpx has already decompressed the body, and content-length is recomputed.
DROP_REQUEST_HEADERS = {"host", "content-length", "accept-encoding"}
DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}
KANBAN_TASK_ID = re.compile(r"\bt_[0-9a-f]{8}\b")
STATS_MISS_SAMPLE = 5

LOGGER = logging.getLogger("response_cache_proxy")

configure(
    learning=False,
    min_words=0,
    path=STORE_PATH,
    test_metadata=(
        TestMetadata("kanban_show", (("task", "created_at"),)),
        TestMetadata("kanban_show", (("task", "started_at"),)),
        TestMetadata("kanban_show", (("task", "completed_at"),)),
        TestMetadata("kanban_show", (("task", "current_run_id"),)),
        TestMetadata("kanban_show", (("runs", "*", "id"),)),
        TestMetadata("kanban_show", (("runs", "*", "started_at"),)),
        TestMetadata("kanban_show", (("runs", "*", "ended_at"),)),
        TestMetadata("kanban_show", (("events", "*", "created_at"),)),
        TestMetadata("kanban_show", (("events", "*", "run_id"),)),
        TestMetadata("kanban_show", (("events", "*", "payload", "expires"),)),
        TestMetadata("kanban_show", (("events", "*", "payload", "run_id"),)),
        TestMetadata("kanban_show", (("events", "*", "payload", "pid"),)),
    ),
)


def _task_aliases(request: dict) -> tuple[str, dict[str, str]] | None:
    """Map this conversation's kanban task ids to stable per-position handles.

    The first task id a conversation mints is unique to its repetition and
    stable for every later turn, which makes it the per-fixture conversation
    key the package's alias contract asks for. Returns None before any id
    exists, which disables aliasing for that call.
    """
    seen: list[str] = []
    for message in request.get("messages") or []:
        for task_id in KANBAN_TASK_ID.findall(json.dumps(message)):
            if task_id not in seen:
                seen.append(task_id)
    if not seen:
        return None
    return seen[0], {t: f"test_id_{i:08d}" for i, t in enumerate(seen)}


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    await client.aclose()


app = FastAPI(lifespan=_lifespan)
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=None)


def _request_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in DROP_REQUEST_HEADERS
    }


def _response_headers(reply: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in reply.headers.items()
        if name.lower() not in DROP_RESPONSE_HEADERS
    }


async def _forward(request: Request, path: str) -> Response:
    """Forward one request to the gateway container, streaming SSE through.

    Non-stream replies are materialized so the caching decorator can store
    them; SSE replies keep a live iterator, which the decorator records
    chunk-wise and commits when the stream completes.
    """
    upstream = client.build_request(
        request.method,
        path,
        content=await request.body(),
        headers=_request_headers(request),
        params=request.query_params,
    )
    reply = await client.send(upstream, stream=True)
    if STREAM_MIME in reply.headers.get("content-type", ""):

        async def relay():
            try:
                async for chunk in reply.aiter_bytes():
                    yield chunk
            finally:
                await reply.aclose()

        return StreamingResponse(
            relay(), status_code=reply.status_code, headers=_response_headers(reply)
        )
    body = await reply.aread()
    await reply.aclose()
    return Response(
        content=body, status_code=reply.status_code, headers=_response_headers(reply)
    )


# signed_call_handles: Gemini mints a fresh thought signature every
# generation, which used to make one live turn poison every later lookup in
# its conversation; the package keys signed call tokens as stable positional
# handles while forwarding and serving real bytes.
@cached_llm_response(
    namespace="kube-agents-eval",
    version="2",
    test_aliases=_task_aliases,
    alias_version="1",
    signed_call_handles=True,
)
async def _cached_chat(request: Request) -> Response:
    return await _forward(request, "/v1/chat/completions")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Serve from cache when possible, and never fail the agent's call.

    The cache layer raises rather than degrades in one documented case: a
    test-alias contract it cannot satisfy (the package surfaces it so a
    caller's callback bug is visible, and its tests pin that). At an
    inference endpoint that exception is an HTTP 500 the agent's worker dies
    on — 741 of them in build 2098114579014881280, which blocked every task
    in the run. An inference proxy must degrade to a live call instead, so
    the cache can only ever cost latency, never availability.
    """
    try:
        return await _cached_chat(request)
    except Exception:
        LOGGER.exception("response cache failed; forwarding live")
        return await _forward(request, "/v1/chat/completions")


@app.get("/__cache_stats")
async def stats(include_misses: int = 0) -> JSONResponse:
    payload = {"stats": cache_stats()}
    if include_misses:
        payload["recent_misses"] = cache_misses(STATS_MISS_SAMPLE)
    return JSONResponse(payload)


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
async def passthrough(path: str, request: Request) -> Response:
    return await _forward(request, "/" + path)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)
