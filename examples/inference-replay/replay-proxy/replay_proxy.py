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

INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://localhost:4000")
CACHE_FILE = os.environ.get("CACHE_FILE", "/data/replay_cache.json")
MODE_FILE = os.environ.get("MODE_FILE", "/etc/replay/mode")

VALID_MODES = {"on", "off"}
DEFAULT_MODE = "off"

current_mode = DEFAULT_MODE
_mode_mtime = 0.0


def _load_mode() -> None:
    global current_mode, _mode_mtime
    try:
        st = os.stat(MODE_FILE)
    except FileNotFoundError:
        return
    if st.st_mtime == _mode_mtime:
        return
    try:
        with open(MODE_FILE, "r") as f:
            new_mode = f.read().strip().lower()
    except OSError as e:
        logger.warning(f"Failed to read mode file {MODE_FILE}: {e}; keeping mode={current_mode}")
        return
    if new_mode not in VALID_MODES:
        logger.warning(f"Invalid mode {new_mode!r} in {MODE_FILE}; keeping mode={current_mode}")
        return
    _mode_mtime = st.st_mtime
    if new_mode != current_mode:
        logger.info(f"mode changed: {current_mode} -> {new_mode}")
        current_mode = new_mode


async def _mode_watcher() -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            _load_mode()
        except Exception as e:
            logger.error(f"mode watcher error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_mode()
    logger.info(f"Starting in mode={current_mode}")
    app.state.http_client = httpx.AsyncClient(timeout=60.0)
    watcher_task = asyncio.create_task(_mode_watcher())
    try:
        yield
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        await app.state.http_client.aclose()


app = FastAPI(lifespan=lifespan)

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

# Per-message fields injected by clients (e.g. Hermes) that change every request
# and would otherwise prevent any user-message body from ever cache-hitting.
# Stripped only for hash computation — the original body is still forwarded upstream.
_VOLATILE_MSG_FIELDS = {"timestamp"}


def _canonicalize_for_hash(body: dict) -> dict:
    canonical = json.loads(json.dumps(body))
    messages = canonical.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                for field in _VOLATILE_MSG_FIELDS:
                    msg.pop(field, None)
    return canonical


def get_request_hash(body: dict) -> str:
    canonical_json = json.dumps(_canonicalize_for_hash(body), sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

async def replay_stream(lines):
    for line in lines:
        yield line + "\n"
        await asyncio.sleep(0.01)


async def _forward_completion(client, body, headers, req_hash, record):
    try:
        response = await client.post(
            f"{INFERENCE_URL}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=60.0
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact inference backend: {exc}")
    if response.status_code != 200:
        return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))
    resp_body = response.json()
    if record:
        cache[req_hash] = {"type": "completion", "data": resp_body}
        await save_cache()
    return resp_body


async def _forward_stream(client, body, headers, req_hash, record):
    req = client.build_request(
        "POST",
        f"{INFERENCE_URL}/v1/chat/completions",
        json=body,
        headers=headers,
        timeout=60.0,
    )
    try:
        response = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact inference backend: {exc}")

    if response.status_code != 200:
        try:
            content = await response.aread()
        finally:
            await response.aclose()
        return Response(content=content, status_code=response.status_code, headers=dict(response.headers))

    async def stream_body():
        recorded_lines = []
        try:
            async for line in response.aiter_lines():
                if record:
                    recorded_lines.append(line)
                yield line + "\n"
        finally:
            await response.aclose()
            if record and recorded_lines:
                logger.info(f"Recording stream response for hash: {req_hash}")
                cache[req_hash] = {"type": "stream", "data": recorded_lines}
                await save_cache()

    return StreamingResponse(stream_body(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    mode = current_mode  # snapshot for this request — mid-request reload cannot tear
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    req_hash = get_request_hash(body)
    is_stream = body.get("stream", False)
    headers = _forward_headers(request)
    client = request.app.state.http_client

    logger.info(f"chat completion request. mode={mode}, hash={req_hash}, stream={is_stream}")

    if mode == "off":
        if is_stream:
            return await _forward_stream(client, body, headers, req_hash, record=False)
        return await _forward_completion(client, body, headers, req_hash, record=False)

    if req_hash in cache:
        logger.info(f"Cache hit for hash: {req_hash}. Replaying response.")
        cache_entry = cache[req_hash]
        if cache_entry.get("type") == "stream":
            return StreamingResponse(
                replay_stream(cache_entry["data"]),
                media_type="text/event-stream"
            )
        return cache_entry["data"]

    logger.info(f"Cache miss for hash: {req_hash}. Forwarding to {INFERENCE_URL}")
    if is_stream:
        return await _forward_stream(client, body, headers, req_hash, record=True)
    return await _forward_completion(client, body, headers, req_hash, record=True)


@app.get("/admin/mode")
async def admin_mode():
    return {
        "mode": current_mode,
        "cache_size": len(cache),
        "valid_modes": sorted(VALID_MODES),
    }


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
        raise HTTPException(status_code=502, detail=f"Failed to contact inference backend: {exc}")
    return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))
