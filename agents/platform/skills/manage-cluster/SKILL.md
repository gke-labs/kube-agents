---
name: manage-cluster
description: Bring an existing GKE cluster under management on user request (e.g. "manage my cluster <name> in <location>") by creating its Cluster Agent profile, or take one out of management by excluding it. Use whenever a user asks to manage/onboard/watch a specific existing cluster, or to stop managing, exclude or offboard one.
---

# Manage Cluster Skill

When a user asks you to **manage** (onboard / start watching) a specific existing GKE cluster — e.g. _"manage my cluster `payments-prod` in `us-central1`"_ — bring it under management by creating its **Cluster Agent profile**. After this, the cluster gets a per-cluster agent and is delegable via the kanban board.

This is the explicit, user-driven counterpart to onboarding-time creation (`gke-cluster-creation`). It is safe to run repeatedly (idempotent).

## Steps

1. **Gather the target.** You need `project`, `cluster`, and `location`.
   - Use any values the user gave. If the user omits the **project**, resolve it:
     - default to the platform's active project: `gcloud config get-value project`; then
     - confirm the cluster exists in that project (next step). If it isn't there, or the name is ambiguous, run `gcloud container clusters list --format="value(name,location,resourceLabels)"` (optionally `--project <p>`) to locate it, and ask the user only if still ambiguous.

2. **Verify the cluster exists** before creating a profile (clean error instead of a half-scaffold):
   - `verify_gke_cluster` (platform GKE tool) or `gcloud container clusters describe <cluster> --location <location> --project <project> --format="value(status)"`.
   - If it does not exist, tell the user and stop.

3. **Create the profile:**

   ```bash
   python3 /opt/data/scripts/cluster_agent_profile.py create \
     --project "<project>" --cluster "<cluster>" --location "<location>"
   ```

   This scaffolds the Cluster Agent profile home on the data PVC, pins a read-only `KUBECONFIG` to that cluster, stamps its `cluster_identity` into the profile config, and prints the profile name. It is idempotent — re-managing an already-managed cluster is a safe no-op.

4. **Confirm to the user.** Report that `<cluster>` (`<project>/<location>`) is now managed: it has a Cluster Agent and is delegable (kanban).

> This step gives the cluster an agent **immediately**. Even without it, the `cluster-agent-reconcile` cron automatically gives every cluster in every project in scope — including the management cluster where kube-agents runs — an agent on its next run; this skill just does it now.

## Stop managing

Because reconciliation manages **all** clusters in every project in scope, deleting a still-existing cluster's profile alone won't stick — the next reconcile recreates it. To stop managing a cluster:

- **Tear down the cluster** (see `gke-cluster-creation`); reconcile prunes its profile automatically.
- Or **exclude it**: declare the cluster as an exclusion where the install's scope is declared, which depends on how the `PlatformAgent` is applied. Take that from the request, or ask; the agent cannot read the Helm release record. On an install made with `install.sh` and upgraded with `upgrade.sh` (an `install.env` exists; the Terraform composition applies the chart from it), have the user add `project/location/cluster` to `SCOPE_EXCLUDE_CLUSTERS` in `install.env` and run `./upgrade.sh --upgrade-mode=full`, which renders the exclusion into `spec.scope.exclude.clusters` and the reconcile prunes the profile on its next run. Do not route them to edit the `PlatformAgent` or the chart values by hand there. A CR edit takes effect at the next reconcile, but the next full upgrade refuses to apply over a `spec.scope` that `install.env` does not record, and prints the `SCOPE_*` lines that would; a `helm upgrade` by hand is not refused, the next full upgrade renders `install.env`'s scope over it and the profile comes back. On a chart installed with `helm install` directly, the exclusion is the value `platformAgent.scope.exclude.clusters` and a `helm upgrade`; on the composition applied directly, `scope.exclude.clusters` in its tfvars. On a hand-applied `PlatformAgent` (no `meta.helm.sh/release-name` annotation: `kubectl get platformagent -n kubeagents-system -o jsonpath='{.items[0].metadata.annotations.meta\.helm\.sh/release-name}'`, the install's namespace), add an entry `{projectId, location, clusterName}` to `spec.scope.exclude.clusters` on it (the operator delivers it to the reconcile, which prunes the profile on its next run). Fleet answers and the per-cluster audits leave an excluded cluster out too, not only the profile. The older `RECONCILE_EXCLUDE` bare-name variable on the `cluster-agent-reconcile` cron still works for one release.

## Notes

- Requires the cluster to be reachable by the platform's credentials (the create step runs `gcloud container clusters get-credentials`). If that fails, surface the error — the user may need to grant access.
- The Cluster Agent is **read-only**; managing a cluster does not grant any mutation ability. Remediation still flows through the Platform Agent's GitOps write path.
