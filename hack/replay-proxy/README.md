# Shadow-mode replay proxy

The shadow-mode replay proxy is an eval fixture. It sits in front of LiteLLM on an eval install and records, for every model request, what an inference replay cache would have keyed on. In its default shadow mode it never serves a saved answer: every request goes to LiteLLM and every response comes back unchanged. Record and replay modes (below) are for the replay lane. It is part of stage 1 of the rollout in [`examples/inference-replay/eval-design.md`](../../examples/inference-replay/eval-design.md), which owns the normalization rules, the cache key and the reasons behind them: the proxy, the fingerprint log, the scrubbed request store and the hit-rate script. The join with run verdicts and the collision report are not built yet. An install never uses it, and nothing deploys it yet.

## What it does

For each `/v1/chat/completions` request, after the response has gone back to the client, a background thread does the following. The handler only queues the request; if the queue is full it drops the analysis and logs an `analysis_dropped` line instead of waiting.

- `normalizer.py` normalizes the request and computes its fingerprint: the normalized request without `stream`, `user` and `metadata`, the LiteLLM config checksum, the LiteLLM image, and `NORMALIZER_VERSION` (a hash of `normalizer.py`).
- `replay_proxy.py` writes one JSON line to stdout (`"event": "replay_shadow_request"`): the fingerprint, per-message hashes and roles, prompt, cached and completion tokens, model latency, a hash of the answer (when the response carried no usage, `prompt_tokens` is estimated from the request size and `tokens_estimated` is true), LiteLLM's `x-litellm-model-group` and `x-litellm-attempted-fallbacks` headers, and why replay would not have saved the answer (`unstorable`: for example an upstream that broke off mid-answer, a response the proxy could not parse, or an answer whose scrubbing would mask a credential; reformatting or masking an email or IP doesn't count). A shadow or record line holds no request or answer text (a replay miss adds the short scrubbed snippets described under Modes), but its hashes are of the normalized request and answer and can be guessed against, so the log goes to the same private bucket as the stored text, never to Prow artifacts.
- `diagnosis.py` adds `diag_message_hashes` and `diag_settings_hash`, used only to find the closest earlier request for a miss: each message is normalized with the time the proxy first saw it, and IPs stay literal only if a CIDR appeared in it or an earlier message, so a later CIDR or a later request time doesn't make an earlier message look different. The settings get their own hash, normalized without any message's context. The proxy keeps one first-seen timestamp per distinct message for this, up to a million, oldest dropped first. `schema.py` names the line's fields for both the proxy and the analysis.
- `request_store.py` scrubs the request with the install's `AuditRedactor` and then the `worker_transcript.py` rules, the order `bench/kube_agents_bench/worker_trajectory.py` uses, and writes it under `REPLAY_STORE_DIR`: each distinct message once under `blobs/<sha256>.json`, and each request as a list of blob hashes under `requests/<request_id>.json`. If either scrubber cannot load or scrubbing a request fails, no text from it is written. If a write fails partway, some of its message blobs may be on disk but no request manifest is. Either way its line is still logged.

`/metrics` is answered by the proxy with its own counters and is never forwarded, because the proxy carries the `app: litellm` label (see "Where the proxy runs" in the design). `/replay/healthz` is its health check. Every other path goes to LiteLLM unchanged. The request body is forwarded byte for byte; the hop-by-hop headers and `Accept-Encoding` are dropped, so the usage block can be read. A request body over 64 MiB gets a 413 and is not forwarded. The proxy stops observing a response, streamed or not, after 8 MiB or at anything it can't parse; the bytes keep flowing to the client unchanged. The line then has `observe_failed: true` and the `unstorable` reason `observe_too_large` or `observe_failed`. When the response was cut short, `upstream_incomplete` or `client_disconnected` is the reason instead.

