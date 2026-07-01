---
name: gke-backup-dr
description: Workflows for configuring Backup for GKE and disaster recovery.
---

# GKE Backup & Disaster Recovery Skill

This skill provides workflows for protecting your stateful workloads on GKE using Backup for GKE.

## Workflows

### 1. Enable Backup for GKE

Backup for GKE must be enabled on the cluster level.

**Command:**

```bash
gcloud container clusters update <cluster-name> \
    --enable-gke-backup \
    --region <region>
```

### 2. Create a Backup Plan

A Backup Plan defines what to back up, when, and for how long.

**Command to create a backup plan:**

```bash
gcloud container backup-restore backup-plans create <plan-name> \
    --cluster=<cluster-name> \
    --region=<region> \
    --retention-days=<days> \
    --cron-schedule="<cron-expression>" \
    --all-namespaces
```

> [!NOTE]
> You can replace `--all-namespaces` with `--included-namespaces=<namespace1>,<namespace2>` to back up specific namespaces instead of all of them.

**Encryption Note**: You can specify a Customer-Managed Encryption Key (CMEK) to encrypt backups. Add `--backup-encryption-key=<key-resource-name>` to the `create` command.

### 3. Create a Manual Backup

Trigger a backup immediately outside the schedule.

**Command:**

```bash
gcloud container backup-restore backups create <backup-name> \
    --backup-plan=<plan-name> \
    --region=<region>
```

### 4. Restore from Backup

Restore a workload or cluster from a backup.

**Command to create a restore plan:**

```bash
gcloud container backup-restore restore-plans create <restore-plan-name> \
    --cluster=<target-cluster-name> \
    --region=<region> \
    --backup-plan=<source-backup-plan-name> \
    --cluster-resource-conflict-policy=USE_EXISTING_VERSION \
    --namespaced-resource-restore-mode=FAIL_ON_CONFLICT
```

**Execute the restore:**

```bash
gcloud container backup-restore restores create <restore-name> \
    --restore-plan=<restore-plan-name> \
    --backup=<backup-name> \
    --region=<region>
```

## Best Practices

1. **Automate Backups**: Always use a cron schedule for production workloads.
2. **Test Restores**: Regularly test restoring backups to a separate namespace or cluster to ensure data integrity.
3. **Cross-Region DR**: Consider storing backups in a different region or setting up a cross-region restore plan for disaster recovery.
4. **Secure Backups**: Use Customer-Managed Encryption Keys (CMEK) to encrypt backups for compliance and security.
