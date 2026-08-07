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
│   │   ├── kustomization.yaml
│   │   └── networkpolicy-dataplane-v2-patch.yaml
│   └── platform/
│       ├── kustomization.yaml    # Kustomize entrypoint
│       ├── networkpolicy.yaml    # Ingress/egress NetworkPolicy for Platform Agent
│       └── service.yaml          # ClusterIP Service for the Platform Agent
└── shared/
    ├── docker-entrypoint.sh
    ├── envoy-credential-proxy.yaml
    ├── start-services.sh
    └── defaults/config.yaml
```

The Kustomize surface at [`deploy/kustomize/platform/`](https://github.com/gke-labs/kube-agents/tree/main/deploy/kustomize/platform) includes the base Service and network isolation policies:

- [`networkpolicy.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy.yaml) — Explicitly allowlists required Ingress ports (`8642`, `8643`, `9119`) and restricted Egress destinations (CoreDNS, GCP Metadata `169.254.169.254/32`, LiteLLM Gateway, the Kubernetes Control Plane `10.96.0.1/32`, and external HTTPS with RFC 1918 exclusions to prevent internal lateral movement).
- [`service.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/service.yaml) — ClusterIP Service for the Platform Agent.

### GKE Dataplane V2 & FQDN Network Policies

> [!IMPORTANT]
> **GKE Dataplane V2 Requirement**: The FQDN-based network policy features under [`deploy/kustomize/gke-dataplane-v2/`](https://github.com/gke-labs/kube-agents/tree/main/deploy/kustomize/gke-dataplane-v2/) (`FQDNNetworkPolicy` custom resource `networking.gke.io/v1alpha1`) **require GKE Dataplane V2** (`--enable-dataplane-v2`) **and FQDN Network Policy enabled** (`--enable-fqdn-network-policy`) on your Google Kubernetes Engine (GKE) cluster (running GKE 1.26.4-gke.500 or 1.27.1-gke.400 or later). Standard clusters running kube-proxy without Dataplane V2 will not enforce or support `FQDNNetworkPolicy` objects.

### Configuring NetworkPolicy for GKE Private Clusters, Dataplane V2, & Custom CIDRs

The base [`networkpolicy.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy.yaml) defaults the Kubernetes API Server egress CIDR to `10.96.0.1/32` (standard Kubernetes `kubernetes.default.svc` ClusterIP).

> [!IMPORTANT]
> **Kubernetes API Server Egress on GKE Dataplane V2**: On GKE Dataplane V2, eBPF performs Destination NAT (DNAT) on `kubernetes.default.svc` ClusterIP traffic to the control plane's internal endpoint before `NetworkPolicy` evaluation. Because Kubernetes NetworkPolicy `ipBlock` evaluates the post-DNAT destination address, the default ClusterIP `10.96.0.1/32` will not match.
>
> - **Operator Deployments**: The operator automatically discovers the real control plane endpoint IPs (from `default/kubernetes` Endpoints, `KUBERNETES_SERVICE_HOST`, and Service ClusterIP). You can supply custom CIDRs via the `kubeagents.x-k8s.io/apiserver-cidr` annotation on the `PlatformAgent` CR or the `KUBERNETES_API_SERVER_CIDR` environment variable on the operator deployment.
> - **Static Kustomize Deployments**: When using the Dataplane V2 overlay (`deploy/kustomize/gke-dataplane-v2`), you **must** patch rule index 5 (`/spec/egress/5/to/0/ipBlock/cidr`) with your cluster's actual control plane master / PSC endpoint IP or master IPv4 CIDR range (e.g., `172.16.0.0/28`).

Do **not** edit base manifests directly. If your cluster uses a different service CIDR, is a GKE Dataplane V2 cluster, or is a GKE Private Cluster with a specific Control Plane VIP range (e.g., `172.16.0.0/28`), override the CIDR cleanly in your deployment overlay using a Kustomize patch in your `kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - github.com/gke-labs/kube-agents//deploy/kustomize/platform?ref=main

patches:
  - target:
      group: networking.k8s.io
      version: v1
      kind: NetworkPolicy
      name: platform-agent-gateway-base-netpol
    patch: |-
      - op: replace
        path: /spec/egress/5/to/0/ipBlock/cidr
        value: "172.16.0.0/28" # Replace with your GKE Control Plane VIP range or endpoint IP
```

> [!WARNING]
> **Positional Patch Fragility**: JSON patches targeting specific rules by array index (e.g., `/spec/egress/5/to/0/ipBlock/cidr`) rely on the canonical rule order defined in [`networkpolicy.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/networkpolicy.yaml). If rule ordering changes in future updates, verify the index of the Kubernetes API Server egress rule before applying positional patches. The GKE Dataplane V2 overlay (`gke-dataplane-v2/`) replaces the blanket external HTTPS (`0.0.0.0/0:443`) rule with domain-level filtering via `FQDNNetworkPolicy`, retaining the internal Kubernetes API Server rule at index 5.

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
    app: platform-agent-gateway
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
- `9119` — Hermes dashboard. Behind `harness.hermes.dashboardEnabled` in the CR.

## Kustomize for operator integrations

`k8s-operator/config/` holds larger Kustomize bases the operator manager uses. Notable subtrees:

- `config/crd/` — the `PlatformAgent` and `AgentPlugin` CRDs.
- `config/rbac/` — ClusterRoles + bindings for the manager.
- `config/webhook/` — admission webhook config (validating + mutating).
- `config/manager/` — Deployment for the controller manager.
- `config/integrations/github/` — Minty deployment.
- `config/integrations/litellm/` — LiteLLM Deployment + Service (plus `NetworkPolicy`, `PodMonitoring`, and a `chatgpt` overlay).
- `config/integrations/inference-replay/` — replay proxy Deployment, Service, and PVC.

Deploy these via `make deploy-*` from `k8s-operator/`:

```bash
make deploy                     # operator
make deploy-litellm             # inference gateway
make deploy-github              # Minty
make deploy-inference-replay    # replay proxy
```
