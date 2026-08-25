# GKE Backup Plan Module

Reusable Terraform module for provisioning a [Backup for GKE](https://cloud.google.com/kubernetes-engine/docs/add-on/backup-for-gke) `BackupPlan` that snapshots the namespace kube-agents runs in, on a schedule.

The plan is only half of the feature: the Backup for GKE **agent** must also be enabled on the target cluster. The `gke-cluster` module does that by default (`enable_backup_agent = true`), and the project needs `gkebackup.googleapis.com`.

> **Backups include Kubernetes Secrets and persistent volume data** by default, matching the provisioning script. That means the agent's credentials Secret is inside every backup — restrict backup and restore IAM to administrators who are already allowed to read those credentials, and consider `encryption_key` for CMEK.

> **Cost.** Backup for GKE bills per backed-up pod and per gigabyte of volume snapshot storage. Nothing is charged until a plan exists, which is why both install paths leave it opt-in.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`, when
`ENABLE_GKE_BACKUP_PLAN=true`) uses for the scheduled backups.

The defaults: the name `<cluster_name>-backup-plan`, a `0 2 * * *` schedule, 30-day retention, the `kubeagents-system` namespace, secrets and volume data included, and the schedule un-paused.

## Teardown is not symmetric

**A BackupPlan cannot be deleted while it still owns backups.** Terraform cannot
purge them itself, so
once any backup has been taken, `terraform destroy` — or flipping
`enable_gke_backup_plan` back to `false`, or anything else that replaces the plan — fails on
this resource with the API refusing the delete. The apply stops there, after whatever was
ordered before it has already gone.

Delete the backups first, then destroy:

```bash
PLAN=<cluster_name>-backup-plan
for backup in $(gcloud beta container backup-restore backups list \
      --backup-plan="$PLAN" --location="$LOCATION" --project="$PROJECT_ID" \
      --format="value(name)"); do
  gcloud beta container backup-restore backups delete "$backup" \
      --backup-plan="$PLAN" --location="$LOCATION" --project="$PROJECT_ID" --quiet
done
```

The full-install composition's `lifecycle.sh destroy` does exactly this purge for you
(`purge_backups`), permanently — read it before running it on an installation whose
backups matter. To keep the backups, remove the module from state
(`terraform state rm module.gke_backup_plan`) rather than destroying it.

## Usage

```hcl
module "gke_backup_plan" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-backup-plan?ref=vX.Y.Z"
  project_id   = "my-gcp-project"
  cluster_name = "platform-agent-host"
  location     = "us-central1"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
