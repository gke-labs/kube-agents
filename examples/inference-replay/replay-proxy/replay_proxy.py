import os
import json
import hashlib
import logging
import asyncio
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("replay-proxy-v1")

app = FastAPI()

LITELLM_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
CACHE_FILE = os.environ.get("CACHE_FILE", "/data/replay_cache.json")

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

def save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"Saved cache to {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Failed to save cache: {e}")

def get_request_hash(body: dict) -> str:
    canonical_json = json.dumps(body, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

async def replay_stream(chunks):
    for chunk in chunks:
        if isinstance(chunk, dict):
            yield f"data: {json.dumps(chunk)}\n\n"
        else:
            yield chunk
        await asyncio.sleep(0.01)
    yield "data: [DONE]\n\n"

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
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
            
    # Cache miss -> Forward to LiteLLM and record
    logger.info(f"Forwarding request to LiteLLM: {LITELLM_URL}")
    
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    
    if is_stream:
        return StreamingResponse(
            forward_and_record_stream(body, headers, req_hash),
            media_type="text/event-stream"
        )
    else:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{LITELLM_URL}/v1/chat/completions",
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
                save_cache()
                return resp_body
            except httpx.RequestError as exc:
                raise HTTPException(status_code=500, detail=f"Failed to contact LiteLLM: {exc}")

async def forward_and_record_stream(body, headers, req_hash):
    recorded_chunks = []
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            f"{LITELLM_URL}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=60.0
        ) as response:
            if response.status_code != 200:
                content = await response.aread()
                yield content
                return
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        yield line + "\n\n"
                        continue
                    try:
                        data_json = json.loads(data_str)
                        recorded_chunks.append(data_json)
                    except json.JSONDecodeError:
                        recorded_chunks.append(line + "\n\n")
                else:
                    recorded_chunks.append(line + "\n\n")
                yield line + "\n\n"
                
    if recorded_chunks:
        logger.info(f"Recording stream response for hash: {req_hash}")
        cache[req_hash] = {
            "type": "stream",
            "data": recorded_chunks
        }
        save_cache()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def fallback(request: Request, path: str):
    logger.info(f"Fallback forwarding for path: {path}")
    async with httpx.AsyncClient() as client:
        url = f"{LITELLM_URL}/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        method = request.method
        content = await request.body()
        
        response = await client.request(
            method,
            url,
            headers=headers,
            content=content,
            params=request.query_params,
            timeout=60.0
        )
        return Response(content=response.content, status_code=response.status_code, headers=dict(response.headers))

