# Host-side MCP infrastructure

Helper scripts that the Scion agent containers depend on at runtime. Run these on the host (the developer's laptop) before starting any demo. ADC stays on the host; agent containers reach these services via `host.docker.internal`.

## Why two separate MCP setups

The Scion role templates use **both** GKE MCP variants:

- **Local `gke-mcp` binary** (workflow-oriented) — exposes prompts like `gke:upgrade-risk-report` and tools like `query_logs`, `list_recommendations`, `gke_deploy`, `generate_manifest`, `get_cluster`, `update_cluster`. Used by `upgrade-coordinator`, `dev-workload-guardian`, `cost-optimizer`, `workload-deployer`.
- **Remote `container.googleapis.com/mcp`** (granular K8s + node-pool API) — exposes `*_node_pool`, `apply_k8s_manifest`, `patch_k8s_resource`, `get_k8s_resource`, `list_k8s_events`, `get_k8s_rollout_status`, etc., with a 3-endpoint blast-radius split (read-only / full / delete-tools). Used by `dev-workload-guardian` and `cost-optimizer` (read-only proxy on :8082) and by `node-pool-provisioner` and `workload-deployer` (full proxy on :8081). The delete-tools endpoint isn't consumed by any current role; the proxy supports it but we don't start an instance.

The local binary handles auth via ADC natively. The remote endpoint requires a Bearer token in `Authorization`; Scion's `mcp_servers.headers` is static, so we run a small token-refreshing reverse proxy that injects a fresh token per request.

## Files

| File | What it does |
|---|---|
| `start-local-mcp.sh` | Launches `gke-mcp --server-mode http --server-port 9080`. (Port 9080 avoids colliding with Scion's web dashboard on 8080.) |
| `start-remote-mcp-proxy.sh` | Launches two `proxy.py` instances (8081 full, 8082 read-only) and waits. |
| `remote-mcp-proxy/proxy.py` | aiohttp reverse proxy. Per-request `gcloud auth print-access-token`, forwards to `container.googleapis.com/mcp{,/read-only,/delete-tools}`. |
| `remote-mcp-proxy/requirements.txt` | `aiohttp>=3.9` |

## Prerequisites on the host

1. **`gcloud` CLI** with ADC configured: `gcloud auth application-default login`
2. **`gke-mcp` binary** on PATH:
   ```sh
   curl -sSL https://raw.githubusercontent.com/GoogleCloudPlatform/gke-mcp/main/install.sh | bash
   ```
   (or `go install github.com/GoogleCloudPlatform/gke-mcp@latest`)
3. **`python3`** with venv support (used to bootstrap the proxy's `aiohttp` dependency on first run)

## Typical demo bring-up order

In four separate terminals:

```sh
# 1. Local gke-mcp
./tools/start-local-mcp.sh

# 2. Remote MCP token-refreshing proxies
./tools/start-remote-mcp-proxy.sh

# 3. Scion combo Hub + Broker + Web (in the demo project dir)
cd demos/upgrade-handshake
scion server start --foreground --enable-hub --enable-runtime-broker --enable-web

# 4. Run the demo
scion start coordinator --type platform-coordinator "$(cat opening-prompt.md)" --attach
```

Alternatively, foreground only the most-watched process (the demo) and put the others in `tmux` panes or `&` background.

## Network exposure

Both `gke-mcp --server-mode http` and `proxy.py` bind `0.0.0.0` by default so containers reaching `host.docker.internal` can connect. Anything else on the host's network can also reach them — keep the host firewalled. For laptop-only demos this is typically fine.

## Why per-request token fetch (not cached)

Tokens last ~60 min, so caching is safe and would speed things up materially. The proxy does not cache yet — fast-enough for demos and avoids a refresh edge case at expiry. Add caching when call volume justifies it.

## Verifying the proxies work

```sh
# Local gke-mcp
curl -fsS http://localhost:9080/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head

# Remote proxy (full)
curl -fsS http://localhost:8081/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head
```

If the remote proxy returns 500 with "failed to fetch GCP access token", run `gcloud auth application-default login`.
