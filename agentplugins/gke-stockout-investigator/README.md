# GKE Stockout Investigator

Diagnoses GKE scale-up failures. When the cluster autoscaler cannot place a workload, this
judges whether the alert is a real, active stockout or a stale duplicate; if it is real, it
inspects the live cluster — pending pods, ComputeClasses, quotas, capacity advice — and
proposes a GitOps remediation PR.

The diagnosis is not in the alert. The alert carries only a cluster, a namespace and a
controller name; everything else comes from the agent looking at the cluster afterwards.

- **Skill**: [`files/skills/gke-stockout-investigator/SKILL.md`](files/skills/gke-stockout-investigator/SKILL.md),
  registered at load time as `gkestockoutinvestigator:gke-stockout-investigator`
- **Installs into**: the `platform` profile (`spec.targetProfile`)
- **Alert ingress**: the [`pubsub-platform`](../pubsub-platform/) adapter, configured by this
  chart's `platforms.pubsub` block

## Install

```bash
GCP_PROJECT_ID=<project> \
TARGET_CLUSTER_NAME=<cluster the alerts are about> \
KUBECTL_CONTEXT=<context of the cluster running the agent> \
  ./install.sh
```

`TARGET_CLUSTER_NAME` is required and has no default: it is compiled into the route's
filter expression, so a wrong value drops every alert without an error anywhere. The
installer builds the image locally and pushes it to Artifact Registry — see
[Images](../README.md#images) for the reference it derives and the variables that override
it; `PLUGIN_IMAGE` installs one that already exists and skips the build.

`HERMES_NAMESPACE` (default `kubeagents-system`) and `AGENT_REF` (default `platform-agent`)
say which agent to attach to. Give both installers the same pair: a route installed beside
one agent and the adapter that serves it beside another produces no error and no alert.

The installer also creates the Pub/Sub topic, subscription and log sink, grants the agent's
service account the roles it needs to read capacity and quota, and patches the
`PlatformAgent` with [`tuning.yaml`](tuning.yaml) — long remediation runs exhaust Hermes'
default retry and turn limits, and the resulting failure is reported as a protocol
violation rather than as "the limits were too low". Skip that with `APPLY_TUNING=false`
(see the file for the reasoning behind each number).

That is an environment variable on `install.sh`, not a Helm value, because the limits live
on the `PlatformAgent` — a resource this chart does not own, so they are patched rather
than templated:

```bash
APPLY_TUNING=false TARGET_CLUSTER_NAME=<cluster> ./install.sh
```

## Two behaviours worth knowing

**Alerts become kanban tasks, not chat turns.** The route sets `dispatch: kanban`, so the
alert is filed as a task owned by the `platform` profile. A plugin skill only resolves in
the profile that has the plugin, and the gateway answers as the _default_ profile — a turn
run there cannot open this skill however the prompt is worded, and the agent improvises
instead, which reads as a working investigation. Filing directly is also the cheaper path:
the alternative still ends in a kanban task, after a full turn spent deciding to create it.

**One stockout is one investigation.** The route deduplicates on workload identity —
project, location, cluster, namespace, controller kind and name — and deliberately not on
the failure reason, which the autoscaler varies between retries of the same incident. The
controller name keeps its ReplicaSet suffix on purpose: a new pod-template revision that
still cannot schedule is a failed remediation, and worth a fresh look.

## Verify and exercise

```bash
TARGET_CLUSTER_NAME=<cluster> ./verify.sh     # smoke test: does an alert reach the agent
```

`verify.sh` exits non-zero when the alert does not become work, and says which suppression
swallowed it — the filter, the threshold, or dedup. Each run uses a fresh workload name, so
a second run is a second incident rather than a duplicate of the first.

[`scenarios/`](scenarios/) holds one script per kind of capacity failure — GPU scarcity,
quota, rare VM shapes, Hyperdisk, priority starvation, duplicate and false signals. Each
wedges a real workload so there is something to diagnose. Use `--no-alert` to wait for the
autoscaler's own alert instead of publishing one: that is the only mode that shows whether
one real incident produces exactly one investigation.
