---
title: Multiple GCP projects
description: How to grant a single kube-agents installation access to manage, debug, and audit GKE clusters and GCP infrastructure across multiple projects.
sidebar:
  order: 8
---

A single `kube-agents` installation in one host project can manage GKE clusters,
workloads, and GCP infrastructure across any number of separate GCP projects —
whether or not those projects belong to a shared GKE Hub fleet.

What the agent needs in another project is **read-only IAM roles for its Google
Service Account** there, and the project's GCP APIs enabled. On an install made
with `install.sh`, list the project in `SCOPE_PROJECTS` in `install.env`: the
installer then binds the read roles in that project and declares it in
`spec.scope`, so Terraform owns the bindings and revokes them when the project is
removed (see [Installer-managed installs](#installer-managed-installs)). With the
drift detector on, which it is unless `ENABLE_DRIFT_DETECTOR=false`, the same apply
also creates a Log Router sink in that project, named after the install's drift sink
with the management project appended, that exports the project's GKE admin-activity
audit records into the management project's drift topic, so a change made by hand
on a cluster there is reported like one on the management cluster; besides setting IAM
policy there, the identity running the apply needs to read the project, mint its Logging
service agent and create a sink (`roles/owner` carries all of them).
`SCOPE_FOLDERS` and `SCOPE_ORGANIZATIONS` bind the roles on a folder or
organisation, whose bindings every project beneath it inherits; its members get
no drift sink: their clusters are discovered at runtime and their changes are
not exported, whether or not the scoped service account pool lists them.
`SCOPE_SHARED_VPC_HOSTS` and `SCOPE_METRICS_SCOPES` bind the roles, and create
the sink, in every project a Shared VPC host or Metrics Scope resolves to. Without any of these
the installer binds the service account in the host project alone, and the
grants in other projects are yours to make by hand, as the steps below
describe.

Once IAM access is granted, the two layers of the harness discover projects
differently:

- **Platform Agent operations, governance audits, and upgrade checks (IAM
  only):** The Platform Agent can immediately inspect resources in the target
  project via `gcloud`, and scheduled governance audits and fleet upgrade checks
  automatically discover every project the agent's identity can list
  (`gcloud projects list` unioned with the host project), so the IAM grant is
  what sets their scope. Audits that target GKE clusters name each one
  `<project>/<location>/<name>`, and the project-level audits name their targets
  `project/<id>` (and subnets `<project>/<region>/<subnet>`), so identically named
  resources in different projects never collide. If the project listing fails or
  a project cannot be read (or an audit run is narrowed to named projects), the
  run still covers what it can reach and reports the result as partial. A
  project whose own relevant API is disabled counts as empty.
- **Cluster Agent profiles, specialist routing, and Kubernetes event watching
  (`spec.scope` or chat onboarding):** Unlike the governance audits, the hourly
  Cluster Agent reconciler (`cluster_agent_reconcile.py`) does **not** enumerate
  every project from `gcloud projects list` — by default it lists only the host
  project. For clusters in another project to get dedicated
  [Cluster Agent](/kube-agents/concepts/cluster-agents/) profiles (`cluster-*`),
  appear on the Planning Agent's specialist roster, and have their Kubernetes
  warning events watched by `k8s-event-watcher`, you either declare the project
  in [`spec.scope.projects`](/kube-agents/operator/platformagent-crd/#specscope)
  or ask the agent in chat to onboard specific clusters (`manage-cluster`).

## Installer-managed installs

On an install made with `install.sh`, `spec.scope` is declared in `install.env`
and never by editing the `PlatformAgent`: a full upgrade refuses to apply over a
`spec.scope` edited by hand until `install.env` records it. Add the project to
`SCOPE_PROJECTS` (space- or comma-separated) and run a full upgrade:

```bash
# install.env
SCOPE_PROJECTS=<OTHER_PROJECT_ID>
```

```bash
./upgrade.sh --upgrade-mode=full
```

That one apply binds the read roles in the project, creates its drift audit-log
sink when the detector is on, and adds it to
`spec.scope.projects`, which covers Steps 2 and 4 below; Step 3 (enabling the
APIs) and the Verify section still apply. `SCOPE_EXCLUDE_PROJECTS` and
`SCOPE_EXCLUDE_CLUSTERS` (`project/location/cluster`) declare exclusions the
same way; a project an exact `SCOPE_EXCLUDE_PROJECTS` entry names gets no drift
sink, while one a glob matches is excluded at runtime and still exported. To keep
a project in the scope and out of the export, add
`TF_VAR_drift_pubsub_source_exclude_projects=<project id>` to `install.env`: the
lever for a project where the applying identity cannot mint the Logging service
agent or create the sink, which otherwise stops the apply at that project's
resources. The field and every key that sets it are described under
[`spec.scope`](/kube-agents/operator/platformagent-crd/#specscope).

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

Declare the target project in [`spec.scope.projects`](/kube-agents/operator/platformagent-crd/#specscope)
so the hourly reconciler automatically creates and maintains a
`cluster-<project>-<cluster>-<location>` profile for every GKE cluster in it.
On an install made with `install.sh`, do this through `SCOPE_PROJECTS`
([Installer-managed installs](#installer-managed-installs)), not with
`kubectl`.

On an install you manage with Helm or `kubectl` directly, append the project to
the existing list with a JSON patch. Do not use a merge patch with a one-element
list: a merge patch replaces the whole array, and the reconciler retires every
project that drops out of it, deleting their profiles two clean runs later.

```bash
kubectl patch platformagent platform-agent -n kubeagents-system \
  --type=json \
  -p '[{"op":"add","path":"/spec/scope/projects/-","value":"<OTHER_PROJECT_ID>"}]'
```

The `add` operation needs `spec.scope.projects` to exist already. If
`kubectl get platformagent platform-agent -n kubeagents-system -o jsonpath='{.spec.scope.projects}'`
prints nothing, the list is absent and the first project can be set with
`--type=merge -p '{"spec":{"scope":{"projects":["<OTHER_PROJECT_ID>"]}}}'`.

A project added on the `PlatformAgent` alone gets no drift audit-log sink: only
the Terraform composition creates one, so on such an install changes made by hand
on the project's clusters are not reported.

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
`fleet_scope.json` to confirm the project outcome is `ok` and its `cluster-*`
profiles are registered. The gateway container already carries the install's
`HERMES_HOME`, so the command reads it rather than assuming `/opt/data`:

```bash
kubectl exec -i deployment/platform-agent-gateway -n kubeagents-system \
  -c platform-agent -- bash -lc '
    /opt/hermes/.venv/bin/python3 "$HERMES_HOME/scripts/cluster_agent_reconcile.py" --require-create-pass &&
    cat "$HERMES_HOME/fleet_scope.json" &&
    /opt/hermes/.venv/bin/hermes profile list
  '
```

`--require-create-pass` stops the chain before it prints a snapshot this run
did not write. Exit 4 means the hourly reconcile or the bootstrap gate holds the
lock: retry in a minute. Exit 3 means the run could not list the host project's
clusters, every create failed, or the run aborted; the log line above it names
which.

## Removing a project

On an install made with `install.sh`:

1. Remove the project from `SCOPE_PROJECTS` and add any chat-onboarded cluster
   in it to `SCOPE_EXCLUDE_CLUSTERS` (`project/location/cluster`), then run
   `./upgrade.sh --upgrade-mode=full`. The apply revokes the read roles the
   installer bound there and deletes the project's drift audit-log sink, waiting
   two minutes by default between the sink and its publish grant so that Cloud Logging has
   stopped exporting before the grant goes, and the reconciler retires the
   project's Cluster Agent profiles over two clean runs; an excluded cluster
   loses its profile on the next run.
2. Revoke any binding you made by hand. A folder-level grant cannot be revoked
   for one project alone; move the project out of the folder or grant per
   project instead.

On an install you manage with Helm or `kubectl` directly:

1. If you added the project to `spec.scope.projects`, remove that entry with a
   JSON patch `remove` operation on its index (keeping `spec.scope: {projects: []}`
   if it was the last additional project) so the reconciler retires its Cluster
   Agent profiles over two clean runs.
2. If you onboarded clusters in chat, add each one to
   [`spec.scope.exclude.clusters`](/kube-agents/operator/platformagent-crd/#specscope)
   (`projectId`, `location`, `clusterName`). A profile the scope never
   produced is kept (listed under `unmanaged` in `fleet_scope.json`), so
   dropping the project alone leaves it in place; an excluded cluster loses its
   profile on the next run.
3. Revoke the IAM bindings on the target project so the Platform Agent and its
   scheduled audits stop querying it. A folder-level grant cannot be revoked for
   one project alone; move the project out of the folder or grant per project
   instead. A drift audit-log sink the composition created in the project is
   Terraform's and leaves when the project leaves the composition's `scope`, not
   with an edit here.
