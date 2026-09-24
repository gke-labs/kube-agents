---
title: Network policies and Service
description: The gateway NetworkPolicy and the Service the operator renders for the Platform Agent, and how to adjust the CIDRs they carry.
sidebar:
  order: 1
---

The operator lays down every concrete Kubernetes object for the Platform Agent — the `Deployment`, `ConfigMap`s, RBAC, the `Service` and the `NetworkPolicy` — when it reconciles a `PlatformAgent` CR. No static copy of the Service or the network policies ships in the repository: the objects below exist only as the operator renders them, and each carries an owner reference to the CR that produced it.

## The gateway NetworkPolicy

The operator renders one `NetworkPolicy` over the agent Pod, `<agent-name>-gateway-netpol`, covering:

- **Ingress** — the Hermes API (`8642`), the credential proxy (`8643`) and the dashboard (`9119`) from Pods in the agent's own namespace.
- **DNS and metadata egress** — CoreDNS and NodeLocal DNSCache, the cluster's DNS ClusterIP, and the GCP metadata server (`169.254.169.254/32` and `169.254.169.252/32`). The metadata address is also a DNS peer, on port `53` alone, because it is the resolver on a [Cloud DNS for GKE](https://cloud.google.com/kubernetes-engine/docs/how-to/cloud-dns) cluster.
- **In-cluster egress** — LiteLLM, vLLM, the GitHub token minter, Hindsight and the managed OTel collector.
- **Control-plane egress** — the Kubernetes API server, at the endpoints the operator discovers.
- **External egress** — HTTPS (`443`) to `0.0.0.0/0` minus the private ranges, unless FQDN filtering is on.

[Security and IAM](/kube-agents/reference/security-and-iam/) is canonical for the full rule set and why each peer is there. `spec.networkPolicy.enabled: false` stops policy generation and deletes the policies the operator manages; [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/#specnetworkpolicy) is canonical for that field.

### GKE Dataplane V2 & FQDN Network Policies

> [!IMPORTANT]
> **GKE Dataplane V2 Requirement**: Setting the annotation `kubeagents.x-k8s.io/enable-fqdn-network-policy: "true"` on the `PlatformAgent` CR makes the operator render a companion `FQDNNetworkPolicy` (`networking.gke.io/v1alpha1`) and omit the blanket `0.0.0.0/0:443` rule. That custom resource **requires GKE Dataplane V2** (`--enable-dataplane-v2`) **and FQDN Network Policy enabled** (`--enable-fqdn-network-policy`) on your Google Kubernetes Engine (GKE) cluster (running GKE 1.26.4-gke.500 or 1.27.1-gke.400 or later). Standard clusters running kube-proxy without Dataplane V2 will not enforce or support `FQDNNetworkPolicy` objects.

### Configuring NetworkPolicy for GKE Private Clusters, Dataplane V2, & Custom CIDRs

> [!IMPORTANT]
> **Kubernetes API Server Egress on GKE Dataplane V2**: On GKE Dataplane V2, eBPF performs Destination NAT (DNAT) on `kubernetes.default.svc` ClusterIP traffic to the control plane's internal endpoint before `NetworkPolicy` evaluation. Because Kubernetes NetworkPolicy `ipBlock` evaluates the post-DNAT destination address, a rule naming only the ClusterIP (`10.96.0.1/32` on a classic service range) will not match.
>
> The operator discovers the real control plane endpoint IPs (from `default/kubernetes` Endpoints, `KUBERNETES_SERVICE_HOST`, and the Service ClusterIP). You can supply custom CIDRs (including private fleet cluster control plane subnets like `172.16.0.0/28` and Private Service Connect VIPs) via the `kubeagents.x-k8s.io/apiserver-cidr` or `kubeagents.x-k8s.io/custom-egress-cidrs` annotation on the `PlatformAgent` CR, or the `KUBERNETES_API_SERVER_CIDR` environment variable on the operator deployment.

> [!IMPORTANT]
> **Workload Identity metadata egress**: On GKE Dataplane V1 (iptables), the node DNATs `169.254.169.254:80` to the node-local metadata daemon at `169.254.169.252:988` in `nat PREROUTING` before `NetworkPolicy` is evaluated. Dataplane V2 (eBPF) evaluates policy pre-NAT at the socket layer, where the `169.254.169.254/32` rule on port `80` satisfies it directly. Ports `8080` and `987` (ALTS DirectPath) are intentionally omitted under least privilege since agent components authenticate over standard REST ADC. That deviates from Google's guidance, which recommends allowing both and warns that workloads omitting them "might experience disruptions during auto-upgrades" — if a token fetch starts failing during a node auto-upgrade, check the drop's destination port before looking elsewhere.
>
> The operator generates both rules (`169.254.169.254/32` on port `80` and `169.254.169.252/32` on port `988`), covering both dataplanes out of the box. The cluster DNS ClusterIP is discovered from the `kube-system/kube-dns` Service; the metadata daemon container port is discovered from the `kube-system/gke-metadata-server` DaemonSet (falling back to port `988` and IP `169.254.169.252` if undiscoverable). Either can be overridden via the `kubeagents.x-k8s.io/dns-cluster-ip` / `kubeagents.x-k8s.io/metadata-daemon-ip` annotations, the typed `spec.networkPolicy` block on the CR, or the `KUBERNETES_DNS_CLUSTER_IP` / `KUBERNETES_METADATA_DAEMON_IP` operator environment variables — in that precedence order, ahead of discovery. [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/#specnetworkpolicy) is canonical for the typed field, including `enabled: false`, which stops policy generation and deletes the policies the operator manages — the gateway `NetworkPolicy`, the `FQDNNetworkPolicy` that `kubeagents.x-k8s.io/enable-fqdn-network-policy` turns on, and the shared `litellm-policy` (if LiteLLM is present).

Do **not** edit the rendered policy in the cluster: the operator applies it with server-side apply on every reconcile and reverts a hand edit. Change the annotations or the `spec.networkPolicy` block on the CR instead.

## The Service

The operator applies a `ClusterIP` Service named after the CR (`platform-agent` on a stock install) whose selector matches what it labels its gateway pods, `<agent-name>-gateway`. Its `app.kubernetes.io/*` labels follow the project-wide contract that makes the whole kube-agents footprint selectable in one query — [Resource labels](/kube-agents/reference/resource-labels/) is canonical for what each key means and why `component` and `version` are absent.

The exposed ports:

- `8642` — the Platform Agent API. Chat integrations hit this. It targets `8643` on the pod, the credential proxy's authenticated listener: Hermes itself binds `8642` on loopback only and validates a different key, so a caller never reaches it directly. [Credential isolation](/kube-agents/reference/credential-isolation/#request-paths) is canonical for that topology. The operator's health probes do not use this port — they `exec` `curl` against `127.0.0.1:8642` inside the container.
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
(via the Terraform engine the installer drives), while `k8s-operator/config/`
remains the source of truth for the CRDs, operator RBAC, admission policy and
webhook configuration the chart copies or mirrors (`make chart-check` enforces
that). Deploy the dev copies via `make deploy-*`
from `k8s-operator/`:

```bash
make deploy IMG=<registry>/k8s-operator:$(git rev-parse HEAD)   # operator; refuses a floating tag
make deploy-litellm             # inference gateway
make deploy-github              # Minty
make deploy-inference-replay    # replay proxy
make deploy-hindsight           # memory store
```
