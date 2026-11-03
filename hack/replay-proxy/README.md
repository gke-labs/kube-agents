# Shadow-mode replay proxy

The shadow-mode replay proxy is an eval fixture. It sits in front of LiteLLM on an eval install and records, for every model request, what an inference replay cache would have keyed on. It never serves a saved answer: every request goes to LiteLLM and every response comes back unchanged. It is stage 1 of the rollout in [`examples/inference-replay/eval-design.md`](../../examples/inference-replay/eval-design.md), which owns the normalization rules, the cache key and the reasons behind them. An install never uses it, and nothing deploys it yet.

## What it does

For each `/v1/chat/completions` request, after the response has gone back to the client:

- `normalizer.py` normalizes the request and computes its fingerprint: the normalized request without `stream`, `user` and `metadata`, the LiteLLM config checksum, the LiteLLM image, and `NORMALIZER_VERSION` (a hash of `normalizer.py`).
- `replay_proxy.py` writes one JSON line to stdout (`"event": "replay_shadow_request"`): the fingerprint, per-message hashes and roles, prompt, cached and completion tokens, model latency, a hash of the answer, LiteLLM's `x-litellm-model-group` and `x-litellm-attempted-fallbacks` headers, and why replay would not have saved the answer (`unstorable`). The line holds no request or answer text.
- `request_store.py` scrubs the request with the install's `AuditRedactor` and then the `worker_transcript.py` rules, the order `bench/kube_agents_bench/worker_trajectory.py` uses, and writes it under `REPLAY_STORE_DIR`: each distinct message once under `blobs/<sha256>.json`, and each request as a list of blob hashes under `requests/<request_id>.json`. If either scrubber cannot load, or scrubbing a request fails, no text from it is written; its line is still logged.

`/metrics` is answered by the proxy with its own counters and is never forwarded, because the proxy carries the `app: litellm` label (see "Where the proxy runs" in the design). `/replay/healthz` is its health check. Every other path goes to LiteLLM unchanged. The request body is forwarded byte for byte; the hop-by-hop headers and `Accept-Encoding` are dropped, so the usage block can be read.

`analyze_shadow.py` reads the lines from a set of jobs and reports the token-weighted would-hit rate (low: first seen from a nightly; high: also from another presubmit build), the share of hits first seen in another project, the tokens and model time hits would save, and the misses ranked by tokens lost with the closest earlier request by longest matching message prefix. Its `simulated` row also counts a request as a hit when only the wording of earlier answers differs, since replay would have served those answers; the module docstring says what that assumes.

## Configuration

| Environment variable              | Default                               | Meaning                                                 |
| --------------------------------- | ------------------------------------- | ------------------------------------------------------- |
| `REPLAY_LISTEN_PORT`              | `8080`                                | Port the proxy listens on                               |
| `REPLAY_UPSTREAM_HOST`            | `inference-gateway-upstream`          | LiteLLM's host                                          |
| `REPLAY_UPSTREAM_PORT`            | `80`                                  | LiteLLM's port                                          |
| `REPLAY_UPSTREAM_TIMEOUT_SECONDS` | `1800`                                | Socket timeout for each read from LiteLLM               |
| `REPLAY_LITELLM_CONFIG_CHECKSUM`  | empty                                 | The LiteLLM pod template's `checksum/config` annotation |
| `REPLAY_LITELLM_IMAGE`            | empty                                 | The LiteLLM container image                             |
| `REPLAY_STORE_DIR`                | `/data/shadow`                        | Where scrubbed request text is written                  |
| `REPLAY_REDACTOR_PATH`            | `/app/scrubbers/redactor.py`          | The install's `AuditRedactor`                           |
| `REPLAY_TRANSCRIPT_RULES_PATH`    | `/app/scrubbers/worker_transcript.py` | The shared transcript scrubbing rules                   |
| `REPLAY_JOB_KIND`                 | empty                                 | `nightly` or `presubmit`, stamped on every line         |
| `REPLAY_JOB_PROJECT`              | empty                                 | The eval project, stamped on every line                 |
| `REPLAY_JOB_BUILD`                | empty                                 | The Prow build ID, stamped on every line                |

The image is built from the repository root (`docker build -f hack/replay-proxy/Dockerfile .`), because it copies the scrubbers from `agents/`.

## Tests

The tests are standard-library `unittest` and need no cluster: `make test-python` runs them, or run them from this directory with `python3 -m unittest`. `test_replay_proxy.py` runs the proxy against a fake LiteLLM on localhost.
