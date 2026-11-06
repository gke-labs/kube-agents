# Shadow-mode replay proxy

The shadow-mode replay proxy is an eval fixture. It sits in front of LiteLLM on an eval install and records, for every model request, what an inference replay cache would have keyed on. It never serves a saved answer: every request goes to LiteLLM and every response comes back unchanged. It is part of stage 1 of the rollout in [`examples/inference-replay/eval-design.md`](../../examples/inference-replay/eval-design.md), which owns the normalization rules, the cache key and the reasons behind them: the proxy, the fingerprint log, the scrubbed request store and the hit-rate script. The join with run verdicts and the collision report are not built yet. An install never uses it; eval runs deploy it by default (see [In CI](#in-ci)).

## What it does

For each `/v1/chat/completions` request, after the response has gone back to the client, a background thread does the following. The handler only queues the request; if the queue is full it drops the analysis and logs an `analysis_dropped` line instead of waiting.

- `normalizer.py` normalizes the request and computes its fingerprint: the normalized request without `stream`, `user` and `metadata`, a checksum of the LiteLLM config that treats the Gemini Developer API and Vertex AI routes as one (what it keeps and drops: [What goes into the cache key](../../examples/inference-replay/eval-design.md#what-goes-into-the-cache-key)), the LiteLLM image, and `NORMALIZER_VERSION` (a hash of `normalizer.py`).
- `replay_proxy.py` writes one JSON line to stdout (`"event": "replay_shadow_request"`): the fingerprint, per-message hashes and roles, prompt, cached and completion tokens, model latency, a hash of the answer (when the response carried no usage, `prompt_tokens` is estimated from the request size and `tokens_estimated` is true), LiteLLM's `x-litellm-model-group` and `x-litellm-attempted-fallbacks` headers, the reasoning and cache-write token counts (`reasoning_tokens` from `completion_tokens_details`, `cache_write_tokens` from `cache_creation_input_tokens`), the model behind the alias (`litellm_model_name`, from `x-litellm-model-name`), LiteLLM's retries inside the request (`litellm_attempted_retries`, from `x-litellm-attempted-retries`, an integer when it parses), LiteLLM's cost in USD (`litellm_cost_usd`: the usage block's `cost`, else `x-litellm-response-cost` on a non-streamed response only, since a stream sends that header as `0.0` before the cost is known; null otherwise), and why replay would not have saved the answer (`unstorable`: for example an upstream that broke off mid-answer, a response the proxy could not parse, or an answer whose scrubbing would mask a credential; reformatting or masking an email or IP doesn't count). The line holds no request or answer text, but its hashes are of the normalized request and answer and can be guessed against, so the log goes to the same private bucket as the stored text, never to Prow artifacts.
- `diagnosis.py` adds `diag_message_hashes` and `diag_settings_hash`, used only to find the closest earlier request for a miss: each message is normalized with the time the proxy first saw it, and IPs stay literal only if a CIDR appeared in it or an earlier message, so a later CIDR or a later request time doesn't make an earlier message look different. The settings get their own hash, normalized without any message's context. The proxy keeps one first-seen timestamp per distinct message for this, up to a million, oldest dropped first. `schema.py` names the line's fields for both the proxy and the analysis.
- `request_store.py` scrubs the request with the install's `AuditRedactor` and then the `worker_transcript.py` rules, the order `bench/kube_agents_bench/worker_trajectory.py` uses, and writes it under `REPLAY_STORE_DIR`: each distinct message once under `blobs/<sha256>.json`, and each request as a list of blob hashes under `requests/<request_id>.json`. If either scrubber cannot load or scrubbing a request fails, no text from it is written. If a write fails partway, some of its message blobs may be on disk but no request manifest is. Either way its line is still logged.

`/metrics` is answered by the proxy with its own counters and is never forwarded, because the proxy carries the `app: litellm` label (see "Where the proxy runs" in the design). `/replay/healthz` is its health check. Every other path goes to LiteLLM unchanged. The request body is forwarded byte for byte; the hop-by-hop headers and `Accept-Encoding` are dropped, so the usage block can be read. A request body over 64 MiB gets a 413 and is not forwarded. The proxy stops observing a response, streamed or not, after 8 MiB or at anything it can't parse; the bytes keep flowing to the client unchanged. The line then has `observe_failed: true` and the `unstorable` reason `observe_too_large` or `observe_failed`. When the response was cut short, `upstream_incomplete` or `client_disconnected` is the reason instead.

`analyze_shadow.py` reads the lines from a set of jobs and reports the token-weighted would-hit rate. Low counts nightly sightings. High adds earlier finished builds of the same pull request and, given `--merged` (pull request to merge time), another pull request's last build before it merged. Until run verdicts are joined, high is an upper bound: it counts those builds, and nightly answers once written, whatever their runs' verdicts. The report counts presubmit lines without a pull request number, which shows an unset `REPLAY_JOB_PR`. It also reports the share of hits first seen in another project, the tokens and model time hits would save, and the misses ranked by tokens lost with the closest earlier request by longest matching message prefix. Many requests share a prefix (every worker conversation opens the same way); given `--store` (the copied-out `REPLAY_STORE_DIR`), the closest is the one whose next message is most alike by the words they share, relative to both messages' length (among the 64 most recent distinct next messages with stored text), and otherwise the earliest. A candidate sharing no word with the miss's next message is no match. With `--store`, each miss says which (`closest_by`, with `closest_score` for a text pick and `unranked_reason` when it fell back) and the report counts the misses ranked by text, so a store copied from only some jobs shows up as fallbacks rather than as a ranked report; a `--store` without `requests/` and `blobs/` is refused. Its `simulated` row is high plus the requests that match with earlier assistant messages ignored, since replay would have served those; the module docstring says what that assumes. For multi-agent runs every estimate here is a floor: one agent's answers feed another's input and pick its actions, which shadow mode can't hold fixed, so the real hit rate comes from the design's replay lane (Rollout step 2), which runs real replay where it gates nothing.

Check the report's health counts after the first day: lines by job kind; untagged lines (an unset or unknown `REPLAY_JOB_KIND`, dropped from every estimate); error lines; lines without a fingerprint; `analysis_dropped`; unstorable lines and their share by reason; presubmit lines without a pull request number; lines without message hashes; the upstream-incomplete, observe-failed and observe-too-large counts; and the share of prompt tokens that are estimated. A high number in any of them means the setup is broken, not that replay would miss.

## Configuration

| Environment variable              | Default                               | Meaning                                                                            |
| --------------------------------- | ------------------------------------- | ---------------------------------------------------------------------------------- |
| `REPLAY_LISTEN_PORT`              | `8080`                                | Port the proxy listens on                                                          |
| `REPLAY_UPSTREAM_HOST`            | `inference-gateway-upstream`          | LiteLLM's host                                                                     |
| `REPLAY_UPSTREAM_PORT`            | `80`                                  | LiteLLM's port                                                                     |
| `REPLAY_UPSTREAM_TIMEOUT_SECONDS` | `1800`                                | Socket timeout for each read from LiteLLM                                          |
| `REPLAY_MODEL_BEHAVIOUR_CHECKSUM` | empty                                 | The chart's checksum of the LiteLLM config, Gemini API and Vertex AI routes as one |
| `REPLAY_LITELLM_IMAGE`            | empty                                 | The LiteLLM container image                                                        |
| `REPLAY_STORE_DIR`                | `/data/shadow`                        | Where scrubbed request text is written                                             |
| `REPLAY_REDACTOR_PATH`            | `/app/scrubbers/redactor.py`          | The install's `AuditRedactor`                                                      |
| `REPLAY_TRANSCRIPT_RULES_PATH`    | `/app/scrubbers/worker_transcript.py` | The shared transcript scrubbing rules                                              |
| `REPLAY_JOB_KIND`                 | empty                                 | `nightly` or `presubmit`, stamped on every line                                    |
| `REPLAY_JOB_PR`                   | empty                                 | The pull request number, stamped on every line                                     |
| `REPLAY_JOB_PROJECT`              | empty                                 | The eval project, stamped on every line                                            |
| `REPLAY_JOB_BUILD`                | empty                                 | The Prow build ID, stamped on every line                                           |
| `REPLAY_LOG_DIR`                  | empty                                 | Also write every line to segment files here                                        |
| `REPLAY_LOG_SEGMENT_SECONDS`      | `60`                                  | Close a segment this long after its first line                                     |
| `REPLAY_DATA_MAX_BYTES`           | `0` (no limit)                        | Stop the store and the segments at this many bytes                                 |

With `REPLAY_LOG_DIR` set, `spool.py` appends each line to `<pod>-<start ms>-<seq>.jsonl.open` and renames it to `.jsonl` once it is 8 MiB or `REPLAY_LOG_SEGMENT_SECONDS` old, so a closed segment never changes. At start it closes the segments a previous run of the container left open, cut to their last whole line. On TERM or INT the proxy stops serving, spends at most 10 seconds logging what is queued, and closes the open segment before it exits. stdout still gets every line. With `REPLAY_DATA_MAX_BYTES` set, the store and the segments stop writing once the files under `REPLAY_STORE_DIR` and `REPLAY_LOG_DIR` hold that many bytes, and start again when the collector has emptied files; the line still goes to stdout and the store reports `budget_spent`.

The image is built from the repository root (`docker build -f hack/replay-proxy/Dockerfile .`), because it copies the scrubbers from `agents/`.

## In CI

Every Prow presubmit and nightly eval puts the proxy in front of LiteLLM, unless the job sets `EVAL_REPLAY_SHADOW=false`. `hack/ci-deploy.sh` builds the image from this directory's `Dockerfile` (the `replay-shadow-proxy` step of `deploy/docker/cloudbuild-ci.yaml`) and installs it through the chart's `litellm.replayShadow` values: two proxy pods take the `inference-gateway` Service, and LiteLLM moves behind `inference-gateway-upstream`. Both carry `app: litellm`, the label the worker pods' egress policies admit, and differ by `kubeagents.x-k8s.io/inference-role` (`replay-shadow` or `litellm`). The comments on `litellm.replayShadow` in `charts/kube-agents/values.yaml` cover the rest. `hack/ci-eval-pr.sh`, which runs the presubmits and the nightlies, starts `collect_shadow.py` once it knows the host cluster and stops it from its EXIT trap. The collector copies closed segments and stored requests out of the proxy pods every 30 seconds, uploads them, then empties each uploaded file in the pod. At the end it makes one last pass, bounded to 60 seconds, that also takes the open segments. It prints counts and error classes to the `replay-shadow-collector.log` artifact and never fails the eval; the EXIT trap waits at most 75 seconds for its last pass, which runs while the trap's other captures do. The image has no `images.json` entry: each run builds it into its own project's registry and nothing publishes it, so there is nothing for `make mirror-images` to copy; `hack/check-image-inventory.sh` holds its Python base to the inventory.

It uploads to `kube-agents-replay-shadow` (`EVAL_REPLAY_SHADOW_BUCKET` points it elsewhere, no `gs://`); the runners' grants on that bucket are in [`docs/ci-pool-projects.md`](../../docs/ci-pool-projects.md). It needs the `JOB_TYPE`, `BUILD_ID` and, on a presubmit, `PULL_NUMBER` that Prow sets. A presubmit is filed as `presubmit`; a periodic or postsubmit as `nightly`. A run it can't serve goes without it, with a one-line note: a batch run, a release-candidate run (`RC_COMMIT_SHA`, which builds no images), a run outside Prow, or a Prow job missing one of those tags. Setting `EVAL_REPLAY_SHADOW=true` makes each of those an error instead. Outside Prow, set `EVAL_REPLAY_SHADOW_JOB_KIND` to `presubmit` or `nightly`, plus `BUILD_ID` and, for a presubmit, `PULL_NUMBER`.

Each run writes under `gs://<bucket>/replay-shadow/<nightly|presubmit>/<YYYY-MM-DD>/<BUILD_ID>/`, the date being the collector's start in UTC: `logs/*.jsonl` (the lines), `store/requests/*.json` and `store/blobs/*.json` (the scrubbed text), and `job-<ms>.json` (kind, project, build and pull request, written even when the run logged nothing). Every name is written once, so a merged copy of many runs loses nothing:

```bash
mkdir -p week/logs week/nightly week/presubmit
for kind in nightly presubmit; do
  for day in 2026-11-01 2026-11-02 2026-11-03 2026-11-04 2026-11-05 2026-11-06 2026-11-07; do
    gcloud storage cp -r "gs://<bucket>/replay-shadow/${kind}/${day}" "week/${kind}" || true
  done
done
find week/nightly week/presubmit -path '*/logs/*.jsonl' -exec cat {} + > week/logs/all.jsonl
for sub in requests blobs; do
  mkdir -p "week/store/${sub}"
  find week/nightly week/presubmit -path "*/store/${sub}/*.json" -exec cp -n {} "week/store/${sub}/" \;
done
python3 hack/replay-proxy/analyze_shadow.py week/logs/all.jsonl
```

`analyze_shadow.py` reads the lines only; a miss's stored text is `week/store/requests/<request_id>.json`, whose entries name files in `week/store/blobs/`.

Limitations, while the bucket and its grants are still to be made:

- presubmit and nightly write under separate top-level prefixes, so an IAM condition on the object name can confine each job's identity to its own (create and view, never delete). Within the presubmit prefix one pull request's run could still write under another's build;
- the image is built from the commit under test, not from `PULL_BASE_SHA` as the design's replay stage requires ("The proxy can't come from the pull request"). Shadow mode changes no verdict, but a pull request can change what its own run records.

## Baseline report

`baseline_report.py` reports what eval inference costs today, from the same lines: requests, prompt tokens split cached and uncached, completion tokens, the token-weighted cache hit rate, estimated cost, model time, non-200 statuses (429s above all), LiteLLM fallbacks, and, given the eval dashboard's `data.json`, model time against agent time, pass rates and flaky cases. It reports totals, each job kind, each build and, from the eval data, each case. The baseline is the report over the shadow data from before replay is switched on; every later report comes from the same script, so `--compare` measures the change the same way.

It takes segment files, job manifests or directories, searched for `*.jsonl` and `*.jsonl.open` segments and `job-<ms>.json` manifests. A file's job kind is the kind folder it was filed under, and only at the collector's layout: `<kind>/<YYYY-MM-DD>/<build>/logs/<segment>.jsonl` or `<kind>/<YYYY-MM-DD>/<build>/job-<ms>.json`, with `<build>` in a build ID's shape and no other directory anywhere in the file's path named `nightly`, `nightly-untrusted` or `presubmit`. Everything below a bucket kind prefix is the uploader's choice, and on a presubmit the uploader runs the pull request's code, so a `nightly/` folder planted under `presubmit/` makes a file nothing, never a nightly. A line's own `job.kind` tag never decides either, because the proxy that writes it is built from the commit under test; a line whose tag names another kind is dropped and counted as `kind_mismatch`, and a manifest likewise under `manifest_kind_mismatches`. A file found under a folder you pass but off that layout is skipped and counted in `off_layout_files`, with a note for a folder that holds nothing at the layout, so an object a presubmit plants under its own prefix cannot stop the report. A path that doesn't exist, or a named file that is neither a `.jsonl` segment nor a `job-<ms>.json` manifest, is always an error; a named file off the layout, or a run whose files are all off it, is an error unless `--kind` names the kind of every input, which also skips the layout check. A run that finds no files at all reports nothing. `nightly-untrusted`, a scheduled run that did not upload as the baseline recorder, is a scope of its own beside `nightly` and `presubmit`. A build named in a manifest with no lines is listed under data quality. Tokens and cost count the lines answered with 200, because a refused request bills nothing; model time counts every line with a latency. A line whose analysis failed or was dropped still counts as a request and by status.

`cost_usd` takes LiteLLM's own cost for a line (`litellm_cost_usd`) when it has one, and otherwise an estimate from `model-prices.json`: a dated table of list prices per 1M tokens (input, cached input, output, and a long-context tier above a prompt size). The report gives both totals and how many lines each covered (`litellm_cost_usd`, `table_cost_usd`, `cost_from_table_requests`), and `table_to_litellm_cost_ratio` compares them over the lines that have both, which checks the table. The table is looked up by the deployment's model (`litellm_model_name`, with and then without its provider prefix such as `vertex_ai/`), then by the model group and the request's model; `--price-as <group>=<model>` is only the fallback, for lines written before the model name was logged (the group on an eval install is the `model-default` alias). A line with neither a LiteLLM cost nor a price is reported as unpriced with its count and left out, never counted as zero. A line whose tokens were estimated (the response carried no usage, so `prompt_tokens` is the request's size and there is no completion count) is never priced from the table: it counts only through a LiteLLM cost, and otherwise goes to `estimated_unpriced_requests` and out of `cost_usd`. Such lines are a minority, and a streamed answer that carries its usage carries LiteLLM's cost too, but `--compare` notes them on either side. The report prints the table's `as_of` date, whether its prices were `confirmed` against the provider's price page on that date, its `source` and its hash; the checked-in table is not confirmed. The table does not price cache writes.

The eval join matches a line's `job.build` to a dashboard run's `build_id`. Lines carry no case, so tokens, cost and model time stop at the build; the case table comes from the eval data's repetitions. Model time includes every agent on the install and every request a repetition never waited on, so its ratio to agent time can pass 100%.

To freeze a baseline, download the window and keep the report and its inputs together:

```bash
kinds="nightly nightly-untrusted presubmit"
: > objects.txt
for kind in ${kinds}; do
  gcloud storage ls "gs://<bucket>/replay-shadow/${kind}/**" >> objects.txt 2>/dev/null || true
done
if grep -E '^gs://[^/]+/(/|\.\.?(/|$)|.*(//|/\.\.?(/|$)))' objects.txt; then
  echo "Refusing to download: the object keys above have an empty, '.' or '..' segment." >&2
else
  # Each kind's segments and manifests, keeping the <kind>/<day>/<build>/ layout; the scrubbed text is not needed.
  for kind in ${kinds}; do
    gcloud storage rsync -r --exclude='.*/store/.*' "gs://<bucket>/replay-shadow/${kind}" "shadow/${kind}"
  done
  gcloud storage cp gs://kube-agents-dashboards/evals/data.json data.json
  python3 baseline_report.py shadow/ --eval-data data.json --price-as model-default=<model> \
    --since 2026-11-05 --until 2026-11-18 --json > baseline.json
fi
```

The listing comes first because the folders' trust depends on the downloader: IAM matches a key's prefix as text, so `replay-shadow/presubmit/x/../../nightly/...` is a presubmit's object, and a tool that resolved the `..` would write it into `shadow/nightly`; only the downloader's own check keeps such a key out of the wrong folder.

`--since` and `--until` take epoch seconds (a plain run of at least nine digits, up to the end of year 9999), a `YYYY-MM-DD` date (`--until` includes that day) or an ISO time that starts with one, and refuse anything else. They keep whole builds by their first request, and the manifests of the builds they keep; a line without a build id is kept or left out by its own time, and counts in every sum but in no per-build figure. A line's value outside its bound (a time past year 9999, a latency over a day, a count over a trillion, a cost over a million dollars) is a bad field value, not a number. Read the data-quality block before freezing: skipped lines, bad field values, estimated token lines and unpriced requests each mean a number above is short. Later, download the new window the same way, into its own directory with the same `<kind>/` folders, and run the same command over it with `--compare baseline.json` (add `--json` for the deltas as JSON), or compare two saved reports with `--report current.json --compare baseline.json`. The deltas are current minus baseline, absolutely (percentage points for rates) and as a percent of the baseline; a note says when the price tables, `--price-as` or the report schema differ, or when either side's cost is partial. Compare per-build metrics: two windows rarely hold the same number of builds.

The metrics, as `baseline_report.py` defines them:

<!-- BEGIN GENERATED: replay-baseline-metrics -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

| Metric | Unit | Headline | Definition |
| ------ | ---- | -------- | ---------- |
| `builds` | count | yes | Distinct (job kind, build) pairs with at least one request line. Lines without a build id count in every sum but are no build, so they stay out of this and every per-build figure. |
| `requests` | count | yes | Request lines, any status, including lines whose analysis failed or was dropped (each was a model request). |
| `requests_per_build` | count | yes | requests from lines with a build id / builds. |
| `ok_requests` | count |  | Request lines with status 200. |
| `prompt_tokens` | tokens | yes | Sum of prompt_tokens over status-200 lines, estimated ones included (tokens_estimated: the response carried no usage and the proxy estimated from the request size). |
| `cached_prompt_tokens` | tokens |  | Sum of cached_tokens over status-200 lines (missing counts as 0). |
| `uncached_prompt_tokens` | tokens | yes | Sum over status-200 lines of max(prompt_tokens - cached_tokens, 0). |
| `completion_tokens` | tokens | yes | Sum of completion_tokens over status-200 lines; reasoning tokens are part of it. |
| `reasoning_tokens` | tokens | yes | Sum of reasoning_tokens (usage.completion_tokens_details.reasoning_tokens) over status-200 lines. |
| `reasoning_token_share` | fraction |  | reasoning_tokens / completion_tokens, both over the status-200 lines that log reasoning_tokens (a line written before the field existed would read as no reasoning). |
| `cache_write_tokens` | tokens |  | Sum of cache_write_tokens (usage.cache_creation_input_tokens, Anthropic-style cache writes) over status-200 lines. |
| `prompt_tokens_per_build` | tokens | yes | prompt_tokens from lines with a build id / builds. |
| `cache_hit_rate` | fraction | yes | cached / prompt tokens, token-weighted, over status-200 lines whose usage was reported (not estimated): an estimated line has no cached count and would read as a miss. |
| `estimated_prompt_token_share` | fraction |  | Prompt tokens on status-200 lines with tokens_estimated, over prompt_tokens. |
| `cost_usd` | usd | yes | Sum over status-200 lines of LiteLLM's own cost (litellm_cost_usd) where the line has it, else the price-table estimate (see table_cost_usd); cost_from_table_requests says how many lines took the table. Lines with neither are left out, and so are lines with estimated tokens and no LiteLLM cost: a partial figure when unpriced_requests or estimated_unpriced_requests &gt; 0. |
| `cost_per_build_usd` | usd | yes | cost_usd from lines with a build id / builds. |
| `litellm_cost_usd` | usd |  | Sum of litellm_cost_usd over the status-200 lines that carry it. |
| `litellm_costed_requests` | count |  | Status-200 lines that carry litellm_cost_usd. |
| `table_cost_usd` | usd |  | Sum over status-200 lines with reported (not estimated) tokens that the price table prices (with or without a LiteLLM cost) of (uncached x input + cached x cached_input + completion x output) per 1M tokens, at the long-context price when the line's prompt_tokens exceed the table's above_prompt_tokens. Cache writes are not priced. |
| `table_priced_requests` | count |  | Status-200 lines with reported tokens that the price table prices. |
| `cost_from_table_requests` | count |  | Status-200 lines whose cost_usd came from the price table (no LiteLLM cost). |
| `table_to_litellm_cost_ratio` | fraction |  | The table estimate over LiteLLM's cost, both over the status-200 lines that have both: the cross-check of the price table (1.0 is agreement). |
| `unpriced_requests` | count |  | Status-200 lines with reported tokens but neither a LiteLLM cost nor a price-table price. |
| `estimated_token_requests` | count |  | Status-200 lines with tokens_estimated. |
| `estimated_unpriced_requests` | count |  | Status-200 lines with tokens_estimated and no LiteLLM cost: an estimate of the prompt size and no completion count cannot be priced, so they are left out of cost_usd and table_cost_usd. |
| `model_time_s` | seconds | yes | Sum of latency_ms over lines that carry one, any status, in seconds. Concurrent requests add up, so this can exceed wall clock. |
| `model_time_per_build_s` | seconds | yes | model_time_s from lines with a build id / builds. |
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
| `harness_tokens_total` | tokens |  | Sum of tokens.total over the repetitions of joined builds that carry it (the harness's own count; a cross-check on prompt_tokens + completion_tokens), leaving out repetitions whose tokens.partial is true. |
| `harness_partial_reps` | count |  | Repetitions of joined builds left out of harness_tokens_total because their tokens.partial is true. |
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

The tests are standard-library `unittest` and need no cluster: `make test-python` runs them, or run them from this directory with `python3 -m unittest`. `test_replay_proxy.py` runs the proxy against a fake LiteLLM on localhost; `test_collect_shadow.py` runs the collector against a fake cluster and bucket, and as a process with fake `kubectl` and `gcloud`.
