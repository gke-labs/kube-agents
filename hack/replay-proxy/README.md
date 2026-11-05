# Shadow-mode replay proxy

The shadow-mode replay proxy is an eval fixture. It sits in front of LiteLLM on an eval install and records, for every model request, what an inference replay cache would have keyed on. It never serves a saved answer: every request goes to LiteLLM and every response comes back unchanged. It is part of stage 1 of the rollout in [`examples/inference-replay/eval-design.md`](../../examples/inference-replay/eval-design.md), which owns the normalization rules, the cache key and the reasons behind them: the proxy, the fingerprint log, the scrubbed request store and the hit-rate script. The join with run verdicts and the collision report are not built yet. An install never uses it, and nothing deploys it yet.

## What it does

For each `/v1/chat/completions` request, after the response has gone back to the client, a background thread does the following. The handler only queues the request; if the queue is full it drops the analysis and logs an `analysis_dropped` line instead of waiting.

- `normalizer.py` normalizes the request and computes its fingerprint: the normalized request without `stream`, `user` and `metadata`, the LiteLLM config checksum, the LiteLLM image, and `NORMALIZER_VERSION` (a hash of `normalizer.py`).
- `replay_proxy.py` writes one JSON line to stdout (`"event": "replay_shadow_request"`): the fingerprint, per-message hashes and roles, prompt, cached and completion tokens, model latency, a hash of the answer (when the response carried no usage, `prompt_tokens` is estimated from the request size and `tokens_estimated` is true), LiteLLM's `x-litellm-model-group` and `x-litellm-attempted-fallbacks` headers, the reasoning and cache-write token counts (`reasoning_tokens` from `completion_tokens_details`, `cache_write_tokens` from `cache_creation_input_tokens`), the model behind the alias (`litellm_model_name`, from `x-litellm-model-name`), LiteLLM's retries inside the request (`litellm_attempted_retries`, from `x-litellm-attempted-retries`, an integer when it parses), LiteLLM's cost in USD (`litellm_cost_usd`: the usage block's `cost`, else `x-litellm-response-cost` on a non-streamed response only, since a stream sends that header as `0.0` before the cost is known; null otherwise), and why replay would not have saved the answer (`unstorable`: for example an upstream that broke off mid-answer, a response the proxy could not parse, or an answer whose scrubbing would mask a credential; reformatting or masking an email or IP doesn't count). The line holds no request or answer text, but its hashes are of the normalized request and answer and can be guessed against, so the log goes to the same private bucket as the stored text, never to Prow artifacts.
- `diagnosis.py` adds `diag_message_hashes` and `diag_settings_hash`, used only to find the closest earlier request for a miss: each message is normalized with the time the proxy first saw it, and IPs stay literal only if a CIDR appeared in it or an earlier message, so a later CIDR or a later request time doesn't make an earlier message look different. The settings get their own hash, normalized without any message's context. The proxy keeps one first-seen timestamp per distinct message for this, up to a million, oldest dropped first. `schema.py` names the line's fields for both the proxy and the analysis.
- `request_store.py` scrubs the request with the install's `AuditRedactor` and then the `worker_transcript.py` rules, the order `bench/kube_agents_bench/worker_trajectory.py` uses, and writes it under `REPLAY_STORE_DIR`: each distinct message once under `blobs/<sha256>.json`, and each request as a list of blob hashes under `requests/<request_id>.json`. If either scrubber cannot load or scrubbing a request fails, no text from it is written. If a write fails partway, some of its message blobs may be on disk but no request manifest is. Either way its line is still logged.

`/metrics` is answered by the proxy with its own counters and is never forwarded, because the proxy carries the `app: litellm` label (see "Where the proxy runs" in the design). `/replay/healthz` is its health check. Every other path goes to LiteLLM unchanged. The request body is forwarded byte for byte; the hop-by-hop headers and `Accept-Encoding` are dropped, so the usage block can be read. A request body over 64 MiB gets a 413 and is not forwarded. The proxy stops observing a response, streamed or not, after 8 MiB or at anything it can't parse; the bytes keep flowing to the client unchanged. The line then has `observe_failed: true` and the `unstorable` reason `observe_too_large` or `observe_failed`. When the response was cut short, `upstream_incomplete` or `client_disconnected` is the reason instead.

`analyze_shadow.py` reads the lines from a set of jobs and reports the token-weighted would-hit rate. Low counts nightly sightings. High adds earlier finished builds of the same pull request and, given `--merged` (pull request to merge time), another pull request's last build before it merged. Until run verdicts are joined, high is an upper bound: it counts those builds, and nightly answers once written, whatever their runs' verdicts. The report counts presubmit lines without a pull request number, which shows an unset `REPLAY_JOB_PR`. It also reports the share of hits first seen in another project, the tokens and model time hits would save, and the misses ranked by tokens lost with the closest earlier request by longest matching message prefix. Its `simulated` row is high plus the requests that match with earlier assistant messages ignored, since replay would have served those; the module docstring says what that assumes. For multi-agent runs every estimate here is a floor: one agent's answers feed another's input and pick its actions, which shadow mode can't hold fixed, so the real hit rate comes from the design's replay lane (Rollout step 2), which runs real replay where it gates nothing.

Check the report's health counts after the first day: lines by job kind; untagged lines (an unset or unknown `REPLAY_JOB_KIND`, dropped from every estimate); error lines; lines without a fingerprint; `analysis_dropped`; unstorable lines and their share by reason; presubmit lines without a pull request number; lines without message hashes; the upstream-incomplete, observe-failed and observe-too-large counts; and the share of prompt tokens that are estimated. A high number in any of them means the setup is broken, not that replay would miss.

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
| `REPLAY_JOB_PROJECT`              | empty                                 | The eval project, stamped on every line                 |
| `REPLAY_JOB_BUILD`                | empty                                 | The Prow build ID, stamped on every line                |

The image is built from the repository root (`docker build -f hack/replay-proxy/Dockerfile .`), because it copies the scrubbers from `agents/`.

## Baseline report

`baseline_report.py` reports what eval inference costs today, from the same lines: requests, prompt tokens split cached and uncached, completion tokens, the token-weighted cache hit rate, estimated cost, model time, non-200 statuses (429s above all), LiteLLM fallbacks, and, given the eval dashboard's `data.json`, model time against agent time, pass rates and flaky cases. It reports totals, each job kind, each build and, from the eval data, each case. The baseline is the report over the shadow data from before replay is switched on; every later report comes from the same script, so `--compare` measures the change the same way.

It takes JSONL files or directories, searched for `*.jsonl` and `*.jsonl.open` segments and `job-<ms>.json` manifests, so a bucket prefix downloaded as it is works. A build named in a manifest with no lines is listed under data quality. Tokens and cost count the lines answered with 200, because a refused request bills nothing; model time counts every line with a latency. A line whose analysis failed or was dropped still counts as a request and by status.

`cost_usd` takes LiteLLM's own cost for a line (`litellm_cost_usd`) when it has one, and otherwise an estimate from `model-prices.json`: a dated table of list prices per 1M tokens (input, cached input, output, and a long-context tier above a prompt size). The report gives both totals and how many lines each covered (`litellm_cost_usd`, `table_cost_usd`, `cost_from_table_requests`), and `table_to_litellm_cost_ratio` compares them over the lines that have both, which checks the table. The table is looked up by the deployment's model (`litellm_model_name`, with and then without its provider prefix such as `vertex_ai/`), then by the model group and the request's model; `--price-as <group>=<model>` is only the fallback, for lines written before the model name was logged (the group on an eval install is the `model-default` alias). A line with neither a LiteLLM cost nor a price is reported as unpriced with its count and left out, never counted as zero; the report prints the table's `as_of` date and hash. The table does not price cache writes.

The eval join matches a line's `job.build` to a dashboard run's `build_id`. Lines carry no case, so tokens, cost and model time stop at the build; the case table comes from the eval data's repetitions. Model time includes every agent on the install and every request a repetition never waited on, so its ratio to agent time can pass 100%.

To freeze a baseline, download the window and keep the report and its inputs together:

```bash
gcloud storage cp -r "gs://<bucket>/replay-shadow" shadow/
gcloud storage cp gs://kube-agents-dashboards/evals/data.json data.json
python3 baseline_report.py shadow/ --eval-data data.json --price-as model-default=<model> \
  --since 2026-11-05 --until 2026-11-18 --json > baseline.json
```

`--since` and `--until` take epoch seconds, a date (`--until` includes that day) or an ISO time, and keep whole builds by their first request. Read the data-quality block before freezing: skipped lines, bad field values, estimated token lines and unpriced requests each mean a number above is short. Later, run the same command over the new window with `--compare baseline.json` (add `--json` for the deltas as JSON), or compare two saved reports with `--report current.json --compare baseline.json`. The deltas are current minus baseline, absolutely (percentage points for rates) and as a percent of the baseline; a note says when the price tables, `--price-as` or the report schema differ, or when either side's cost is partial. Compare per-build metrics: two windows rarely hold the same number of builds.

The metrics, as `baseline_report.py` defines them:

<!-- BEGIN GENERATED: replay-baseline-metrics -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

| Metric | Unit | Headline | Definition |
| ------ | ---- | -------- | ---------- |
| `builds` | count | yes | Distinct (job kind, build) pairs with at least one request line. |
| `requests` | count | yes | Request lines, any status, including lines whose analysis failed or was dropped (each was a model request). |
| `requests_per_build` | count | yes | requests / builds. |
| `ok_requests` | count |  | Request lines with status 200. |
| `prompt_tokens` | tokens | yes | Sum of prompt_tokens over status-200 lines, estimated ones included (tokens_estimated: the response carried no usage and the proxy estimated from the request size). |
| `cached_prompt_tokens` | tokens |  | Sum of cached_tokens over status-200 lines (missing counts as 0). |
| `uncached_prompt_tokens` | tokens | yes | Sum over status-200 lines of max(prompt_tokens - cached_tokens, 0). |
| `completion_tokens` | tokens | yes | Sum of completion_tokens over status-200 lines; reasoning tokens are part of it. |
| `reasoning_tokens` | tokens | yes | Sum of reasoning_tokens (usage.completion_tokens_details.reasoning_tokens) over status-200 lines. |
| `reasoning_token_share` | fraction |  | reasoning_tokens / completion_tokens, both over the status-200 lines that log reasoning_tokens (a line written before the field existed would read as no reasoning). |
| `cache_write_tokens` | tokens |  | Sum of cache_write_tokens (usage.cache_creation_input_tokens, Anthropic-style cache writes) over status-200 lines. |
| `prompt_tokens_per_build` | tokens | yes | prompt_tokens / builds. |
| `cache_hit_rate` | fraction | yes | cached / prompt tokens, token-weighted, over status-200 lines whose usage was reported (not estimated): an estimated line has no cached count and would read as a miss. |
| `estimated_prompt_token_share` | fraction |  | Prompt tokens on status-200 lines with tokens_estimated, over prompt_tokens. |
| `cost_usd` | usd | yes | Sum over status-200 lines of LiteLLM's own cost (litellm_cost_usd) where the line has it, else the price-table estimate (see table_cost_usd); cost_from_table_requests says how many lines took the table. Lines with neither are left out: a partial figure when unpriced_requests &gt; 0. |
| `cost_per_build_usd` | usd | yes | cost_usd / builds. |
| `litellm_cost_usd` | usd |  | Sum of litellm_cost_usd over the status-200 lines that carry it. |
| `litellm_costed_requests` | count |  | Status-200 lines that carry litellm_cost_usd. |
| `table_cost_usd` | usd |  | Sum over status-200 lines the price table prices (with or without a LiteLLM cost) of (uncached x input + cached x cached_input + completion x output) per 1M tokens, at the long-context price when the line's prompt_tokens exceed the table's above_prompt_tokens. Cache writes are not priced. |
| `table_priced_requests` | count |  | Status-200 lines the price table prices. |
| `cost_from_table_requests` | count |  | Status-200 lines whose cost_usd came from the price table (no LiteLLM cost). |
| `table_to_litellm_cost_ratio` | fraction |  | The table estimate over LiteLLM's cost, both over the status-200 lines that have both: the cross-check of the price table (1.0 is agreement). |
| `unpriced_requests` | count |  | Status-200 lines with neither a LiteLLM cost nor a price-table price. |
| `model_time_s` | seconds | yes | Sum of latency_ms over lines that carry one, any status, in seconds. Concurrent requests add up, so this can exceed wall clock. |
| `model_time_per_build_s` | seconds | yes | model_time_s / builds. |
| `latency_p50_ms` | ms | yes | Median latency_ms (nearest rank) over lines that carry one. |
| `latency_p95_ms` | ms | yes | 95th percentile latency_ms (nearest rank) over lines that carry one. |
| `non_200_requests` | count | yes | Lines whose status is not 200 (status_counts breaks them down). |
| `rate_limited_requests` | count | yes | Lines with status 429. |
| `rate_limited_rate` | fraction | yes | rate_limited_requests / requests. |
| `retried_requests` | count | yes | Lines, any status, whose litellm_attempted_retries (x-litellm-attempted-retries) is above 0: retries LiteLLM made inside one request, apart from the client's own retries, which are lines of their own. |
| `litellm_retries` | count | yes | Sum of litellm_attempted_retries over lines that carry it. |
| `fallback_attempted_requests` | count | yes | Lines whose x-litellm-attempted-fallbacks header was present and not 0. |
| `upstream_incomplete_requests` | count |  | Lines with upstream_incomplete true (the answer broke off). |
| `eval_builds_joined` | count |  | Builds in scope whose build id is a run in the eval data. |
| `agent_time_s` | seconds |  | Sum of agent_s over the repetitions of joined builds that carry it. |
| `model_time_share_of_agent_time` | fraction | yes | model_time_s over agent_time_s, both over the joined builds that have repetitions with agent_s. Model time outside a repetition (background agents, retries the harness never sees) counts too, so it is a ratio, not a share bounded by 1. |
| `harness_tokens_total` | tokens |  | Sum of tokens.total over the repetitions of joined builds that carry it (the harness's own count; a cross-check on prompt_tokens + completion_tokens). |
| `graded_reps` | count |  | Repetitions of joined builds graded pass or fail. |
| `passed_reps` | count |  | Repetitions graded pass. |
| `infra_reps` | count |  | Repetitions lost to infrastructure (infra), outside pass_rate. |
| `pass_rate` | fraction | yes | passed_reps / graded_reps. |
| `cases` | count |  | Distinct case names among the joined builds' tasks. |
| `flaky_cases` | count | yes | Cases with at least one joined build whose graded repetitions include both a pass and a fail. |

| Case metric | Unit | Definition |
| ----------- | ---- | ---------- |
| `case_builds` | count | Joined builds that ran the case. |
| `case_graded_reps` | count | Its repetitions graded pass or fail. |
| `case_passed_reps` | count | Its repetitions graded pass. |
| `case_infra_reps` | count | Its repetitions lost to infrastructure. |
| `case_pass_rate` | fraction | case_passed_reps / case_graded_reps. |
| `case_flaky_builds` | count | Builds whose graded repetitions of the case include a pass and a fail. |
| `case_agent_s_p50` | seconds | Median agent_s over its repetitions that carry it. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: replay-baseline-metrics -->

## Tests

The tests are standard-library `unittest` and need no cluster: `make test-python` runs them, or run them from this directory with `python3 -m unittest`. `test_replay_proxy.py` runs the proxy against a fake LiteLLM on localhost.
