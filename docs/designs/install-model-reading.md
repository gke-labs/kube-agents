# The install's model: which provider and model an install is on

**Status:** proposed, 2026-11-02. Workstream 6 of the telemetry epic that asked which model
provider and model serve an install. Builds on
[`usage-counters-producer.md`](usage-counters-producer.md) (the status block, its gauges and its
zero-PII contract) and [`human-interaction-readings.md`](human-interaction-readings.md) (how a
maintainer reads an install's numbers across installs: the customer's collector or the pasted
summary, nothing else).

## 1. The question and what answers it today

The maintainers want a headcount: how many installs run each provider and model, read from the
summary blocks customers paste and from the Prometheus projects customers share. One value per
install is enough; the mix of models inside an install, and how it moves, is not asked for.

Today the operator does not know the model. The chart (or the kustomize base) renders
`<provider>/<model>` into the installer-owned ConfigMap `litellm-config` (key `config.yaml`,
`model_list[].litellm_params.model`) and the operator pins the agent to the LiteLLM alias
`model-default` (`agentModelName` in `platformagent_manifests.go`). `feedback_summary.py` reads
that ConfigMap with `kubectl` and prints `model: <provider>/<name>` under `environment:`
(`model_from_litellm_config`: the first provider-qualified `model:` line; a name with a further
`/` keeps its provider and last segment only, so a resource path's project or region never reaches
the block). The gateway's `/feedback summary` reads no LiteLLM config and prints no model line.
Nothing in `status`, nothing on the operator's gauges, so a collector cannot read the model from
the CR.

## 2. Decisions

### 2.1 The value: the Platform Agent's default model, as configured

The install's model is the model LiteLLM serves under `model-default`: the `litellm_params.model`
of the `model_list` entry named `model-default` in `litellm-config`, split at its first `/` into
the provider and the name. Profiles that run other models do not change it, and the model that
served a given request does not either: the headcount wants one stable label per install, and the
configured default is the value that does not move with traffic. "Which model served the
requests" is a non-goal here (section 4).

The name is kept as typed, with one exception: a resource reference keeps its last segment only.
A resource reference is a name that, after the provider, starts with `projects/` (a Vertex AI
endpoint, `vertex_ai/projects/<id>/locations/<region>/endpoints/<n>`) or has a `/`-separated
segment that starts with `arn:` (a Bedrock model or inference profile,
`bedrock/arn:aws:bedrock:<region>:<account>:inference-profile/<id>`, and LiteLLM's routed forms
`bedrock/converse/arn:…` and `bedrock/invoke/arn:…`); the segment after its last `/` is the
model, so a project id, a region or an account id never reaches the status, the gauge or the
summary. Every other name keeps everything after the
provider: `hosted_vllm/meta-llama/Llama-3.1-8B-Instruct` reads as provider `hosted_vllm`, model
`meta-llama/Llama-3.1-8B-Instruct`, because on a self-hosted install the organisation prefix is
part of what is running. The summary script trims every slash-bearing name to its last segment
today; this design narrows that rule to the two resource-reference shapes, in both summary copies
and the operator, so the three agree. A provider slug outside the chart's table is the customer's
choice (the chart passes any LiteLLM slug through), and a name of a shape this rule does not
know is kept as typed; the anti-goal in section 4 is scoped to the two shapes the rule covers.

### 2.2 The carrier: `status.inference` and an info gauge

A new status block beside `status.usage`, not inside it:

