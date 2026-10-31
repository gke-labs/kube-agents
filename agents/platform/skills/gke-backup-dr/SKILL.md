---
name: gke-backup-dr
description: >-
  Configures Backup for GKE: the BackupRestore cluster addon, BackupPlan and
  RestorePlan resources, restore workflows, and CMEK-encrypted backups. Use
  for backup policies, disaster recovery, or GKE cluster restores. Don't use
  for database backups.
metadata:
  version: "1.0.0"
  category: Storage
---

# GKE Backup & Disaster Recovery

Protects stateful GKE workloads using Backup for GKE. Backup for GKE can capture
both Kubernetes resource metadata (manifests, configurations, and secrets) and
the underlying persistent volume (PV) data — but volume data and secrets are
**only** captured when the backup plan explicitly enables them (see the flags
below).

## CLI Reference

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

Each change below is proposed as Config Connector YAML through `gcp-config-connector`:

-   **BackupRestore addon** (slow cluster-level update):
    `spec.addonsConfig.gkeBackupAgentConfig.enabled: true` on the cluster's
    `ContainerCluster`.
-   **Backup plan** `{plan_name}` for
    `projects/{project_id}/locations/{location}/clusters/{cluster_name}`: all
    namespaces, volume data and secrets included, retained `{days}` days, on
    the cron schedule `{cron}`.
-   **Restore plan** `{restore_plan_name}` targeting
    `projects/{project_id}/locations/{location}/clusters/{target_cluster_name}`
    from the backup plan `{source_backup_plan_name}`: all namespaces, volume data
    restored from the backup, the cluster-resource conflict policy that keeps
    existing versions, and the namespaced-resource restore mode that fails on
    conflict (the safe defaults in Restore Safety below).
-   **A manual backup** `{backup_name}` and **a restore** `{restore_name}` of it
    also go through `gcp-config-connector`; a restore needs the confirmation in
    Restore Safety below.

The backup plan, restore plan, backup and restore are Backup for GKE resources,
not `ContainerCluster` fields. If `gcp-config-connector` has no resource for one
of them, it has no declarative path in a Config Connector repository, and the
agent blocks rather than inventing a field.

Verify the restore status:

```bash
gcloud beta container backup-restore restores describe {restore_name} \
  --restore-plan={restore_plan_name} --location={location}
```

> [!WARNING] **Volume data inclusion (`--include-volume-data`) and secret
> inclusion (`--include-secrets`) BOTH DEFAULT TO FALSE.** If you omit them, the
> backup plan silently produces **config-only backups** with no persistent volume
> snapshots and no Secrets. Always enable both explicitly in the backup plan when
> the goal is full workload protection.

Notes:

-   The `backup-restore` command group is on the `gcloud beta` track
    (`gcloud beta container backup-restore ...`), which this image's gcloud
    already includes. Do not run `gcloud components install`: the credential
    proxy refuses it.
-   `--cluster` requires the full resource path
    `projects/{project_id}/locations/{location}/clusters/{cluster_name}` (or
    `projects/{project_id}/zones/{zone}/clusters/{cluster_name}` for zonal
    clusters), not a bare cluster name.
-   Restore plans require exactly one namespaced-resource scope flag:
    `--all-namespaces`, `--selected-namespaces={ns1},{ns2}`,
    `--excluded-namespaces=...`, `--selected-applications=...`, or
    `--no-namespaces`.

## Restore Safety (CRITICAL)

A restore writes into a **live cluster** and, depending on the conflict policy,
can overwrite or delete existing resources:

-   `--cluster-resource-conflict-policy=use-existing-version` keeps existing
    cluster-scoped resources (safe default); `use-backup-version` **deletes**
    the existing version first — deleting a CRD deletes all of its CRs.
-   `--namespaced-resource-restore-mode=fail-on-conflict` aborts on any conflict
    (safe default); `merge-skip-on-conflict` skips conflicting resources;
    `merge-replace-on-conflict` and `merge-replace-volume-on-conflict`
    **overwrite** existing resources or volumes; `delete-and-restore` **deletes
    entire conflicting namespaces** (and all resources in them) before
    restoring.

