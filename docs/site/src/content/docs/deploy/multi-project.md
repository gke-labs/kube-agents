---
title: Multiple GCP projects
description: How to grant a single kube-agents installation access to manage, debug, and audit GKE clusters and GCP infrastructure across multiple projects.
sidebar:
  order: 8
---

A single `kube-agents` installation in one host project can manage GKE clusters,
workloads, and GCP infrastructure across any number of separate GCP projects —
whether or not those projects belong to a shared GKE Hub fleet.

The installer binds the agent's Google Service Account in the host project
alone. Giving the agent access to another project requires only **granting the
service account read-only IAM roles** on that project and ensuring its GCP APIs
are enabled.

Once IAM access is granted, the two layers of the harness discover projects
differently:

- **Platform Agent operations, governance audits, and upgrade checks (IAM
  only):** The Platform Agent can immediately inspect resources in the target
  project via `gcloud`, and scheduled governance audits and fleet upgrade checks
  automatically discover every project the agent's identity can list
  (`gcloud projects list` unioned with `GCP_PROJECT_ID`, or
  `MONITORED_PROJECT_IDS` when pinned). Every audit qualifies cluster names with
  their project ID (`<project>/<cluster>`, or `<project>/<location>/<name>` in
  `fleet-consistency-drift`) so identical cluster names in different projects
  never collide. If the project listing fails, or a project cannot be read, the
  audit still runs on what it can reach and reports the run as partial. A
  project with the relevant API disabled counts as empty.
- **Cluster Agent profiles, specialist routing, and Kubernetes event watching
  (`spec.scope` or chat onboarding):** Unlike the governance audits, the hourly
  Cluster Agent reconciler (`cluster_agent_reconcile.py`) does **not** enumerate
  every project from `gcloud projects list` — by default it lists only the host
  project. For clusters in another project to get dedicated
  [Cluster Agent](/kube-agents/concepts/cluster-agents/) profiles (`cluster-*`),
  appear on the Planning Agent's specialist roster, and have their Kubernetes
  warning events watched by `k8s-event-watcher`, you either add the project ID
  to [`spec.scope.projects`](/kube-agents/operator/platformagent-crd/#specscope)
  or ask the agent in chat to onboard specific clusters (`manage-cluster`).

## Setup

### 1. Find the agent's service account

Look up the host project ID from the `PlatformAgent` resource:

```bash
kubectl get platformagents -A \
  -o jsonpath='{.items[0].spec.harness.projectId}{"\n"}'
```

The service account defaults to `kubeagents-platform-gsa`, giving the full
address `kubeagents-platform-gsa@<INSTALL_PROJECT_ID>.iam.gserviceaccount.com`.
Confirm the email if your install overrode the default:

```bash
gcloud iam service-accounts list \
  --project="<INSTALL_PROJECT_ID>" \
  --filter="displayName:kube-agents OR email:kubeagents" \
  --format="value(email)"
```

### 2. Grant read-only IAM roles (choose one approach)

The agent needs the read roles listed under
[`spec.scope` in the `PlatformAgent` CRD reference](/kube-agents/operator/platformagent-crd/#specscope),
which is the canonical list. Set them once:

```bash
AGENT_SA="kubeagents-platform-gsa@<INSTALL_PROJECT_ID>.iam.gserviceaccount.com"
ROLES="roles/container.clusterViewer roles/container.viewer roles/compute.viewer
  roles/monitoring.viewer roles/logging.viewer roles/iam.securityReviewer"
```

Then bind them with **either** per-project bindings or a single folder-level
binding.

#### Option A: Grant per project

Use this approach when onboarding individual projects:

```bash
for ROLE in $ROLES; do
  gcloud projects add-iam-policy-binding "<OTHER_PROJECT_ID>" \
    --member="serviceAccount:${AGENT_SA}" \
    --role="$ROLE" \
    --condition=None
done
```

#### Option B: Grant on a parent folder

Use this approach when onboarding all projects under a GCP folder at once, so
new projects created in that folder are automatically accessible without
repeating the bindings:

```bash
for ROLE in $ROLES; do
  gcloud resource-manager folders add-iam-policy-binding "<FOLDER_ID>" \
    --member="serviceAccount:${AGENT_SA}" \
    --role="$ROLE"
done
```

### 3. Enable the required APIs

Ensure the Kubernetes Engine, Compute Engine, Cloud Monitoring, and Cloud
Logging APIs are enabled in each target project:

```bash
gcloud services enable \
  container.googleapis.com \
  compute.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  --project="<OTHER_PROJECT_ID>"
```

### 4. Onboard clusters for Cluster Agent profiles and event watching (choose one approach)

Granting IAM access in Step 2 is sufficient for Platform Agent CLI queries and
scheduled governance audits. To also create per-cluster
[Cluster Agent](/kube-agents/concepts/cluster-agents/) profiles and enable
real-time Kubernetes event watching on clusters in another project, choose one
of the following approaches:

#### Option A: Automatically discover all clusters in the project (`spec.scope.projects`)

Add the target project IDs to [`spec.scope.projects`](/kube-agents/operator/platformagent-crd/#specscope)
on the `PlatformAgent` resource so the hourly reconciler automatically creates
and maintains a `cluster-<project>-<cluster>-<location>` profile for every GKE
cluster in those projects:

```bash
kubectl patch platformagent platform-agent -n kubeagents-system \
  --type=merge \
  -p '{"spec":{"scope":{"projects":["<OTHER_PROJECT_ID>"]}}}'
```

You can also exclude specific project globs or individual clusters under
`spec.scope.exclude`; see [`spec.scope` in the `PlatformAgent` CRD reference](/kube-agents/operator/platformagent-crd/#specscope).

#### Option B: Onboard individual clusters on demand in chat

If you do not want every cluster in the project reconciled automatically, ask
the agent in chat to manage specific clusters (for example: _"Manage my cluster
`payments-prod` in `us-central1` in project `payments-prod`"_). The Platform
Agent's `manage-cluster` skill creates the `Cluster Agent` profile on demand,
and the hourly reconciler preserves manually created profiles (`unmanaged` in
`fleet_scope.json`) without pruning them.

## Verify

IAM bindings take up to a minute to propagate. Check from the agent's sandbox
pod that the new project appears in `gcloud projects list`:

```bash
kubectl exec -it platform-agent-shell-0 -n kubeagents-system -c shell -- \
  bash -lc 'gcloud projects list --format="value(projectId)"'
```

Confirm the agent can read clusters in the target project:

```bash
kubectl exec -it platform-agent-shell-0 -n kubeagents-system -c shell -- \
  bash -lc 'gcloud container clusters list --project="<OTHER_PROJECT_ID>"'
```

If you configured `spec.scope.projects`, run the reconciler and inspect
`/opt/data/fleet_scope.json` to confirm the project outcome is `ok` and its
`cluster-*` profiles are registered:

```bash
kubectl exec -i deployment/platform-agent-gateway -n kubeagents-system \
  -c platform-agent -- bash -lc '
    /opt/hermes/.venv/bin/python3 /opt/data/scripts/cluster_agent_reconcile.py &&
    cat /opt/data/fleet_scope.json &&
    HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes profile list
  '
```

## Removing a project

1. If you added the project to `spec.scope.projects`, remove it from that list
   (keeping `spec.scope: {projects: []}` if it was the last additional project)
   so the reconciler retires its Cluster Agent profiles over two clean runs.
2. Revoke the IAM bindings on the target project (or folder) so the Platform
   Agent and its scheduled audits stop querying it.
