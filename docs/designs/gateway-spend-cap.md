# A spend ceiling at the gateway

**Status:** first slice implemented 2026-11-09 (the knob, chart, installer, Terraform, docs). Issue #5262, from the PoC report tracked in #2597. The `status.usage` reading and the refusal's surface on the CR and in chat are follow-ups, below.

## 1. The ask

A customer running the install unattended asked for a per-install ceiling on model spend, with the remaining budget exported. Their LiteLLM budget metric read `inf`, the chart had no budget key, and the one thing that ever stopped spend was the provider refusing once the account's allowance ran out.

## 2. What was verified

Read from `main` and LiteLLM v1.106.0 (the pinned image) on 2026-11-09.

| Fact                                                                                                                                                                                                                                                                                                                                                                                     | Where                                                                                                           |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| The chart runs LiteLLM with no database, no master key, two replicas, no Redis. `router_settings` carries `fallbacks`, `timeout`, `num_retries`, `retry_after` and the Claude cache breakpoints, nothing else; the default render adds no key the kustomize base lacks.                                                                                                                  | `templates/litellm.yaml`, `_helpers.tpl` (`kube-agents.litellmConfig`), `values.yaml`                           |
| `inf` is what the Prometheus callback sets on `litellm_remaining_api_key_budget_metric` and `litellm_remaining_team_budget_metric` when the key has no `max_budget` (`_safe_get_remaining_budget`). With no key there never is one.                                                                                                                                                      | `litellm/integrations/prometheus.py`                                                                            |
| `litellm_settings.max_budget` without a database checks `litellm._current_cost` in the client wrapper, and the cost is added only when `self.stream is False`, from the completion text alone. Nothing exports it. The agent streams every turn.                                                                                                                                         | `litellm/utils.py`, `litellm/litellm_core_utils/litellm_logging.py`                                             |
| The router budget limiter takes `router_settings.provider_budget_config.<provider>: {budget_limit, time_period}`, adds each response's `response_cost` from the standard logging payload to `provider_spend:<provider>:<period>` in the router's `DualCache`, filters the provider's deployments out at `current_spend >= max_budget`, and sets the provider gauge on every filter pass. | `litellm/router_strategy/budget_limiter.py` (`async_log_success_event`, `_filter_out_deployments_above_budget`) |
| The window starts at the first spend and the keys expire `time_period` later; a new window starts at the next spend. The `DualCache` is in-memory with an optional Redis the router gets from its own cache settings; the sync task returns at once without one.                                                                                                                         | `_increment_spend_for_key`, `_sync_in_memory_spend_with_redis`, `litellm/router.py` (`self.cache`)              |
| With every deployment over budget the filter raises `ValueError("No deployments available - crossed budget: Exceeded budget for provider <p>: <spend> >= <limit>")`.                                                                                                                                                                                                                     | `async_filter_deployments`, `litellm/types/router.py` (`RouterErrors`)                                          |
| `budget_limit` of `0` is read as no limit (`if config.max_budget and ...`).                                                                                                                                                                                                                                                                                                              | `_filter_out_deployments_above_budget`                                                                          |
| The provider key is `custom_llm_provider` as `get_llm_provider` resolves it from the deployment's `litellm_params.model`, the prefix before the first `/`; the chart writes one prefix on every alias (`hosted_vllm` for `openai_compatible`).                                                                                                                                           | `_get_llm_provider_for_deployment`, `_helpers.tpl`                                                              |
| The gauge is `litellm_provider_remaining_budget_metric{api_provider}`, `budget_limit - spend`; the chart's `PodMonitoring` collects each gateway pod's `/metrics`.                                                                                                                                                                                                                       | `prometheus.py` (`track_provider_remaining_budget`), `templates/litellm.yaml`                                   |
| A model the cost map does not price has `response_cost` 0; the bundled map does not carry every model an install may run.                                                                                                                                                                                                                                                                | `litellm/model_prices_and_context_window_backup.json`                                                           |
| The usage-counters poller scrapes operator-rendered pods only, through NetworkPolicy rules the operator renders; #4722 plans the LiteLLM `/metrics` target for the token and spend counters. The LiteLLM pods and their policy are the chart's.                                                                                                                                          | `usage_counters_poller.go`, `usage-counters-producer.md`, #4722                                                 |
| The replay shadow's `REPLAY_MODEL_BEHAVIOUR_CHECKSUM` strips the delivery settings (`timeout`, `num_retries`, `retry_after`) from `router_settings` before hashing.                                                                                                                                                                                                                      | `_helpers.tpl` (`kube-agents.replayModelBehaviourChecksum`)                                                     |