**Rules:**

1.  Validate the restore in a non-production target cluster first.
2.  Prefer the safe defaults (`use-existing-version` + `fail-on-conflict`)
    unless the user explicitly needs to revert live resources.
3.  **Always obtain explicit user confirmation before executing a restore into a
    production cluster**, and state which conflict policy is in effect and what
    it may overwrite or delete.

## Best Practices

1.  **CMEK Encryption**: Encrypt backup plans using Customer-Managed Encryption
    Keys:
    `--encryption-key=projects/{project_id}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}`.
2.  **Scope**: Prefer backing up specific namespaces rather than the entire
    cluster: `--selected-namespaces={ns1},{ns2}` (instead of
    `--all-namespaces`).
3.  **Application Consistency**: Recommend quiescing the database or pausing
    application writes (e.g. using pre-backup hooks or database-specific tools)
    prior to backups to ensure data integrity.
4.  **CSI Volume Snapshots**: Ensure that stateful backups utilize GKE's CSI
    (Container Storage Interface) driver for volume snapshots to capture
    persistent volume data.
5.  **Service Terminology**: Always explicitly refer to the service as **Backup
    for GKE** in your response. This distinguishes it from the broader (but
    complementary) Google Cloud **Backup and Disaster Recovery (DR)
    Service**, as **Backup for GKE** is built specifically for GKE.

## Golden Path Backup Defaults

The recommended production golden path configuration for Backup for GKE:

-   **Addon**: BackupRestore addon enabled
    (`--update-addons=BackupRestore=ENABLED`).
-   **Volume Inclusion**: `--include-volume-data` explicitly passed (enabled,
    since the service default is false).
-   **Secret Inclusion**: `--include-secrets` explicitly passed (enabled, since
    the service default is false).
-   **Retention**: Defined retention period (e.g. 30 days via
    `--backup-retain-days=30`).
-   **Encryption**: CMEK enabled (`--encryption-key=...`).

## Recent Changes

-   **Cross-project backup and restore (GA)**: Backup plans can store backups in
    a different project than the source cluster, and restore plans can target
    clusters in a third project. Enables centralized backup projects (with
    immutability/retention managed by a platform team) and cross-project
    environment seeding without granting access to the source project.
-   **Pricing change (effective 2026-03-02)**: The backup management fee moved
    from **pod-based** to **NAMESPACE-based** pricing — charged per non-system
    namespace in the most recent successful backup of each plan (system
    namespaces like `kube-system` are excluded). Existing committed use discount
    (CUD) holders keep pod-based management pricing until their commitment ends;
    everyone else moves to the new model. See
    https://cloud.google.com/products/backup-for-gke/pricing-changes.
-   **Smart Scheduling**: RPO-driven backup scheduling as an alternative to
    fixed cron schedules — pass `--target-rpo-minutes={minutes}` instead of
    `--cron-schedule` when creating the backup plan (optionally with RPO
    exclusion windows via `--exclusion-windows-file`).
-   **Hyperdisk support**: Backup and restore of **Hyperdisk ML** and
    **Hyperdisk Balanced High Availability** volumes is supported on GKE
    clusters running **1.33.1-gke.1959000 and later** (Hyperdisk throughput,
    extreme, and balanced types are also supported).

## Troubleshooting & Common Pitfalls (CRITICAL)

> [!IMPORTANT] **Slow Operations**: Enabling the BackupRestore addon
> (`--update-addons=BackupRestore=ENABLED`) triggers a slow Google Cloud control
> plane cluster update that takes several minutes. * **Rule**: **Do not run a
> terminal loop waiting for the GKE Backup addon to become active.** *
> **Action**: Propose the addon change as a pull request, explain that the
> operation will proceed in the background once it applies, and immediately
> proceed to write the backup plan configs. Do not block.
