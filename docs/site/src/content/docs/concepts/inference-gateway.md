---
title: Inference gateway
description: LiteLLM for hosted models, vLLM for local models. Plus optional replay caching for demos.
sidebar:
  order: 8
---

The Platform Agent talks to an LLM through a **Completions API** proxy so provider choice is a config toggle. There are shipping options for both hosted and local models, plus a replay layer.

## Choosing a provider

| You want                                                           | Use                                                               | Why                                                                                                                                                                                                            |
| ------------------------------------------------------------------ | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fastest path with a hosted frontier model                          | **LiteLLM → Gemini** (default)                                    | One API key, no GPU node pool, no cluster egress beyond the LiteLLM pod.                                                                                                                                       |
| Provider redundancy or A/B                                         | **LiteLLM → Gemini + Anthropic + OpenAI**                         | LiteLLM handles the router config; agent config is unchanged.                                                                                                                                                  |
| Inference billed to your own GCP project                           | **LiteLLM → Vertex AI / Model Garden**                            | Workload Identity by default instead of an API key (or a SA key Secret); Gemini plus Model Garden publishers. See [below](#vertex-ai-and-model-garden).                                                        |
| Free local prototyping with a consumer subscription                | **LiteLLM → ChatGPT subscription** (OAuth device flow)            | See [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription).                                                                       |
| Model traffic kept off hosted model providers                      | **LiteLLM → your OpenAI-compatible server** (`openai_compatible`) | vLLM, TGI or an internal gateway, in the cluster or on your network. Google APIs stay required. See [below](#openai-compatible-endpoints-self-hosted-or-private).                                              |
| Model traffic through your SAP tenant, or another LiteLLM provider | **LiteLLM → SAP AI Core** (`sap`), or the provider's slug         | Credentials in a Secret you create, named with `--litellm-env-secret`; an OpenAI-compatible gateway behind OAuth2 client credentials uses the same Secret. See [below](#a-credentials-secret-for-the-gateway). |
| Deterministic demos / cheap tests                                  | **Any of the above + inference-replay proxy**                     | Caches responses in a PVC; replays on cache hit.                                                                                                                                                               |

## LiteLLM (hosted models)

[LiteLLM](https://litellm.ai) is an OpenAI-Completions-compatible proxy in front of every major model provider. The `kube-agents` Helm chart deploys it with the API key you provide (the dev copy is `make -C k8s-operator deploy-litellm`).

### What ships

- [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) — Gemini-only default. Uses `GEMINI_API_KEY`.
- [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription) — proxies to a personal ChatGPT subscription via OAuth device flow. Useful for demos where you don't want a per-token cost.

To switch providers, edit the LiteLLM `config.yaml` (mounted from a `ConfigMap`) and set the corresponding API key secret. The Platform Agent config doesn't change — it always talks to a Service named `inference-gateway`.

### Setting the default model

The agent always requests a single logical model, `model-default`. LiteLLM maps that alias to a real provider model in its [`config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/litellm/base/config.yaml):

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: ${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}
```

Two things have to name that alias, not one. The profile config covers Chat, which resolves the model on every message; sessions created through the agent's HTTP API instead take a model resolved once at gateway startup, and that path reads `API_SERVER_MODEL_NAME`. The operator sets both from the same constant so they cannot drift — if they do, Chat keeps working while every API-created session (autonomous event triage, for one) dies asking LiteLLM for a model it does not serve.

The two substituted values come from the install (`MODEL_PROVIDER` and `MODEL_DEFAULT_NAME`, saved in `install.env` and carried into the chart values). Supported providers and their shipping defaults:

| `MODEL_PROVIDER`       | Default `MODEL_DEFAULT_NAME`   | Notes                                                                                                                                 |
| ---------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `gemini` (default)     | `gemini-3.5-flash`             | Uses `GEMINI_API_KEY`.                                                                                                                |
| `anthropic`            | `claude-opus-5`                | Uses `ANTHROPIC_API_KEY`.                                                                                                             |
| `openai`               | `gpt-5.4`                      | Uses `OPENAI_API_KEY`.                                                                                                                |
| `vertex_ai`            | `gemini-3.5-flash`             | Workload Identity by default (or a SA key Secret). See below.                                                                         |
| `openai_compatible`    | none; required                 | Optional `OPENAI_COMPATIBLE_API_KEY`. The base URL is required too. See [below](#openai-compatible-endpoints-self-hosted-or-private). |
| `sap`                  | `anthropic--claude-4.5-sonnet` | OAuth2 client credentials from a Secret you create. See [below](#sap-ai-core).                                                        |
| any other LiteLLM slug | none; required                 | `azure`, `bedrock`, `databricks`, ...; credentials from the same Secret. See [below](#other-litellm-providers).                       |

Any model string the chosen provider accepts is valid — there is no allow-list in the harness. For example, [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) pins `gemini-3.1-flash-lite`.

For a Claude model (`anthropic`, `vertex_ai` with a `claude-` model name, or `sap` with an `anthropic--` model name) the chart adds `additional_drop_params: ["temperature", "top_p", "top_k"]` under every alias. Claude Opus 5 refuses all three with a `400`, and a caller can send them without knowing which model sits behind `model-default`, so the gateway removes them from every request before it reaches the provider. A caller therefore cannot set sampling on any Claude model through the gateway. The kustomize dev copy does not render the key.

To change the default on an installed system, re-run the installer with `--menu` (e.g. `./install.sh --menu` or `$HOME/kube-agents/install.sh --menu`) and use the model-provider entry followed by **Save & Apply** — one `terraform apply` rewrites the LiteLLM `ConfigMap` and rolls the gateway. On a dev cluster, set the variables and redeploy the dev copy:

```bash
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.5-flash
make -C k8s-operator deploy-litellm
```

Either way the agent picks up the new model on its next request without any change to its own config.

### Setting the output-token budget

`MODEL_MAX_TOKENS` is the number of output tokens the gateway asks the provider for on a request that names none. Set it in `install.env` or with `--model-max-tokens=N`; it reaches the chart as `litellm.maxTokens` and the Terraform example as `model_max_tokens`, and the chart renders it as `max_tokens` under every `model_list` alias. With `MODEL_PROVIDER=gemini` and `MODEL_MAX_TOKENS=8192` the first alias reads:

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: gemini/gemini-3.5-flash
      max_tokens: 8192
```

The chart is the only path that renders it: the kustomize dev copy above (`make -C k8s-operator deploy-litellm`) carries no `max_tokens`. On an installed system, set the key in `install.env` and re-run `upgrade.sh`, or use `install.sh --menu` and **Save & Apply**, which regenerates from `install.env`; there is no menu entry for it.

At the default of `0` nothing is rendered and the gateway config is what it was. The hosted providers above need no value: the agent's requests carry their own `max_tokens`, and a provider with its own output limit answers a value above it with an error rather than a truncated reply. A self-hosted backend such as vLLM, SGLang, TGI or llama.cpp admits a request only if the prompt and `max_tokens` together fit its served window, so a request that asks for more output than the window holds is refused before the model runs. Set the value below that window, leaving the prompt the room it needs.

The value is a default the gateway supplies, not a cap it enforces: LiteLLM lets a request's own `max_tokens` win over the one in `litellm_params`. The agent image pinned today (Hermes `v2026.9.14`) does not send `max_tokens` on agent turns, so this value is the one the agent's requests get, the same as a direct call to the gateway's `/v1/chat/completions` that omits the field. Images built on `v2026.8.19` and earlier sent their own `max_tokens` on every request; on those the client's value won for agent turns and only a request that omitted the field took the configured one.

On non-adaptive Anthropic Claude models, LiteLLM maps `fallback.reasoningEffort` (`low`, `medium`, `high`) to `thinking.budget_tokens` (`1024`, `2048`, `4096`), and Anthropic requires `max_tokens > thinking.budget_tokens`. When setting `litellm.maxTokens` alongside `fallback.reasoningEffort` on a Claude model, set `maxTokens` strictly above that effort budget (for example, `> 4096` for `high`).

### Fallback alias and parameter dropping

Two chart blocks, both off by default, add a fallback alias and router retry limits when a primary call fails. At their defaults nothing is rendered, so the gateway config and its rollout checksum are what they were.

`litellm.fallback.reasoningEffort` (`low`, `medium` or `high`) adds a fallback alias for the same model at its own reasoning effort, named `litellm.fallback.modelName` or `<model>-<effort>` by default. `"minimal"` is omitted because Vertex AI Gemini 3.x Flash rejects `thinkingLevel: "minimal"` with HTTP 400 and Claude maps it to a 1024-token thinking budget. It also renders a `router_settings.fallbacks` entry that routes each primary alias (`model-default`, `hermes-agent` and the model name) to the fallback once primary retries are spent.

`litellm.fallback.timeoutSeconds` and `numRetries` render `router_settings.timeout` and `num_retries` (both default to `0`, omitting the keys). On Vertex `gemini-3.5-flash`, a `600`-second request cap covers `99.96%` of streams while `300` seconds covers `99.57%`. The render fails if `litellm.fallback.modelName` is set without `litellm.fallback.reasoningEffort` or matches a primary alias name. With `fallback.reasoningEffort: low` and `gemini-3.8-flash` on Vertex AI the result reads:

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: vertex_ai/gemini-3.8-flash
      thinking: { "type": "enabled" }
  # hermes-agent and gemini-3.8-flash: the same
  - model_name: "gemini-3.8-flash-low"
    litellm_params:
      model: vertex_ai/gemini-3.8-flash
      reasoning_effort: "low"
litellm_settings:
  drop_params: true
  callbacks:
    ["prometheus", "litellm_thinking_replay_callback.proxy_handler_instance"]
router_settings:
  fallbacks:
    - model-default: ["gemini-3.8-flash-low"]
    # one entry per primary alias
```

The primary aliases keep the `thinking` key that a Gemini 2.5 or 3.x model gets ([Gemini's thoughts](#geminis-thoughts)). The fallback alias never gets it: it carries its own `reasoning_effort`, and the two on one alias fail every Gemini 3.x request.

`litellm.dropParams: true` (or setting `litellm.fallback.reasoningEffort`) renders `drop_params: true`, which tells LiteLLM to drop a parameter the provider does not accept rather than fail the request (for example, `temperature != 1.0` when Anthropic Claude thinking is enabled). It drops silently. LiteLLM decides whether a model takes `reasoning_effort` from its model cost map, so on a model the map does not list, the effort can vanish from every request with no error. Check the model is in the map before you rely on the two together.

A fallback is not silent. The gateway's reply carries `x-litellm-attempted-fallbacks` (how many fallbacks it tried), `x-litellm-model-group` (the alias that answered) and `x-litellm-attempted-retries`. With LiteLLM's log level at `INFO` the pod also logs `Falling back to model_group = <alias>` and `Successful fallback b/w models.`; the chart does not raise the level.

### Setting the reasoning effort

`litellm.reasoningEffort` is the reasoning effort the gateway asks the model for on a request that names none: `low`, `medium` or `high`. The chart renders it as `reasoning_effort` under every `model_list` alias. With `litellm.modelProvider=vertex_ai` and `litellm.reasoningEffort=high` the first alias reads:

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: vertex_ai/gemini-3.5-flash
      reasoning_effort: "high"
```

LiteLLM translates the value for each provider. On Gemini 3.x, through `gemini` or `vertex_ai`, it becomes `thinkingConfig.thinkingLevel` on the request to Google. On Claude it becomes adaptive thinking with that effort, and OpenAI and `openai_compatible` servers receive `reasoning_effort` as written. A model that LiteLLM does not treat as a reasoning model, such as `gemini-2.0-flash`, fails every request with `UnsupportedParamsError`, so leave the value empty for one.

At the default, empty, no `reasoning_effort` is rendered and the model thinks at its own default level; a Gemini 2.5 or 3.x model keeps the `thinking` key described in [Gemini's thoughts](#geminis-thoughts). The value is a default the gateway supplies, the same as `max_tokens` above: LiteLLM lets a request's own `reasoning_effort` win over the one in `litellm_params`.

The chart is the only path that renders it. The kustomize dev copy carries no `reasoning_effort`, and neither `install.env` nor the Terraform example has a variable for it. Set it through the Terraform example's `extra_helm_values` (`{ litellm = { reasoningEffort = "high" } }`) or with `--set litellm.reasoningEffort=high` on a direct Helm install.

### Prompt caching

Agent turns are mostly re-sent context: the same system prompt, skills, and conversation tail go up again on every tool call. Anthropic-family models bill that at full price unless the request marks where the reusable prefix ends, and the marks have to be in the request — so when the default model is an Anthropic-family one (`MODEL_PROVIDER=anthropic`, or `vertex_ai` with a `claude-` model), the gateway adds them, via [`cache_control_injection_points`](https://docs.litellm.ai/docs/tutorials/prompt_caching) in its `config.yaml`:

```yaml
router_settings:
  default_litellm_params:
    cache_control_injection_points:
      - location: message
        role: system
        control:
          type: ephemeral
          ttl: 1h
      - location: message
        index: -3
      - location: message
        index: -1
```

The agent cannot do this itself, and that is the point of putting it here. It asks for `model-default` over the Completions API and never learns what is behind the alias, while the harness only emits its own cache markers when it recognises a Claude-named model — so on an Anthropic backend it caches nothing. Teaching it otherwise would mean naming the model in the agent config, which is exactly the coupling the gateway exists to prevent. A 45-call agent session measured 3.5M input tokens and zero cache reads before this block; the first cron tick after it re-ran the same 83k-token prompt as a cache write, and subsequent ticks read it back.

The system prompt takes the 1h tier because it is the largest static span, every profile and cron tick shares it, and a read refreshes the TTL — so a half-hourly cron schedule keeps it warm instead of missing a 5-minute window every time. The two rolling points ride the conversation tail on the default 5-minute tier. Anthropic allows four breakpoints per request; LiteLLM counts any the caller supplied and never overwrites them, so a client with its own layout still wins.

Any other model gets no breakpoints. On Gemini, LiteLLM turns these markers into Vertex explicit context caching: before every model call it lists the project's cached contents, and it creates a new cache whenever the cached prefix is new. A create that Vertex is slow to finish fails the whole turn: in one case it surfaced as a 401 after a stall of more than ten minutes. Gemini's implicit caching needs no markers and still serves a repeated prefix from cache. The chart reads the same provider and model name as the sampling-key drop above, with one difference: a Claude model behind `sap` gets the drop and no breakpoints. Switching `MODEL_PROVIDER` re-renders both. The kustomize dev copy adds them for `MODEL_PROVIDER=anthropic` only, in its `anthropic` overlay; `vertex_ai` with a `claude-` model gets them only through the chart.

### Gemini's thoughts

Gemini thinks before it answers whether or not a request asks it to, but it returns those thoughts only when the request sets `thinkingConfig.includeThoughts`. With it off, Gemini 3.x has been seen writing its reasoning into the answer instead, under a first line `thought`, where nothing downstream can tell it from the answer. To give the thoughts a channel of their own, for a Gemini 2.5 or 3.x model through `gemini` or `vertex_ai` the chart renders `thinking` under every `model_list` alias:

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: vertex_ai/gemini-3.5-flash
      thinking: { "type": "enabled" }
```

LiteLLM maps it to `includeThoughts` alone, with no `thinkingLevel` or `thinkingBudget`, so the model thinks as deeply as it did before and its thought summaries come back as `reasoning_content` beside the answer. The agent keeps them out of its replies: the agent's API answers with the reply text alone, the agent image never uses the reasoning as a reply (which the harness would otherwise do for a turn that ends with reasoning and no answer, or a safety refusal that gives no explanation), and the Slack and Google Chat gateways never display reasoning. `/reasoning show` answers that display is turned off, and a `show_reasoning` setting in the agent's config has no effect.

No other model gets the key. LiteLLM refuses it on a model it does not treat as a reasoning model, such as `gemini-2.0-flash`, and on Claude it would turn thinking on. Setting `litellm.reasoningEffort` replaces it: LiteLLM asks for the thoughts with that value too, and refuses the two together on Gemini 3.x. A request that names its own `reasoning_effort` overrides it the same way. The kustomize dev copy does not render the key.

### Replayed thinking on Gemini

A client that turns thinking on, as Claude Code does in the session pods of an install on the `spec.mode: next` stack, sends each earlier turn's thinking back in the next request. LiteLLM releases before v1.106.0 passed that thinking to a Gemini model as ordinary text rather than as a thought, so Gemini read its earlier thought summaries as things it said and opened its next answers with more of them: a bold step title and a line such as "I will now draft the report". No prompt reached that text. The release the install pins ([Container images](/kube-agents/deploy/docker-images/)) carries the upstream fix.

Both the chart and the kustomize base still load a pre-call hook that drops replayed thinking blocks from earlier assistant turns before the request goes to a Gemini model. It removes more than the upstream fix does. Thinking still streams back, and requests to any other model, Claude above all, go out as sent. The gateway log records each request it changed, as `kube_agents.litellm_thinking_replay INFO thinking replay: dropped N replayed thinking block(s)`.

### Redaction at the gateway

Everything the agent observes on a cluster goes up in the next request: pod IPs, cluster and project names, and whatever credential material a command printed. The gateway is the one point every provider request transits, so it is where redaction runs. `litellm.redaction.enabled=true` in the chart values mounts the shared redactor module (a copy of the chat plugins' `AuditRedactor`, kept identical by a test) and a LiteLLM pre-call hook beside `config.yaml`, and the hook rewrites `messages[].content` (strings and `text` parts), each assistant message's `tool_calls[].function.arguments` (parsed, so a key name such as `password` counts as it does in a mapping), embeddings `input` and completion `prompt` before LiteLLM calls the provider. The rendered default config does not change while the value is off.

Three layers run, in this order:

1. **The built-in credential patterns**, always on: GCP API keys and OAuth tokens, PEM private keys, bearer and basic auth values, GitHub, OpenAI, Anthropic and Slack tokens, AWS access key IDs, JWTs, the password in a URL (`scheme://user:password@host`, unless it holds `,`, `;`, a quote or an unencoded `/`, `?` or `#`), secret-shaped key/value pairs (a block scalar's lines included), container env entries whose name is secret-shaped (in YAML and in JSON), Kubernetes Secret `data:` blocks and the `data`/`stringData` objects of JSON that holds a Secret, and e-mail addresses other than service-account principals. These are masked (`[REDACTED_SECRET]`, `[REDACTED_PRIVATE_KEY]`, `[REDACTED_EMAIL]`). A `true`, `false`, `null` or `none` value, or an env reference such as `$(DB_PASSWORD)`, is left as it is under a secret-shaped name, so `automountServiceAccountToken: false` still reads as a boolean. JSON held inside a JSON string, such as the `kubectl.kubernetes.io/last-applied-configuration` annotation in `kubectl get -o json`, is decoded and redacted the same way.
2. **IP literals**, IPv4 and IPv6, under `litellm.redaction.ip`. `action: pseudonym` (the default) replaces each with `[ip:<12 hex>]`; `mask` replaces each with `[REDACTED_IP]`; `"off"` leaves them alone, and it has to be quoted because YAML reads the bare word as a boolean, which the render refuses. `allowCidrs` lists the networks the model must still see, such as `127.0.0.0/8` or a service range.
3. **Operator rules** under `litellm.redaction.rules`, each a `name` (a letter or digit, then letters, digits, `_`, `.`, `-`), exactly one of `literal` (an exact string) or `pattern` (a Python regular expression), both non-empty, and an `action` of `mask` (default) or `pseudonym` (`[<name>:<12 hex>]`). A mask marker is the name upper-cased with punctuation folded to `_`, so the `cluster-name` rule below masks as `[REDACTED_CLUSTER_NAME]`.

```yaml
litellm:
  redaction:
    enabled: true
    ip:
      action: pseudonym
      allowCidrs: ["127.0.0.0/8"]
    rules:
      - name: cluster-name
        literal: prod-eu-1
        action: pseudonym
      - name: project
        pattern: "my-proj-[0-9]+"
```

Through `install.sh`, set `LITELLM_REDACTION_ENABLED=true` in `install.env` or pass `--litellm-redaction`. `LITELLM_REDACTION_IP_ACTION` (`--litellm-redaction-ip-action`) and `LITELLM_REDACTION_IP_ALLOW_CIDRS` (`--litellm-redaction-ip-allow-cidrs`, comma- or space-separated) set the IP layer, and `LITELLM_REDACTION_RULES` holds the rules as a JSON array in single quotes, with no flag. The same settings as above:

```bash
LITELLM_REDACTION_ENABLED=true
LITELLM_REDACTION_IP_ALLOW_CIDRS=127.0.0.0/8
LITELLM_REDACTION_RULES='[{"name":"cluster-name","literal":"prod-eu-1","action":"pseudonym"},{"name":"project","pattern":"my-proj-[0-9]+"}]'
```

They reach the Terraform example as its `litellm_redaction` variable (`enabled`, `ip_action`, `allow_cidrs`, `rules`), which renders the chart values above only while `enabled` is true; a `litellm.redaction` key in `extra_helm_values` still wins. On an installed system, set the keys in `install.env` and re-run `upgrade.sh`, or use `install.sh --menu` and **Save & Apply**; there is no menu entry for them. The kustomize dev copy (`make -C k8s-operator deploy-litellm`) carries no redaction: install through the chart or `install.sh` to have it.

A pseudonym is the first twelve hex characters of an HMAC-SHA256 over the value, keyed by `SESSION_KV_SALT` from the credentials Secret (the same mechanism that pseudonymises chat identities). The same value maps to the same token in every request while the salt holds, so the model can still tell two pods apart and correlate an address across turns; nothing maps a token back. Without the salt the gateway still redacts, with a per-pod salt the redactor warns about, so tokens stop matching across replicas and restarts.

**Pseudonymisation is not reversible, and that limits what the agent can do with a pseudonymised value.** The model never sees `10.0.0.5`; it sees `[ip:3fa9c2d1e0b4]`, and a `kubectl` command or a chat answer it writes from that token names an address that does not exist, so the tool call fails or the answer is useless. Pseudonymise identifiers the agent only needs to reason _about_; keep the ones it must act on in `allowCidrs`, use `off`, or accept that those tasks degrade. Masking has the same limit without the correlation.

What this covers is the request body on its way to the provider. It does not touch responses, so an identifier the model already knows from an earlier, unredacted turn can still come back. The same switch reaches the agent pod: the chart projects this rule file there at a fixed path, and the image applies the IP and operator-rule layers, after its own credential masking, to what the agent writes to its volume, `agent.log`, the terminal-output cache, the worker transcript, the session store (`state.db`) and the kanban board; [Security and IAM](/kube-agents/reference/security-and-iam/#what-leaves-for-the-model-provider) lists what that covers and what it does not. Each process reads the file once at start: a changed rule file reaches the gateway on its next roll, and in the agent pod a long-lived process keeps the rules it started with while each new worker process reads the current file, so roll the pod to move them all. The image parses the file with the grammar it shipped with, so keep the chart and the agent image at one version when this switch is on. It does not touch chat egress. A rule that fails to load stops the gateway pod at startup rather than forwarding requests unredacted, and the render fails on a name, action or source it does not accept; an exception inside the hook fails that request. Each redacted request writes one line of substitution counts by rule name to the gateway pod's log, never the payload.

### Pass-through trajectory logging

For offline evaluation, fine-tuning, latency analysis, and audit trails, `litellm.traceLogEnabled=true` enables pass-through JSONL trajectory capture at the gateway.

When enabled, the chart mounts `llm_trace_logger.py` and the shared `redactor.py` module beside `config.yaml`, registers `llm_trace_logger.proxy_handler_instance` under `litellm_settings.callbacks`, and passes `LITELLM_TRACE_LOG_PATH` (default: `/tmp/litellm_traces.jsonl`, which must reside under `/tmp/` because the gateway container runs with `readOnlyRootFilesystem: true` and only mounts an `emptyDir` at `/tmp`) plus `SESSION_KV_SALT` to the container.

Before any record is written to disk, `AuditRedactor` runs over `api_base`, `messages`, `tools`, `response`, and `error` to mask built-in credential patterns, Kubernetes `Secret` `data:`/`stringData:` payloads, secret-shaped keys, and e-mail addresses, and to pseudonymise IP literals (plus any operator rules from `redaction.yaml` when `litellm.redaction.enabled` is also on). With `litellm.redaction.enabled` on, IP literals follow `litellm.redaction.ip` instead: `"off"` writes them verbatim, `mask` masks them, and addresses in `allowCidrs` pass through. Because `redaction.enabled` only rewrites outbound requests, running `AuditRedactor` inside the trajectory logger ensures assistant responses and reasoning traces are also scrubbed before hitting `/tmp`.

Every completed or failed LLM invocation appends one JSON object containing:

- `timestamp`: UTC ISO 8601 timestamp.
- `status`: `"success"` or `"failure"`.
- `model_alias`: The requested model alias (e.g. `model-default`).
- `custom_llm_provider`: Upstream provider (`vertex_ai`, `hosted_vllm`, `gemini`, etc.).
- `api_base`: Backend URL (if configured, with URL userinfo passwords masked).
- `reasoning_effort`: Requested reasoning effort (e.g. `high`, `medium`, `low`).
- `latency_ms`: Round-trip invocation latency in milliseconds.
- `messages`: Redacted input messages and tool outputs.
- `tools`: Redacted tool schemas provided to the model.
- `response`: Redacted upstream response payload, including `choices`, `usage`, and `reasoning_content`.
- `error`: Redacted error message if the call failed.
- `truncated`: Present only on a record that was over `1 MiB` serialised: names the payload fields dropped (`messages`, `tools`, `response`) and the size the whole record had. The metadata fields above stay.

To bound disk consumption on the gateway pod's `/tmp` `emptyDir`, the logger rotates the trace file at `5 MiB` across `3` numbered backups (`.1`–`.3`) under an exclusive `flock` on `<path>.lock`, caps one record at `1 MiB` as described above, clips every caller-chosen string, and refuses to append a line that would carry a file past its cap (the once-per-path warning records it), so the on-pod trail holds at most `20 MiB`. Each record is serialised, redacted and written on one writer thread, off the proxy's event loop and in order, so a slow write holds neither other requests nor the readiness probe, and records never contend for the file. `litellm.traceLogPath` must be a normalised path under `/tmp/`: a `.` or `..` component, or a doubled or trailing slash, is refused at render time. The logger is strictly pass-through: it never mutates live request or response payloads in place, and write exceptions emit a once-per-path `WARNING` to stderr (`kube_agents.llm_trace_logger`) so logging I/O issues are observable in pod logs without failing model calls. When `litellm.traceLogEnabled` is `false` (default), the callback is omitted and the default ConfigMap checksum remains byte-identical.

### Vertex AI and Model Garden

`MODEL_PROVIDER=vertex_ai` routes `model-default` to Vertex AI in your own GCP project — the same first-party Gemini models, plus every Model Garden publisher model your project has access to (Anthropic Claude, Llama, Mistral, and the rest). Requests stay inside your project's billing and data boundary, and by default (when using Workload Identity rather than `litellm.vertex.credentialsSecretRef`) no model API key or static GCP key exists in the cluster. That boundary is a project, not a geography: the default location is the global endpoint, which makes no promise about the region a request is processed in — see the location bullet below.

Two things differ from the API-key providers:

- **Authentication is Workload Identity by default.** The gateway gets its own service-account pair rather than an API key — see [Security & IAM](/kube-agents/reference/security-and-iam/#the-vertex-ai-gateway-is-a-separate-identity). Unless a chart install opts into `litellm.vertex.credentialsSecretRef` (below), there is no entry in `platform-agent-secrets` for Vertex.
- **The endpoint is a project and a location.** `VERTEX_PROJECT_ID` and `VERTEX_LOCATION` become `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` on the gateway pod. The project defaults to the install's. The location defaults to `global` in `install.sh`, Terraform, and the chart — not the cluster's region, which need not serve the model you asked for and on a zonal cluster is not a valid Vertex location at all. (The kustomize dev path substitutes `VERTEX_LOCATION` with no fallback, so export it there.) Set `VERTEX_LOCATION` to a region when you have a data-residency requirement, when org policy blocks the global endpoint, or when the model is a Model Garden partner model served only from specific regions. Google's [locations page](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations) lists which locations serve which model, and its [data-residency page](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency) covers what the global endpoint does not guarantee.
- **A serving project you cannot administer.** With `VERTEX_PROJECT_ID` pointing at another project, the install enables `aiplatform.googleapis.com` there and grants the gateway's service account `roles/aiplatform.user` on it, which needs IAM rights on that project. Set `VERTEX_MANAGE_SERVING_PROJECT=false` (`--vertex-manage-serving-project=false`) when you do not have them: the install still creates the service account and its Workload Identity binding in your project, and you enable the API and make the grant by hand. Choose it at the first install; flipping it later on an install that already manages the two revokes the grant unless you remove them from Terraform state first (the [composition's README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) has the command). [Security and IAM](../reference/security-and-iam.md) covers what that does to the identity's lifecycle.

`MODEL_DEFAULT_NAME` is the Vertex **publisher model ID**, which is not always the same string the provider's own API uses — Model Garden Claude models, for instance, carry an `@`-suffixed version (`claude-sonnet-4-5@20250929`). Check the model's Model Garden card for the exact ID; a wrong one surfaces as a 404 from the gateway rather than a provisioning error.

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --model-provider=vertex_ai \
  --model-default-name=gemini-3.5-flash \
  --vertex-project-id=my-gcp-project \
  --vertex-location=us-east4   # both optional: project defaults to the install's, location to "global"
```

A re-run against an existing install reconciles the switch in one `terraform apply` — the gateway's IAM pair, its KSA, and the rolled ConfigMap land together.

Where Workload Identity is not available, the chart can read a service-account key and the project from a Secret instead. `litellm.vertex.credentialsSecretRef: {name, key}` mounts that key read-only at `/var/run/secrets/vertex/sa.json` and sets `GOOGLE_APPLICATION_CREDENTIALS` to it, which Google's client libraries prefer over the KSA's Workload Identity binding. `litellm.vertex.projectSecretRef: {name, key}` sets `VERTEXAI_PROJECT` from the Secret so the project id does not appear in the pod spec, standing in for `projectId`.

Both fields are chart-only and read only with `modelProvider: vertex_ai`; the render fails if either is set for another provider or omits `name` or `key`. Mint the key for a dedicated service account holding only `roles/aiplatform.user` on the serving project (see [Security & IAM](/kube-agents/reference/security-and-iam/#the-vertex-ai-gateway-is-a-separate-identity)), rotate it in the Secret, and prefer Workload Identity wherever the cluster supports it. Changing either Secret does not roll the gateway: run `kubectl rollout restart deployment/litellm -n kubeagents-system` after rotating it so the process reloads `VERTEXAI_PROJECT` and the key file.

### A credentials Secret for the gateway

`LITELLM_ENV_SECRET_NAME` (`--litellm-env-secret`, chart value `litellm.envFromSecret`, Terraform `litellm_env_secret_name`) names a Secret in the agent's namespace that the gateway container reads whole as environment variables. You create it with `kubectl`; the install only names it, so the credentials never pass through `install.env`, `terraform.tfvars` or Terraform state, and `upgrade.sh` never rewrites them. The mount is optional: the pod starts without the Secret, and the first model call, not the rollout, reports a missing credential. It is how the two provider shapes below, and an OAuth2 gateway, get their credentials.

A variable the chart also sets from `platform-agent-secrets` (`GEMINI_API_KEY`, `HOSTED_VLLM_API_KEY` and the others) wins over the same name in this Secret. Changing the Secret does not roll the gateway: run `kubectl rollout restart deployment/litellm -n kubeagents-system` after rotating it.

### SAP AI Core

`MODEL_PROVIDER=sap` routes `model-default` through [SAP AI Core](https://help.sap.com/docs/sap-ai-core)'s orchestration service, for an organization whose model traffic has to go through its SAP tenant. SAP AI Core takes no static key: it issues a JSON service key, and LiteLLM's `sap/` driver exchanges the client id and secret in it at your tenant's token URL for a short-lived bearer token, caches it and renews it before it expires. The driver reads the service key from `AICORE_SERVICE_KEY` and the resource group that holds your orchestration deployment from `AICORE_RESOURCE_GROUP`, so the Secret carries those two keys:

```bash
kubectl create secret generic sap-aicore -n kubeagents-system \
  --from-file=AICORE_SERVICE_KEY=./aicore-service-key.json \
  --from-literal=AICORE_RESOURCE_GROUP=default

curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --model-provider=sap \
  --litellm-env-secret=sap-aicore
```

The service key is the JSON the SAP BTP cockpit gives you when you [create a service key](https://help.sap.com/docs/sap-ai-core/sap-ai-core-service-guide/create-service-key) for the SAP AI Core instance. The driver also accepts the four discrete values `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_AUTH_URL` and `AICORE_BASE_URL` in place of the service key; it reads the first source that has a value and does not merge them, so give one shape, not both. Before the first model call the gateway looks up your tenant's most recent deployment of the `orchestration` scenario in that resource group and sends every request there, so one must exist.

Model names follow SAP's `<publisher>--<model>` form; the default is `anthropic--claude-4.5-sonnet`, and `MODEL_DEFAULT_NAME` picks another. A Claude model behind SAP has its sampling keys dropped like any other Claude model ([above](#setting-the-default-model)); prompt caching stays off, since the `cache_control` markers have not been shown to pass SAP's orchestration API. The token URL and the AI API URL are public SAP endpoints on port 443, which the gateway's NetworkPolicy already allows.

### Other LiteLLM providers

Any other [LiteLLM provider](https://docs.litellm.ai/docs/providers) works the same way: set `MODEL_PROVIDER` to the provider's slug (`azure`, `bedrock`, `databricks`, ...; lowercase letters, digits and underscores), `MODEL_DEFAULT_NAME` to the model, which is required because the install has no default for it, and put the variables that driver reads from the environment in the Secret. Nothing else is derived: the gateway's NetworkPolicy reaches public endpoints on 443 and nothing private, the provider's own cost table applies, and the installer offers the entry as the sixth choice of its provider menu.

## OpenAI-compatible endpoints (self-hosted or private)

`MODEL_PROVIDER=openai_compatible` routes `model-default` to any server that speaks the OpenAI chat-completions API: vLLM, TGI or an internal gateway, in the cluster or on your own network. LiteLLM stays in front of it, so the agent and Hindsight keep calling `inference-gateway` and their config does not change. The provider has no defaults. The model name and the server's base URL are both required, and the render fails without either.

In the chart values:

```yaml
litellm:
  modelProvider: openai_compatible
  modelDefaultName: google/gemma-3-27b-it # the model name the server serves
  openaiCompatible:
    apiBase: http://llm-service.models.svc.cluster.local/v1
    targetPort: 8000
```

`apiBase` is the server's base URL with its `/v1` path, `http://` or `https://`. `modelDefaultName` is the name the server answers to; vLLM serves the model it loaded under its Hugging Face ID unless it was started with `--served-model-name`.

`install.sh` and the Terraform example carry the same settings. The installer checks them against the chart's rules before it creates anything, and its interactive provider menu and the `--menu` panel offer the provider as a fifth entry:

| `install.env` key                  | Flag                                 | Chart value                                                                                  |
| ---------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------- |
| `OPENAI_COMPATIBLE_API_BASE`       | `--openai-compatible-api-base`       | `litellm.openaiCompatible.apiBase`                                                           |
| `OPENAI_COMPATIBLE_TARGET_PORT`    | `--openai-compatible-target-port`    | `litellm.openaiCompatible.targetPort`                                                        |
| `OPENAI_COMPATIBLE_EGRESS_CIDRS`   | `--openai-compatible-egress-cidrs`   | `litellm.openaiCompatible.egressCIDRs` (comma-separated in the installer)                    |
| `OPENAI_COMPATIBLE_CA_SECRET_NAME` | `--openai-compatible-ca-secret-name` | `litellm.openaiCompatible.caBundleSecretRef.name` (the key is always `ca.crt`)               |
| `OPENAI_COMPATIBLE_API_KEY`        | `--openai-compatible-api-key`        | key `OPENAI_COMPATIBLE_API_KEY` in `platform-agent-secrets`                                  |
| `LITELLM_ENV_SECRET_NAME`          | `--litellm-env-secret`               | `litellm.envFromSecret` (the OAuth2 client credentials, [below](#oauth2-client-credentials)) |

The Terraform example's variables are the same names in lower case (`openai_compatible_api_base` and so on), with `model_provider = "openai_compatible"` and `model_default_name`. The install above, through the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --model-provider=openai_compatible \
  --model-default-name=google/gemma-3-27b-it \
  --openai-compatible-api-base=http://llm-service.models.svc.cluster.local/v1 \
  --openai-compatible-target-port=8000
```

### The API key

The key is optional. The gateway reads it from `OPENAI_COMPATIBLE_API_KEY` in the credentials Secret (`platform-agent-secrets`) and sends it as a bearer token. Without it, requests carry the placeholder `Bearer fake-api-key`, which a server that checks no key ignores. With this provider the gateway pod gets none of the Gemini, OpenAI or Anthropic keys, so no request to your server can carry one of them.

### OAuth2 client credentials

An enterprise AI gateway often takes no static key: it issues a client id and a client secret, which are exchanged at an OAuth2 token endpoint (`client_credentials` grant) for a bearer token that expires after minutes or hours. Put the three values in the [credentials Secret](#a-credentials-secret-for-the-gateway) and name it with `--litellm-env-secret`:

```bash
kubectl create secret generic ai-gateway-oauth -n kubeagents-system \
  --from-literal=OPENAI_COMPATIBLE_OAUTH_TOKEN_URL=https://login.example.com/oauth/token \
  --from-literal=OPENAI_COMPATIBLE_OAUTH_CLIENT_ID=<client id> \
  --from-literal=OPENAI_COMPATIBLE_OAUTH_CLIENT_SECRET=<client secret>

curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --model-provider=openai_compatible \
  --model-default-name=<model> \
  --openai-compatible-api-base=https://ai-gateway.example.com/v1 \
  --litellm-env-secret=ai-gateway-oauth
```

With that Secret named, the chart ships a pre-call hook into the gateway (`litellm_oauth2_callback.py`, beside `config.yaml`). While `OPENAI_COMPATIBLE_OAUTH_TOKEN_URL` is set, it fetches a token before the first request, sends it as the bearer on every request bound for the server, and renews it 60 seconds before the expiry the token endpoint stated (a response that states none is treated as 5 minutes, so it renews after 4); one fetch at a time, with the gateway's log recording each fetch and renewal. A failed exchange fails the request that needed it with the token endpoint's status, and the next request tries again. Two optional keys: `OPENAI_COMPATIBLE_OAUTH_SCOPE` is sent as `scope`, and `OPENAI_COMPATIBLE_OAUTH_CLIENT_AUTH=post` puts the client id and secret in the form body for a token endpoint that does not take them as HTTP basic authentication, the default. Without the token URL the hook is idle and `OPENAI_COMPATIBLE_API_KEY`, if set, is the bearer as before.

The token request leaves the pod under the same [egress rules](#egress-to-the-server) as the model requests, which open the server's address and port and nothing else private. So the token endpoint has to be reachable where the server is: a public address on 443, an address inside `OPENAI_COMPATIBLE_EGRESS_CIDRS` on the same port as the base URL, or, for an in-cluster server, its namespace on `OPENAI_COMPATIBLE_TARGET_PORT`. For a server behind a [private CA](#a-server-behind-a-private-ca), the hook trusts that bundle beside the system roots when it calls the token URL, so the token endpoint can sit on the same private CA or on a public one. A gateway that is SAP AI Core itself does not need this: [`MODEL_PROVIDER=sap`](#sap-ai-core) carries the exchange in LiteLLM's own driver.

### How the gateway calls the server

Each alias renders as LiteLLM's `hosted_vllm` provider. Despite the name, that is LiteLLM's handler for any OpenAI-compatible server:

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: hosted_vllm/google/gemma-3-27b-it
      api_base: http://llm-service.models.svc.cluster.local/v1
```

LiteLLM's `openai/` handler would fit the API too, but it falls back to the `OPENAI_API_KEY` environment variable when an entry has no key, which either sends an OpenAI key to your server or fails every request, and it ignores a per-entry `ssl_verify`, so a private CA would have to replace the trust store for every provider at once. `hosted_vllm/` reads only `HOSTED_VLLM_API_KEY`, which the chart fills from the Secret key above, and honours `ssl_verify` on its own entries. It also strips `strict` from tool schemas and drops replayed reasoning content from earlier turns before the request leaves.

The gateway pod also sets `LITELLM_LOCAL_MODEL_COST_MAP=True` for this provider, so LiteLLM uses the model-price table bundled in its image instead of fetching one from GitHub at startup.

### A server behind a private CA

`litellm.openaiCompatible.caBundleSecretRef` names a Secret in the release namespace that holds the PEM bundle signing the server's certificate (under `key`, default `ca.crt`). The chart mounts that key read-only into the gateway pod at `/etc/litellm/openai-compatible-ca/` and points `ssl_verify` on this provider's entries at it. The bundle replaces the public roots for those entries, so it must hold every CA the server's chain needs. Nothing else trusts it: no other entry, no other pod, and no global `SSL_CERT_FILE`; the one addition is the [OAuth2 hook](#oauth2-client-credentials), which trusts it beside the system roots for the token URL. Through the installer, create the Secret first and pass its name:

```bash
kubectl create secret generic model-server-ca -n kubeagents-system --from-file=ca.crt=./ca.pem
```

### Egress to the server

The gateway's NetworkPolicy (`litellm-policy`) allows DNS, HTTPS to public addresses, the metadata server and the telemetry collector, and none of the places a private server usually lives. The chart adds one egress rule, derived from `apiBase`:

- **An in-cluster Service** (`<svc>`, `<svc>.<ns>.svc` or `<svc>.<ns>.svc.cluster.local`; a bare `<svc>` means the release namespace). A host with exactly two labels, such as `openrouter.ai`, is a hostname, not `<svc>.<ns>`, and follows the rules below; write an in-cluster Service as `<svc>.<ns>.svc`. The rule admits that namespace on `targetPort`, which is required in this form: NetworkPolicy matches traffic after the Service has translated it to the pod's port, so the Service port in the URL is not enough. The rule names the namespace, not the server's pods, so every pod in that namespace becomes reachable from the gateway on `targetPort`. Run the model server in a namespace of its own rather than the release namespace, which holds the agent and the gateway. If the server's namespace has its own ingress policy, that policy has to admit the gateway's namespace.
- **A private address outside the cluster**, such as an internal load balancer or an on-premises server over Interconnect or VPN. List its ranges in `egressCIDRs`; the rule admits those CIDRs on the URL's port, or 443 for `https` and 80 for `http` when the URL names none. NetworkPolicy matches addresses and never DNS names, so a hostname needs the ranges it resolves to. Each entry is a CIDR no wider than `/12` (IPv4) or `/48` (IPv6), at most 50 of them, and a range that reaches link-local or metadata addresses (`169.254.0.0/16`, `fe80::/10`, the metadata server's `fd20:ce::/48`) is refused, as is an IPv4-mapped IPv6 block. `egressCIDRs` is ignored for an in-cluster Service.
- **A public HTTPS endpoint on port 443** needs no rule: the gateway's existing 443 rule already reaches public addresses. A hostname on 443 that resolves to a private address does not ride that rule, because it excludes private ranges; give it `egressCIDRs`. The chart's static copy of that rule excludes `10.0.0.0/8`, `172.16.0.0/12` and `192.168.0.0/16`, and the operator's copy also excludes `100.64.0.0/10` and `169.254.0.0/16`.

Any other URL with `egressCIDRs` empty fails the render with a message that names `litellm.openaiCompatible.egressCIDRs`.

Use `https` for a server outside the cluster. Over `http`, prompts, which can carry cluster data, and `OPENAI_COMPATIBLE_API_KEY` cross the network unencrypted; the chart accepts it, and both `install.sh` and the chart's install notes (shown by `helm install` and `helm upgrade`) print a warning.

On a default install the operator owns `litellm-policy`, so the chart hands the rule to it as the `kubeagents.x-k8s.io/litellm-upstream-egress` annotation on the `PlatformAgent` ([PlatformAgent CRD](/kube-agents/operator/platformagent-crd/#reconcile-behavior)). The operator checks the value again, and on an invalid one logs `Ignoring invalid annotation value` and adds no rule rather than failing the reconcile; if the gateway cannot reach the server, look there first. With the operator off, the chart's own copy of the policy carries the same rule.

### Context window

A self-hosted server refuses a request whose prompt and `max_tokens` do not fit its served window. Set `litellm.maxTokens` (`MODEL_MAX_TOKENS`) below that window and serve a window large enough for the agent's prompt; [Setting the output-token budget](#setting-the-output-token-budget) has the details.

### What it does not cover

- The kustomize dev copy (`make -C k8s-operator deploy-litellm`) does not carry the provider, the credentials Secret or the OAuth2 hook, and the admin console's provider catalog does not list it. Install through `install.sh`, the Terraform example or the chart.
- The installer does not deploy a model server. The [vLLM example](#vllm-local-models) is one you apply yourself.
- A self-hosted model does not make the install offline. The agent still needs Google APIs, and some of its tools reach the internet on their own. [Security and IAM](/kube-agents/reference/security-and-iam/#egress-when-model-data-must-stay-off-hosted-providers) lists each destination, which ones are required, and how to turn off the rest. A fully disconnected cluster is not supported.

## vLLM (local models)

[vLLM](https://vllm.ai) serves open models with continuous batching, chunked prefill, and prefix caching for high throughput on GPU node pools.

### What ships

- [`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma) — Gemma 4 26B-A4B through vLLM's OpenAI-compatible server on two L4 GPUs, with a 128k-token context. Requires an accelerator node pool; the README gives the command. It runs in the release namespace, unlike the advice above; its README says what that means for the gateway's egress rule.

To put the gateway in front of it, point `litellm.openaiCompatible.apiBase` at its Service, as [OpenAI-compatible endpoints](#openai-compatible-endpoints-self-hosted-or-private) describes; the example's README gives the values.

## Inference replay

[`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay) is a small proxy that sits between the Platform Agent and LiteLLM. Requests are keyed by a SHA-256 hash of the canonicalized request body (messages plus params); hits return the cached response, misses forward to LiteLLM and cache the reply.

### Modes

- `mode: off` (default) — passthrough. Every request forwards.
- `mode: on` — cache hits return; misses forward and cache.
- Toggle at runtime:

  ```bash
  kubectl patch configmap inference-replay-config -n <ns> --type merge \
    -p '{"data":{"mode":"on"}}'
  ```

The proxy uses a `PersistentVolumeClaim` for the cache so replays survive pod restarts.

### When to use it

- Demos where you want repeatable output for the same inputs.
- CI tests against the agent's tool loop where LLM cost or non-determinism would be a problem.
- Cost containment during development.

Deploy it with `make -C k8s-operator deploy-inference-replay` — it is a development tool, never part of the installer.

## What the agent doesn't care about

The Platform Agent's config (`agents/platform/config.yaml`) doesn't mention the LLM provider. Provider selection is entirely at the LiteLLM / vLLM layer — the agent always talks to the `inference-gateway` Service, and the install decides what that Service resolves to. When the replay proxy is deployed, the `inference-gateway` Service is repointed at the replay proxy and the original gateway pods are re-exposed through a new `inference-gateway-upstream` Service that the proxy forwards cache misses to. That means:

- Swapping Gemini for Anthropic is a LiteLLM `ConfigMap` change.
- So is [prompt caching](#prompt-caching) — the breakpoints are injected gateway-side, because only the gateway knows which model they are for.
- Turning on replay is a `make -C k8s-operator deploy-inference-replay` on a dev cluster.
- Neither touches the agent's persona, skills, or governance layer.

## Where to go next

- [Reference → Examples](/kube-agents/reference/examples/) — the inference example bundles walked through.
- [Deploy → Network policies and Service](/kube-agents/deploy/kustomize/#kustomize-for-operator-integrations) — where the LiteLLM Deployment's dev copy lives on disk.
- [Concepts → Observability](/kube-agents/concepts/observability/) — LLM telemetry export.
