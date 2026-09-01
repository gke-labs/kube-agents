---
title: Kustomize
description: What ships in deploy/kustomize/ and what the operator lays down on top of it.
sidebar:
  order: 1
---

The shipping Kustomize base at [`deploy/kustomize/`](https://github.com/gke-labs/kube-agents/tree/main/deploy/kustomize) is intentionally small — the operator lays down most of the concrete Kubernetes objects (`Deployment`, `ConfigMap`s, RBAC) itself when it reconciles a `PlatformAgent` CR.

## What's in the repo today

```text
deploy/
├── docker/
│   ├── Dockerfile              # multi-target Dockerfile (see Docker images)
│   ├── cloudbuild.yaml
│   └── merge_configs.py
├── kustomize/
│   ├── gke-dataplane-v2/       # GKE Dataplane V2 FQDN network policy overlay
│   │   ├── fqdn-networkpolicy.yaml
│   │   └── kustomization.yaml
│   └── platform/
│       ├── kustomization.yaml                 # Kustomize entrypoint
│       ├── networkpolicy-apiserver-egress.yaml # Egress policy for Kubernetes Control Plane
│       ├── networkpolicy-core-egress.yaml      # Egress policy for DNS and GCP Metadata
│       ├── networkpolicy-external-egress.yaml  # Egress policy for External HTTPS CIDRs
│       ├── networkpolicy-ingress.yaml          # Ingress policy for Hermes API & Dashboard
│       ├── networkpolicy-internal-egress.yaml  # Egress policy for LiteLLM, vLLM, Minty, OTel
│       └── service.yaml                       # ClusterIP Service for the Platform Agent
└── shared/
    ├── docker-entrypoint.sh
    ├── envoy-credential-proxy.yaml
    ├── start-services.sh
    └── defaults/config.yaml
```

The Kustomize surface at [`deploy/kustomize/platform/`](https://github.com/gke-labs/kube-agents/tree/main/deploy/kustomize/platform) includes the base Service and modular network isolation policies:

- [`networkpolicy-ingress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-ingress.yaml) — Explicitly allowlists required Ingress ports (`8642`, `8643`, `9119`) from within the namespace.
- [`networkpolicy-core-egress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-core-egress.yaml) — Egress for CoreDNS/NodeLocal DNS and GCP Workload Identity / Metadata server (`169.254.169.254/32` and `169.254.169.252/32`).
- [`networkpolicy-internal-egress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-internal-egress.yaml) — Egress for in-cluster services (LiteLLM, vLLM Gemma, GitHub Token Minter, and GKE Managed OTel Collector).
- [`networkpolicy-apiserver-egress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-apiserver-egress.yaml) — Egress to the Kubernetes Control Plane API Server (`10.96.0.1/32`).
- [`networkpolicy-external-egress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-external-egress.yaml) — Egress to external HTTPS endpoints (`0.0.0.0/0:443`) with RFC 1918 exclusions to prevent lateral movement.
- [`service.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/service.yaml) — ClusterIP Service for the Platform Agent.

### GKE Dataplane V2 & FQDN Network Policies

> [!IMPORTANT]
> **GKE Dataplane V2 Requirement**: The FQDN-based network policy features under [`deploy/kustomize/gke-dataplane-v2/`](https://github.com/gke-labs/kube-agents/tree/main/deploy/kustomize/gke-dataplane-v2/) (`FQDNNetworkPolicy` custom resource `networking.gke.io/v1alpha1`) **require GKE Dataplane V2** (`--enable-dataplane-v2`) **and FQDN Network Policy enabled** (`--enable-fqdn-network-policy`) on your Google Kubernetes Engine (GKE) cluster (running GKE 1.26.4-gke.500 or 1.27.1-gke.400 or later). Standard clusters running kube-proxy without Dataplane V2 will not enforce or support `FQDNNetworkPolicy` objects.

### Configuring NetworkPolicy for GKE Private Clusters, Dataplane V2, & Custom CIDRs

The base [`networkpolicy-apiserver-egress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-apiserver-egress.yaml) defaults the Kubernetes API Server egress CIDR to `10.96.0.1/32` (standard Kubernetes `kubernetes.default.svc` ClusterIP).

> [!IMPORTANT]
> **Kubernetes API Server Egress on GKE Dataplane V2**: On GKE Dataplane V2, eBPF performs Destination NAT (DNAT) on `kubernetes.default.svc` ClusterIP traffic to the control plane's internal endpoint before `NetworkPolicy` evaluation. Because Kubernetes NetworkPolicy `ipBlock` evaluates the post-DNAT destination address, the default ClusterIP `10.96.0.1/32` will not match.
>
> - **Operator Deployments**: The operator automatically discovers the real control plane endpoint IPs (from `default/kubernetes` Endpoints, `KUBERNETES_SERVICE_HOST`, and Service ClusterIP). You can supply custom CIDRs (including private fleet cluster control plane subnets like `172.16.0.0/28` and Private Service Connect VIPs) via the `kubeagents.x-k8s.io/apiserver-cidr` or `kubeagents.x-k8s.io/custom-egress-cidrs` annotation on the `PlatformAgent` CR, or the `KUBERNETES_API_SERVER_CIDR` environment variable on the operator deployment. To enable strict domain-level FQDN egress filtering on Dataplane V2 in operator mode, set the annotation `kubeagents.x-k8s.io/enable-fqdn-network-policy: "true"` on the `PlatformAgent` CR so the operator omits the blanket `0.0.0.0/0:443` IP rule.
> - **Static Kustomize Deployments**: When deploying with Kustomize, override the API server CIDR by patching the dedicated `platform-agent-apiserver-egress` policy directly.

> [!IMPORTANT]
> **Workload Identity metadata egress**: On GKE Dataplane V1 (iptables), the node DNATs `169.254.169.254:80` to the node-local metadata daemon at `169.254.169.252:988` in `nat PREROUTING` before `NetworkPolicy` is evaluated. Dataplane V2 (eBPF) evaluates policy pre-NAT at the socket layer, where the `169.254.169.254/32` rule on port `80` satisfies it directly. Ports `8080` and `987` (ALTS DirectPath) are intentionally omitted under least privilege since agent components authenticate over standard REST ADC. That deviates from Google's guidance, which recommends allowing both and warns that workloads omitting them "might experience disruptions during auto-upgrades" — if a token fetch starts failing during a node auto-upgrade, check the drop's destination port before looking elsewhere.
>
> - **Operator Deployments**: The operator generates both rules (`169.254.169.254/32` on port `80` and `169.254.169.252/32` on port `988`), covering both dataplanes out of the box. The cluster DNS ClusterIP is discovered from the `kube-system/kube-dns` Service; the metadata daemon endpoint is not discovered at all and falls back to `169.254.169.252`. Either can be overridden via the `kubeagents.x-k8s.io/dns-cluster-ip` / `kubeagents.x-k8s.io/metadata-daemon-ip` annotations, the typed `spec.networkPolicy` block on the CR, or the `KUBERNETES_DNS_CLUSTER_IP` / `KUBERNETES_METADATA_DAEMON_IP` operator environment variables — in that precedence order, ahead of discovery. [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/#specnetworkpolicy) is canonical for the typed field, including `enabled: false`, which stops policy generation and deletes both policies the operator owns — the gateway `NetworkPolicy`, and the `FQDNNetworkPolicy` that `kubeagents.x-k8s.io/enable-fqdn-network-policy` turns on.
> - **Static Kustomize Deployments**: [`networkpolicy-core-egress.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy-core-egress.yaml) ships both rules directly, covering both Dataplane V1 and Dataplane V2 out of the box.

Do **not** edit base manifests directly. If your cluster uses a different service CIDR, is a GKE Dataplane V2 cluster, is managing private-endpoint fleet clusters, or is a GKE Private Cluster with a specific Control Plane VIP range (e.g., `172.16.0.0/28`), override the CIDR cleanly in your deployment overlay using a Kustomize patch in your `kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - github.com/gke-labs/kube-agents//deploy/kustomize/platform?ref=main

patches:
  # 1. Patch API Server Control Plane CIDR / VIP (for Private Clusters / Fleet)
  - target:
      group: networking.k8s.io
      version: v1
      kind: NetworkPolicy
      name: platform-agent-apiserver-egress
    patch: |-
      - op: replace
        path: /spec/egress/0/to/0/ipBlock/cidr
        value: "172.16.0.0/28" # Replace with your GKE Control Plane VIP range, fleet cluster CIDR, or endpoint IP

  # 2. (Optional) Patch CoreDNS ClusterIP if your cluster uses a custom Service CIDR without NodeLocal DNSCache
  - target:
      group: networking.k8s.io
      version: v1
      kind: NetworkPolicy
      name: platform-agent-core-egress
    patch: |-
      - op: replace
        path: /spec/egress/0/to/3/ipBlock/cidr
        value: "10.0.0.10/32" # Replace with your custom kube-system/kube-dns Service ClusterIP
```

> [!NOTE]
> **Modular NetworkPolicies**: Because network policies are decomposed by concern (`platform-agent-ingress`, `platform-agent-core-egress`, `platform-agent-internal-egress`, `platform-agent-apiserver-egress`, `platform-agent-external-egress`), patches target dedicated resources directly rather than relying on brittle positional array indices within a single monolithic policy. The GKE Dataplane V2 overlay (`gke-dataplane-v2/`) deletes `platform-agent-external-egress` via `$patch: delete` and supplies `FQDNNetworkPolicy` without impacting other policies.

The canonical ClusterIP Service definition for the Platform Agent is defined in [`service.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/service.yaml):

```yaml
apiVersion: v1
kind: Service
metadata:
  name: platform-agent
  namespace: kubeagents-system
  labels:
    app.kubernetes.io/name: platform-agent
    app.kubernetes.io/instance: kubeagents-system-platform-agent
    app.kubernetes.io/part-of: kube-agents
    app.kubernetes.io/managed-by: kustomize
spec:
  selector:
    app: platform-agent
  ports:
    - name: api
      protocol: TCP
      port: 8642
      targetPort: 8642
    - name: dashboard
      protocol: TCP
      port: 9119
      targetPort: 9119
  type: ClusterIP
```

The `app.kubernetes.io/*` labels follow the project-wide contract that makes the whole kube-agents footprint selectable in one query — [Resource labels](/kube-agents/reference/resource-labels/) is canonical for what each key means and why `component` and `version` are absent.

The exposed ports:

- `8642` — Hermes API server. Chat integrations and the operator health probes hit this.
- `9119` — Hermes dashboard. Behind `harness.hermes.dashboardEnabled` in the CR. Nothing answers on the pod network; the listener is loopback-only — see [`PlatformAgent` CRD](/kube-agents/operator/platformagent-crd/#specharness) for how to reach it.

## Kustomize for operator integrations

`k8s-operator/config/` holds larger Kustomize bases the operator manager uses. Notable subtrees:

- `config/crd/` — the `PlatformAgent` and `AgentPlugin` CRDs.
- `config/rbac/` — ClusterRoles + bindings for the manager.
- `config/webhook/` — admission webhook config (validating + mutating). The Service targets port `10250` on the manager pod for the GKE firewall reason in [Admission webhooks](/kube-agents/operator/#admission-webhooks).
- `config/manager/` — Deployment for the controller manager, plus its `PodDisruptionBudget`.
- `config/integrations/github/` — Minty deployment and its `PodDisruptionBudget`.
- `config/integrations/litellm/` — LiteLLM Deployment + Service (plus `PodDisruptionBudget`, `NetworkPolicy`, `PodMonitoring`, and a `vertex_ai` overlay).
- `config/integrations/inference-replay/` — replay proxy Deployment, Service, PVC, and `PodDisruptionBudget`.
- `config/integrations/hindsight/` — the Planning Agent's memory store: API Deployment, Postgres/pgvector StatefulSet, and their Service, `PodDisruptionBudget`s, `NetworkPolicy`, and `PodMonitoring`.

Each is built and applied on its own; there is no aggregate kustomization over
`config/integrations/`, because every one of them needs `envsubst` over the built
output before it can be applied — each carries its image as a `${…}` variable so
a mirrored install can redirect it, and most need other substitutions besides.

These copies are the **development path**: a stock install gets the same
components rendered by the [`kube-agents` Helm chart](https://github.com/gke-labs/kube-agents/tree/main/charts/kube-agents)
(via the Terraform engine `./install.sh` drives), while `k8s-operator/config/`
remains the source of truth for the CRDs and operator RBAC the chart copies
(`make chart-check` enforces that). Deploy the dev copies via `make deploy-*`
from `k8s-operator/`:

```bash
make deploy                     # operator
make deploy-litellm             # inference gateway
make deploy-github              # Minty
make deploy-inference-replay    # replay proxy
make deploy-hindsight           # memory store
```
