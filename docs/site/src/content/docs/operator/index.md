---
title: Operator overview
description: The Kubebuilder-based Go controller that reconciles PlatformAgent custom resources.
sidebar:
  order: 0
---

The `k8s-operator` is a Kubernetes controller that turns a `PlatformAgent` custom resource into a running Platform Agent Deployment plus everything it needs — Service, ServiceAccount, RBAC, PersistentVolumeClaims, and ConfigMaps for the agent config and logging. It also runs mutating (defaulting) and validating admission webhooks for the `PlatformAgent` type (see [Admission webhooks](#admission-webhooks)).

Source: [`k8s-operator/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator). Full README: [`k8s-operator/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/README.md).

## Layout

```text
k8s-operator/
├── api/v1alpha1/           # PlatformAgent type definitions (Kubebuilder)
├── cmd/                    # manager entrypoint
├── config/                 # Kustomize base for the operator + integrations
├── internal/               # controller reconciler + admission webhook logic
├── examples/               # sample PlatformAgent CR
├── testing/staging_workloads/  # multi-cluster staging PoC
├── Dockerfile              # controller manager image
└── Makefile                # generate, build, test, deploy
```

## What the operator manages

Custom resources in the `kubeagents.x-k8s.io/v1alpha1` API group:

- **`PlatformAgent`** — declares a Platform Agent instance, container image, service account, chat integrations, and harness toggles.
- **`AgentPlugin`** — declares OCI plugin extensions, secret environment variables, and allowed configuration overrides targeted to a `PlatformAgent`.

The controller reconciles a `PlatformAgent` into:

- A `Deployment` (named `<name>-gateway`) for the Platform Agent, running the Hermes runtime with a Fluent Bit log-forwarding sidecar and an `agent-api-auth` sidecar that terminates the PlatformAgent API bearer key and runs the `k8s-event-watcher`. The gateway holds no credential and executes nothing the model wrote.
- A `StatefulSet` (named `<name>-shell`) and its `Service`, the shell sandbox: `sshd` on `2222`, the durable `/opt/data`, and the wrappers that stand in for `gcloud`, `kubectl`, `gh` and `git`. This is the pod that runs model-authored commands, and its ServiceAccount carries no Workload Identity annotation.
- A `Deployment` (named `<name>-credential-proxy`), a `ClusterIP` `Service` on port `8765`, and a `NetworkPolicy` narrowing who may reach it — the credential broker, which holds every credential in the install and executes the real CLIs on the sandbox's behalf. See [Credential isolation](/kube-agents/reference/credential-isolation/).
- A `Service` fronting the gateway `Deployment` (API port `8642`, plus dashboard port `9119` when the dashboard is enabled).
- A `PodDisruptionBudget` selecting the Deployment's pods, `maxUnavailable: 1` at every replica count. That declares the agent evictable rather than blocking node drains, and it stays correct when the agent is scaled — a budget keyed to the replica count would deadlock drains the first time someone scaled back to one.
- A `ServiceAccount` (annotated for Workload Identity) plus RBAC — a viewer `ClusterRoleBinding` and an "explorer" `ClusterRole` with its own `ClusterRoleBinding`.
- `PersistentVolumeClaim`s for the agent's data and system metadata.
- `ConfigMap`s for the pod: config overlays merged into each Hermes profile's `config.yaml` at startup (including the whole rendered config for the default, Planning Agent, profile — see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile)), a `SETTINGS.md` (GKE scope) mounted into `/opt/data/`, and a Fluent Bit config for the logging sidecar. Each profile's base config is baked into the image and scaffolded at startup.
- Optional integrations wired through the CR `spec.integration` block: Google Chat (Pub/Sub topic/subscription), Slack (bot/app token secret refs), and GitHub (GitOps repo, with the GitHub Token Minter endpoint injected as an env var).

## Custom resource shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platformagent
  namespace: kubeagents-system
spec:
  harness:
    clusterName: cluster-a
    location: us-central1-a
    projectId: example-project
    hermes:
      dashboardEnabled: true
      pluginsDebug: false
      apiServerSecretRef:
        name: platformagent-secrets
        key: api-key
  deployment:
    # Image is optional and omitted here on purpose. Omit it to use the
    # operator's default image (its PLATFORM_AGENT_IMAGE env var for
    # private-registry installs, else the public ghcr.io image; see the Docker
    # images page). Set it only to pin an image/registry for this agent:
    #   image: registry.example.com/kube-agents/platform-agent
    imagePullPolicy: IfNotPresent
  security:
    serviceAccountName: kubeagents-platform-agent
    serviceAccountAnnotations:
      iam.gke.io/gcp-service-account: kubeagents-platform-gsa@<project>.iam.gserviceaccount.com
  integration:
    googleChat:
      # subscription config...
```

`harness.clusterName`, `harness.location`, and `harness.projectId` are all required. The credential
proxy only bootstraps a kubectl context when it has the complete triple; leave any one out and every
`kubectl` call the agent makes resolves to `localhost:8080` instead of a cluster.

Full walkthroughs: [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/) and [AgentPlugin CRD](/kube-agents/operator/agentplugin-crd/).

## Admission webhooks

The manager serves a mutating (defaulting) and a validating webhook for `PlatformAgent`, both
registered with `failurePolicy: Fail`. They are part of Kustomize installs only — Helm chart installs
run with `ENABLE_WEBHOOKS=false` (see the [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md)).

**The webhook server listens on port `10250`, not Kubebuilder's usual `9443`.** GKE creates one
firewall rule from the control plane to the nodes, and it permits only `tcp:443` and `tcp:10250`. The
API server dials the endpoint pod IP on the Service's `targetPort`, so on a private cluster a webhook
on any other port is unreachable until someone adds a VPC firewall rule for it — per cluster, by
hand. Serving on 10250 lands inside the rule GKE already made. It does not collide with the kubelet,
which binds 10250 on the node IP in a different network namespace.

The port is set in three places that must agree, and `TestWebhookPortsMatchDefault` fails the build
if they drift: the `--webhook-port` flag default (`DefaultPort` in
`internal/webhook/platformagent_webhook.go`), the manager `containerPort`, and the Service
`targetPort`. The Service `port` stays `443` regardless — that is what the `*WebhookConfiguration`
`clientConfig` resolves to, not what crosses the network.

### Serving on a different port

On a cluster where 10250 is not the reachable port — one that scopes GKE's rule to node IPs, or a
non-GKE cluster with its own constraints — **moving `--webhook-port` on its own wedges the cluster.**
The flag moves only the listener; the Service keeps sending the API server to 10250, nothing answers,
and `failurePolicy: Fail` blocks every `PlatformAgent` write. That is the outage this port change
exists to prevent, reached from the other side.

All three have to move together, so the override is a Kustomize patch rather than a flag:

```yaml
# config/webhook-port-patch.yaml, referenced from your overlay's `patches:`
- target:
    kind: Deployment
    name: controller-manager
  patch: |
    - op: add
      path: /spec/template/spec/containers/0/args/-
      value: --webhook-port=8443
    - op: replace
      path: /spec/template/spec/containers/0/ports/1/containerPort
      value: 8443
- target:
    kind: Service
    name: webhook-service
  patch: |
    - op: replace
      path: /spec/ports/0/targetPort
      value: 8443
```

Changing the compiled-in default instead of patching means editing `DefaultPort` as well —
`TestWebhookPortsMatchDefault` reads both manifests and fails the build if either still names the old
port. `--webhook-port` rejects anything outside 1–65535 at startup rather than letting
controller-runtime fall back to its own 9443 default.

### Upgrading from an operator that served 9443

Re-apply the manifests; do not bump the image alone. `targetPort` lives in the Service, so a
`kubectl set image` — or any pipeline that rolls the tag without re-applying `config/webhook/` —
leaves the Service pointing at 9443 while the new pod listens on 10250, which is the wedge described
below on what looked like a routine version bump. `make deploy` applies both.

Applying both together still leaves a short window: the Service starts sending traffic to 10250 the
moment it is applied, and the old pod does not answer there. Any `PlatformAgent` write in the gap
between the Service change and the new pod becoming Ready fails closed. It is seconds on a healthy
rollout, but schedule the upgrade accordingly rather than alongside a `PlatformAgent` change.

**If the API server cannot reach the webhook**, `failurePolicy: Fail` means every `PlatformAgent`
create, update, and delete fails with a timeout — including the edits you would use to fix it. Errors
read `context deadline exceeded` or `failed calling webhook`. To recover, and to roll back a bad
webhook deployment:

```bash
kubectl delete validatingwebhookconfiguration kubeagents-validating-webhook-configuration
kubectl delete mutatingwebhookconfiguration kubeagents-mutating-webhook-configuration
kubectl -n kubeagents-system set env deploy/kubeagents-controller-manager ENABLE_WEBHOOKS=false
```

That leaves the cluster with the same validation coverage a chart install has. Re-apply with
`make deploy` once the cause is fixed.

## Related resources

- [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/) — reference for `PlatformAgent` custom resource.
- [AgentPlugin CRD](/kube-agents/operator/agentplugin-crd/) — reference for `AgentPlugin` custom resource.
- [Development](/kube-agents/operator/development/) — build, test, and run the operator locally.
- [`scripts/installer/README.md`](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/README.md) — the installer helper scripts (`install.env` loader, tfvars generator), at the repository root rather than under `k8s-operator/`.
