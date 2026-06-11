import os
import json
import hashlib
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("replay-proxy-v1")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=60.0)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(lifespan=lifespan)

INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://localhost:4000")
CACHE_FILE = os.environ.get("CACHE_FILE", "/data/replay_cache.json")

_HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}


def _forward_headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}


cache = {}
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        logger.info(f"Loaded {len(cache)} entries from cache file {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Failed to load cache file: {e}")
else:
    logger.info(f"Cache file {CACHE_FILE} not found, starting with empty cache.")

_cache_lock = asyncio.Lock()

def _write_cache_atomic(snapshot: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    tmp_path = f"{CACHE_FILE}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp_path, CACHE_FILE)

async def save_cache():
    async with _cache_lock:
        snapshot = dict(cache)
        try:
            await asyncio.to_thread(_write_cache_atomic, snapshot)
            logger.info(f"Saved cache to {CACHE_FILE}")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

def get_request_hash(body: dict) -> str:
    canonical_json = json.dumps(body, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

async def replay_stream(lines):
    for line in lines:
        yield line + "\n"
        await asyncio.sleep(0.01)

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    req_hash = get_request_hash(body)
    is_stream = body.get("stream", False)
    
    logger.info(f"Received chat completion request. Hash: {req_hash}, Stream: {is_stream}")
    
    if req_hash in cache:
        logger.info(f"Cache hit for hash: {req_hash}. Replaying response.")
        cache_entry = cache[req_hash]
        if cache_entry.get("type") == "stream":
            return StreamingResponse(
                replay_stream(cache_entry["data"]),
                media_type="text/event-stream"
            )
        else:
            return cache_entry["data"]
            
    # Cache miss -> Forward to inference backend and record
    logger.info(f"Forwarding inference request to: {INFERENCE_URL}")
    
    headers = _forward_headers(request)
    
    client = request.app.state.http_client

    if is_stream:
        return StreamingResponse(
            forward_and_record_stream(client, body, headers, req_hash),
            media_type="text/event-stream"
        )
    else:
        try:
            response = await client.post(
                f"{INFERENCE_URL}/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=60.0
            )
            if response.status_code != 200:
                return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))

            resp_body = response.json()
            cache[req_hash] = {
                "type": "completion",
                "data": resp_body
            }
            await save_cache()
            return resp_body
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to contact LiteLLM: {exc}")

async def forward_and_record_stream(client, body, headers, req_hash):
    recorded_lines = []
    async with client.stream(
        "POST",
        f"{INFERENCE_URL}/v1/chat/completions",
        json=body,
        headers=headers,
        timeout=60.0
    ) as response:
        if response.status_code != 200:
            content = await response.aread()
            yield content
            return
        async for line in response.aiter_lines():
            recorded_lines.append(line)
            yield line + "\n"

    if recorded_lines:
        logger.info(f"Recording stream response for hash: {req_hash}")
        cache[req_hash] = {
            "type": "stream",
            "data": recorded_lines
        }
        await save_cache()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def fallback(request: Request, path: str):
    logger.info(f"Fallback forwarding for path: {path}")
    client = request.app.state.http_client
    url = f"{INFERENCE_URL}/{path}"
    headers = _forward_headers(request)
    method = request.method
    content = await request.body()

    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            content=content,
            params=request.query_params,
            timeout=60.0
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact LiteLLM: {exc}")
    return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))