`analyze_shadow.py` reads the lines from a set of jobs and reports the token-weighted would-hit rate. Low counts nightly sightings. High adds earlier finished builds of the same pull request and, given `--merged` (pull request to merge time), another pull request's last build before it merged. Until run verdicts are joined, high is an upper bound: it counts those builds, and nightly answers once written, whatever their runs' verdicts. The report counts presubmit lines without a pull request number, which shows an unset `REPLAY_JOB_PR`. It also reports the share of hits first seen in another project, the tokens and model time hits would save, and the misses ranked by tokens lost with the closest earlier request by longest matching message prefix. Its `simulated` row is high plus the requests that match with earlier assistant messages ignored, since replay would have served those; the module docstring says what that assumes. For multi-agent runs every estimate here is a floor: one agent's answers feed another's input and pick its actions, which shadow mode can't hold fixed, so the real hit rate comes from the design's replay lane (Rollout step 2), which runs real replay where it gates nothing.

Check the report's health counts after the first day: lines by job kind; untagged lines (an unset or unknown `REPLAY_JOB_KIND`, dropped from every estimate); error lines; lines without a fingerprint; `analysis_dropped`; unstorable lines and their share by reason; presubmit lines without a pull request number; lines without message hashes; the upstream-incomplete, observe-failed and observe-too-large counts; and the share of prompt tokens that are estimated. A high number in any of them means the setup is broken, not that replay would miss.

## Modes

`REPLAY_MODE` picks one of three:

- `shadow` (the default): everything above, nothing saved, nothing served.
- `record`: shadow, plus each storable answer (empty `unstorable`) saved under its fingerprint in `REPLAY_CACHE_DIR` (`replay_cache.py`): the answer with the values its request bound swapped for placeholders, its finish reason and usage, and the request's hashes. One file per key, written atomically; the first write wins. Each request message is also saved once, normalized and scrubbed, so a later miss can show how it differed. Record mode never serves.
- `replay`: on a fingerprint hit in `REPLAY_CACHE_DIR`, the saved answer is restored with the current request's values and served, streamed or not, without calling the model; an answer naming a placeholder the request doesn't bind is a miss. A miss goes to the model live and is not recorded. The cache is only read, and its index is built when the proxy starts.

Replay mode refuses to start unless `REPLAY_ALLOW_SERVE=yes` is also set, and nothing in the repository sets it, so a saved answer can't be served by accident.

Every line carries `mode` and `served`. A served line has `served_from`, the job that recorded the answer. A replay miss has a `miss` block: the closest saved request by longest matching `diag_message_hashes` prefix (under `diag_settings_hash`; when several share that prefix, as every worker conversation's opening does, the one whose next message is most alike by the words they share, relative to both lengths), how many messages matched, the index and role of the first differing message, short normalized and scrubbed snippets of that message on both sides, and a signature (the snippets with digit runs collapsed) for grouping. `analyze_replay.py` groups replay lines into conversations and reports, per conversation and overall, the requests and prompt tokens served before the first miss, the first-miss causes by role and signature ranked by tokens lost, and, with `--verdicts`, whether each conversation's run verdict matched the recording run's.

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
| `REPLAY_JOB_PR`                   | empty                                 | The pull request number, stamped on every line          |
| `REPLAY_MODE`                     | `shadow`                              | `shadow`, `record` or `replay`                          |
| `REPLAY_CACHE_DIR`                | `/data/replay-cache`                  | Saved answers: written by record, read by replay        |
| `REPLAY_ALLOW_SERVE`              | empty                                 | Must be `yes` for replay mode to start                  |
| `REPLAY_JOB_PROJECT`              | empty                                 | The eval project, stamped on every line                 |
| `REPLAY_JOB_BUILD`                | empty                                 | The Prow build ID, stamped on every line                |

The image is built from the repository root (`docker build -f hack/replay-proxy/Dockerfile .`), because it copies the scrubbers from `agents/`.

## Tests

The tests are standard-library `unittest` and need no cluster: `make test-python` runs them, or run them from this directory with `python3 -m unittest`. `test_replay_proxy.py` runs the proxy against a fake LiteLLM on localhost.