| Field                       | Type   | Value                                                                                                                                                                                   |
| --------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status.inference.provider` | string | the provider slug, the part before the first `/` (`gemini`, `vertex_ai`, `anthropic`, `openai`, `hosted_vllm`, `sap`; the chart writes its `openai_compatible` choice as `hosted_vllm`) |
| `status.inference.model`    | string | the model name as typed after the provider (`gemini-3.5-flash`, `claude-opus-5`, `meta-llama/Llama-3.1-8B-Instruct`), or the last segment alone for a resource reference                |

Both absent while the operator has not read a model (no `litellm-config` in the namespace, no
`model-default` entry and no provider-qualified `litellm_params.model`, a read that failed); never
an empty string standing in for "unknown". `AgentStatus` gains `Inference InferenceStatus` with
that struct (the block itself serialises as `{}` like its sibling structs; "absent" is said of the
two fields); the served CRD skew rule the other newer fields follow applies (an older served CRD
prunes the fields, the operator notices from the echo and lands them once the CRD is applied).

The operator serves the pair as an info gauge on its metrics port, beside the usage gauges:

| Series                                                                    | Value | Labels                                                                  |
| ------------------------------------------------------------------------- | ----- | ----------------------------------------------------------------------- |
| `kubeagents_platformagent_model{agent_namespace, agent, provider, model}` | 1     | the two label names the usage gauges carry, plus `provider` and `model` |

One sample per agent; when the model changes the old label pair is deleted
(`DeletePartialMatch` on the agent's two labels, as `forgetUsage` does) before the new one is set,
so a collector never sees two models for one install. No sample while the two fields are absent,
and `forget`, which clears an agent's usage, time and phase samples when the CR is deleted, clears
this one too, so a deleted install leaves the headcount; `forget` is a method of the usage gauges
in `usage_gauges.go`, so that file gains the one call. The gauge itself lives in the new
`inference_status.go` with its reader, not in `usage_gauges.go`, whose header promises closed
label sets. `sum by (provider, model) (kubeagents_platformagent_model)` over a shared project is
the headcount.

The summary prints the pair. The script's `model:` line reads `status.inference` first and falls
back to the ConfigMap it reads today, so an install on an older operator still prints a model. The
gateway's Go copy gains the line from the CR, which it already reads, so the two blocks stop
differing on this line. The environment line keys become a shared list in both copies, as the
counters are, so `GatewayCopyTest` can hold them in step; today both copies write them inline.

### 2.3 The source: the operator reads `litellm-config`

The operator reads the ConfigMap the summary script reads, with the script's selection rule
ported as it stands (`_litellm_config`): every ConfigMap in the agent's namespace named
`litellm-config` or `litellm-config-*` (any suffix; the kustomize generator's is a content hash)
whose data carries `config.yaml` is a candidate; with several, the one the `litellm` Deployment
mounts wins, else the newest by creation time. It parses the YAML and takes the `model_list` entry
whose `model_name` is `model-default`, falling back to the first provider-qualified
`litellm_params.model` when no entry carries that name (a hand-edited config), then applies the
split and the resource-reference rule of section 2.1. The read goes through the reconciler's
uncached `APIReader`, not the manager cache: the installer writes the ConfigMap and then the
Deployment, and the reconcile the Deployment event triggers could otherwise read the ConfigMap's
previous content from a cache the ConfigMap informer has not caught up on. The read happens where
the Ready status is written (`updateStatusReady`), compared for change like the telemetry and
network-policy fields, and the block is written in the same update. The operator already holds
`get`, `list` and `watch` on ConfigMaps; no RBAC change.

A model change is an installer upgrade that re-renders the ConfigMap; it does not touch the
PlatformAgent, and no new watch is needed. The controller already watches the `litellm`
Deployment (a generation predicate, mapped to the agents in the namespace), and both install paths
move that Deployment's generation on a model change: the chart stamps a checksum of the rendered
config on the pod template, and the kustomize base (and the overlays that inherit it), whose
generated ConfigMap name hashes the un-substituted template and so does not move, stamps the pod
template with a `model-config` annotation carrying the provider and model for exactly this reason.
A template edit under kustomize renames the ConfigMap and rewrites the Deployment's volume
reference in the same apply,
which bumps the generation too. The one change nothing enqueues is a hand edit of the ConfigMap
alone; the status follows at the agent's next reconcile, which the steady path's periodic requeue
bounds, and LiteLLM itself serves such an edit only after its pod restarts.

Not chosen: a new `spec.inference` field the chart would set (a spec field the operator acts on
nowhere, and a second expression of a value the chart already renders into the ConfigMap); a call
to LiteLLM's `/model/info` from the operator (a credential the operator does not hold today, for a
value the ConfigMap already states); reading LiteLLM's token series for the served model (a window,
a tie-break rule and a second scrape target for a headcount that asked for one stable label).

### 2.4 The zero-PII contract

`status.usage` stays "static enums and integer counts only". The new block is beside it and holds
two short strings: the provider slug as LiteLLM names it (`gemini`, `vertex_ai`, `anthropic`,
`openai`, `sap`, `hosted_vllm`, a chart-set slug outside the table such as `azure`, or whatever a
hand-edited config says), passed through unchanged, and the model name, passed through as typed.
On a self-hosted or custom provider (`hosted_vllm`, which is also how the chart writes
`openai_compatible`, or `azure`) the model name is text the customer chose, a deployment name as
often as a catalogue name; the maintainers want it as typed (decided 2026-11-02), because a
self-hosted install is the case where the name says what is running. The resource-reference rule
of section 2.1 is what keeps a project id, a region or an account id out of the status, the gauge
label and the summary for the two shapes it covers; it is the only transformation, and the
operator applies no allowlist. `usage-counters-producer.md`'s security section gets a
sentence that names the block, says its two strings come from the install's own LiteLLM config,
and states the rule.

## 3. What changes, by component

- **Operator** (`common_types.go`, `platformagent_controller.go`, a new `inference_status.go`
  beside the usage files holding the reader, the parser and the gauge, `usage_gauges.go` for the
  `forget` call, the CRD copies): `InferenceStatus`, the ConfigMap selection and the YAML parse
  with the `model-default` lookup and the first-line fallback, the split and the
  resource-reference rule, the write in `updateStatusReady` with its unchanged-check term, the
  `kubeagents_platformagent_model` gauge published beside `publishPhase` with the delete-before-set
  and cleared by `forget`, the pruning record for the block; no new watch. Tests beside
  `usage_gauges_test.go` and the controller tests: parse (a plain name, a name with an
  organisation prefix, a Vertex AI resource path, a Bedrock ARN, a `model_list` without
  `model-default`, no provider-qualified entry, no ConfigMap, several candidates), the status
  written and cleared, the gauge's single sample, the label swap on a change and its removal on
  delete, and that a LiteLLM Deployment generation change enqueues the agent (the existing watch).
- **Feedback summary** (`feedback_summary.py`, `a2a/gateway/feedback_summary.go`,
  `test_feedback_summary.py`, `feedback_summary_test.go`): the status-first `model:` line in the
  script and its narrowed trimming rule (the two resource-reference shapes), the new line in the
  Go copy, the environment keys as a shared list, the copy test extended to it.
- **Docs**: `platformagent-fields.md` (generated), `platformagent-crd.md` (`## status`, the skew
  list), `observability.md` (the gauges table, the summary paragraph, the headcount query),
  `usage-counters-producer.md` (the status-as-gauges and security sections), the chart README's
  list of the operator's gauge series (the `operator.metrics` paragraph), `concepts/chatops.md` (the `/feedback summary` paragraph that says the script's block alone has
  the model) and `chat-feedback-command.md` (the same claim), and the sixth question answered on
  the observability page: in the list of one-line answers if
  `human-interaction-readings.md`'s second pull request has landed it, otherwise as one sentence
  in the gauges section that the list absorbs later. This change stands on its own; it does not
  wait for, and is not stacked on, the human-interaction pull requests, even though the summary
  files are shared ground, which is a rebase and not a dependency.

## 4. Goals, non-goals, what stays out

Goals: one `provider/model` per install, readable from the CR, the operator's gauges and both
summary copies, with no new egress.

Non-goals: the model that served a request (fallbacks, per-profile models, per-request overrides);
a per-model request mix or its movement over time; a model per AgentProfile in status.

Anti-goals: a project id, region, endpoint path or key fragment reaching the status, a label or
the summary; a second place that declares the install's model.

## 5. Eval exemption

Exempt under `.agents/rules/eval_driven_development.md` ("When it applies"): a status block, a
gauge and a summary line, with no model call in their paths; no prompt, skill or SOP changes, and
no text reaches the model.

## 6. Left to the builder

The exact unchanged-check and pruning-record names; whether the ConfigMap read shares a helper
with the token-minter ConfigMap read (`minterHeldEntries`, the operator's existing read of an
installer-owned ConfigMap by name); test fixture shapes; the order of the summary's environment
lines.