## 3. Decisions

### 3.1 The mechanism: LiteLLM's provider budget

The router budget limiter is the one LiteLLM budget that works with no database, counts streamed calls, refuses with a clear error, resets on a window and exports a remaining-budget gauge. The proxy-wide `max_budget` fails the first two. Per-key and per-team budgets need the database and virtual keys, which the install does not run; adding Postgres to the gateway for a counter is out of proportion.

### 3.2 The knob: `litellm.budget.usd` and `litellm.budget.period`, off by default

The chart renders `router_settings.provider_budget_config.<provider>` only while `usd` is above 0, so an install without a ceiling renders the ConfigMap and its rollout checksum byte for byte as before (the `maxTokens` and `fallback` rule). `period` is a LiteLLM duration (`^[1-9][0-9]*(s|m|h|d|w|mo)$`), default `1d`, read only beside an amount. The schema takes `usd` as a number at or above 0; a string is refused, which is what `--set litellm.budget.usd=0.5` would hand it (Helm parses only integers from `--set`), so the docs say values file or `--set-json`.

The installer path follows `LITELLM_REDACTION_RULES`: two `install.env` keys, `LITELLM_BUDGET_USD` and `LITELLM_BUDGET_PERIOD`, with no flag, validated in the tfvars generator for the `MODEL_MAX_TOKENS` reason (every front door regenerates from the file), written to `terraform.tfvars` as `litellm_budget = { usd = <n>, period = "<p>" }` only while on, so the goldens and every existing file stay as they were. The Terraform example's `litellm_budget` carries the same two checks and merges `budget` into the chart values only above 0.

### 3.3 Per install: the ceiling split across replicas

The counter is per gateway process and the chart runs two replicas with no Redis. Three options were weighed:

- Render the amount per replica: the install can spend `usd × replicaCount`. A ceiling that admits twice what it says is not a ceiling.
- Refuse a ceiling above one replica: honest, but it trades availability (the PDB, the rolling update) for the cap.
- Split the amount evenly across `replicaCount`: each replica refuses on its share, the Service balances traffic across them, and the install never spends over `usd`. Uneven traffic makes one replica refuse before the install has spent the whole amount: under-spend is the failure mode, never over-spend.

The third is what ships. The share is rendered with four decimals and always a dot (`printf "%.4f"`), because LiteLLM loads the config with PyYAML, whose YAML 1.1 float needs one; a share that rounds to `0.0000` fails the render, because LiteLLM reads it as no limit. The gauge is therefore per replica, and `sum` over the gateway pods is the install's remaining budget.

Two limits stay, documented on the site page: a restarted or rolled pod starts its count over, so the ceiling bounds a runaway between restarts rather than keeping a ledger; and a model the cost map does not price never reaches it.

### 3.4 What the replay checksum sees

The ceiling decides whether a call goes, not what the model answers, so `provider_budget_config` joins the delivery settings the replay checksum strips. The pods still roll on the change, since the rollout checksum covers the whole config.

### 3.5 What is exported, and where it is read

`litellm_provider_remaining_budget_metric{api_provider}` on each gateway pod, collected by the `PodMonitoring` the chart already renders, is the customer's "remaining budget exported" for this slice. The operator does not read it yet: the readings slot reaches operator-rendered pods through policies the operator renders, and a LiteLLM target is #4722's to add for the spend counters. When it exists, the remaining budget is one more reading on that target (micro-dollars, beside `spendMicrosUSDTotal`), with the gauge row and the summary line that come with a reading; it is not a target of its own.

## 4. What is left

- **The reading.** `status.usage` and a `kubeagents_platformagent_*` gauge for the remaining budget, on #4722's LiteLLM target.
- **The surface when the ceiling is hit.** Today the gateway's refusal reaches only the caller, and the turn fails like any gateway error. A `Degraded` or `Paused` reason on the CR needs the operator to know the window is exhausted (the reading above, read as `0` with a ceiling configured, is the natural signal), and a chat line needs the gateway or the agent to recognise the error, which is a prompt or runtime-path change under the eval loop. Both are a mechanism of their own and are filed as follow-ups when this slice merges.
- **One number across replicas and restarts.** A Redis the router shares its cache with turns the split shares into one counter that survives a rollout. Until then the split is the ceiling.
