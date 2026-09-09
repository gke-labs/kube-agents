# SQLInstance

`apiVersion: sql.cnrm.cloud.google.com/v1beta1`. Project comes from the
`cnrm.cloud.google.com/project-id` annotation (resource or namespace); there
is no `projectRef`. Immutable list from the reference page as published in
September 2026; `kubectl explain` on the customer's CRDs decides.

## The read you do not have

`gcloud sql instances describe` is not on the command policy's read
allowlist, so the in-cluster `SQLInstance` object is the only live state
available:

```bash
kubectl auth can-i get sqlinstances.sql.cnrm.cloud.google.com -n <namespace>
kubectl get sqlinstance <name> -n <namespace> -o yaml
```

When that is forbidden or the object is absent, the create-versus-acquire
question cannot be answered from here. Ask the user whether the instance
exists in Google Cloud and, for an acquisition, for the values of the
immutable fields below. Do not guess a database version or a region: a
mismatch fails the object after merge, and a Cloud SQL instance name cannot
be reused for about a week after deletion, so a wrong create is not cheap
to undo.

## Fields

- **Required:** `spec.settings`, `spec.settings.tier`.
- **Immutable (create-only):** `region`, `resourceID`, `cloneSource`,
  `settings.diskType`, `settings.collation`, `settings.timeZone`, and every
  field under `replicaConfiguration`.
- **Create-only in practice, though the CRD carries no `Immutable.` marker:**
  `masterInstanceRef`. Cloud SQL cannot repoint a replica or turn a primary
  into one; changing it on an existing object is a replacement.
- **Not immutable, but constrained:** `databaseVersion` (Cloud SQL supports
  in-place major-version upgrades; a replica must match its primary),
  `settings.tier` (an edit restarts the instance), `settings.diskSize` (only
  grows), `settings.availabilityType` (`REGIONAL` needs backups enabled, and
  binary logging on MySQL or point-in-time recovery on PostgreSQL).
- **Mutable:** `settings.backupConfiguration`, `settings.ipConfiguration`,
  `settings.databaseFlags`, `settings.insightsConfig`,
  `settings.maintenanceWindow`, `settings.deletionProtectionEnabled`,
  `settings.diskAutoresize`, `settings.userLabels`, `settings.edition`.

`settings.deletionProtectionEnabled: true` is Cloud SQL's own brake and is
separate from the `deletion-policy` annotation; set it on any primary you
create and leave it as found on one you acquire.

## Read replica

`spec.masterInstanceRef` makes an instance a replica. State:

- `databaseVersion` equal to the primary's.
- `region` — the primary's for an in-region replica, another for
  cross-region.
- `masterInstanceRef.name` when the primary is a `SQLInstance` object in the
  repository; otherwise `masterInstanceRef.external`, which the reference
  describes as the primary's selfLink. When the primary is a KCC object
  anywhere in the cluster, `status.selfLink` on it is that value; otherwise
  ask the user for it.
- `settings.tier` — the primary's unless the request says otherwise.
- `settings.ipConfiguration` mirroring the primary (private network, SSL
  mode); a replica on a different network is unreachable from the
  application.
- Do not set `settings.backupConfiguration` or
  `settings.availabilityType: REGIONAL` on a replica.

Preconditions the primary must already meet, or Cloud SQL rejects the
create and the object reports it in `status.conditions`: automated backups
enabled, and for MySQL `binaryLogEnabled: true`; a replica of a replica is
not allowed. Read these off the primary's manifest or object before writing
the replica, and state in the PR body that the replica is a second billed
instance at the stated tier.

### Create: a PostgreSQL read replica of a primary in the repository

```yaml
apiVersion: sql.cnrm.cloud.google.com/v1beta1
kind: SQLInstance
metadata:
  name: orders-db-replica-1
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  databaseVersion: POSTGRES_15
  region: us-central1
  instanceType: READ_REPLICA_INSTANCE
  masterInstanceRef:
    name: orders-db
  settings:
    tier: db-custom-2-7680
    ipConfiguration:
      ipv4Enabled: false
      privateNetworkRef:
        external: projects/acme-prod/global/networks/prod-vpc
      sslMode: ENCRYPTED_ONLY
```

## Acquire: an existing primary, to enable point-in-time recovery

The user confirmed the instance exists with `databaseVersion: POSTGRES_15`,
`region: us-central1`, `settings.tier: db-custom-4-15360`,
`settings.diskType: PD_SSD`.

```yaml
apiVersion: sql.cnrm.cloud.google.com/v1beta1
kind: SQLInstance
metadata:
  name: orders-db
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  resourceID: orders-db
  databaseVersion: POSTGRES_15
  region: us-central1
  settings:
    tier: db-custom-4-15360
    diskType: PD_SSD
    backupConfiguration:
      enabled: true
      pointInTimeRecoveryEnabled: true
```

`region`, `databaseVersion`, `tier` and `diskType` are stated so a
mismatch fails the object rather than adopting the wrong instance; the rest
of the live configuration stays externally managed.
