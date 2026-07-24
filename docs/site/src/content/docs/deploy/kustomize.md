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
│   └── platform/
│       └── service.yaml        # ClusterIP Service for the Platform Agent
└── shared/
    ├── docker-entrypoint.sh
    ├── envoy-credential-proxy.yaml
    ├── envoy-credential-sidecar.sh
    └── defaults/config.yaml
```

The Kustomize surface today is one file: [`deploy/kustomize/platform/service.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/kustomize/platform/service.yaml).

```yaml
apiVersion: v1
kind: Service
metadata:
  name: platform-agent
  namespace: kubeagents-system
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

The exposed ports:

- `8642` — Hermes API server. Chat integrations and the operator health probes hit this.
- `9119` — Hermes dashboard. Behind `harness.hermes.dashboardEnabled` in the CR.

## Kustomize for operator integrations

`k8s-operator/config/` holds larger Kustomize bases the operator manager uses. Notable subtrees:

- `config/crd/` — the `PlatformAgent` CRD.
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

## What's coming (not merged)

[PR #230](https://github.com/gke-labs/kube-agents/pull/230) proposes Infrastructure-as-Code deployment for the operator: a Terraform stack under `k8s-operator/deploy/terraform/` plus a Helm chart at `k8s-operator/deploy/helm/kube-agents/` that packages the operator, agents, LiteLLM, and secrets. When it lands, this page will document both surfaces.
