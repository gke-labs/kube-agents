# Connection-state security

This document is the security contract for the local Kube Agents Console's
persisted connection lease. The implementation is
[`connection_persistence.py`](connection_persistence.py); general console setup
and behavior remain in [`README.md`](README.md).

## What is persisted

After an explicit Project and Cluster connection succeeds, the portal writes:

```text
~/.kube-agent/state/admin-portal-connection.json
```

`KUBE_AGENTS_ADMIN_CONNECTION_STATE` may override that path for a test or a
deployment. The JSON document contains only:

- a schema version;
- the launcher-verified gcloud account identifier;
- project ID, cluster name, location, and Kubernetes namespace;
- whether the target came from host-label discovery or manual selection; and
- the last successful verification time; and
- whether runtime use is verified or requires revalidation.

The account identifier and infrastructure names may be sensitive organizational
metadata. Treat the file as private even though it is not a credential.

The file never contains access or refresh tokens, service-account keys, API
keys, cookies, kubeconfig content, prompts, transcripts, logs, traces, or
Kubernetes workload data. Google Cloud credentials remain under gcloud's own
credential management. Kubernetes credentials are prepared in the portal
process's private temporary kubeconfig and removed when that process exits.

## Filesystem protections

The portal creates the state directory with mode `0700` when it does not exist.
It writes a temporary file in that directory, sets mode `0600`, flushes it,
atomically replaces the destination, and enforces mode `0600` on the result.

Loading fails closed when the file:

- is a symbolic link;
- is not owned by the portal process's effective user;
- grants any group or other permissions;
- exceeds the bounded state-file size;
- is malformed or uses an unsupported schema version;
- names an invalid project, cluster, location, or namespace; or
- belongs to a different launcher-verified gcloud account.

These controls protect against accidental disclosure and unsafe file
substitution across local accounts. They do not defend against a process already
running as the same Unix user; that process can also access the user's gcloud
configuration and is outside the local portal's trust boundary.

## Resume and revalidation

An arbitrary URL, configured project, or provisioning-state target cannot create
a connected session. Only a lease written after an explicit successful
connection can resume.

On browser reload or reopen, the portal resumes an exact account-bound lease and
immediately starts bounded, read-only live revalidation. A successful result
refreshes the timestamp and marks the lease verified. A failed result disconnects
the cluster and marks the retained target as requiring revalidation, which makes
both the Streamlit runtime pages and scoped API reject its use. A later portal
session can retry that exact target without another manual selection. The UI
reports the failed check, observed reason, and next action. An open session
repeats revalidation periodically while keeping Disconnect available.
Tabs with a persisted lease also reconcile its status and target. If another tab
disconnects, suspends, or replaces the lease, a stale tab detaches its runtime
state and cannot recreate the removed lease with a late background result.

Disconnecting the cluster or project deletes the lease. No credential is revoked
because the lease contains no credential; gcloud authentication has its own
lifecycle.

## Local verification

Inspect permissions without printing the metadata:

```bash
stat --format='mode=%a owner=%U size=%s path=%n' \
  "$HOME/.kube-agent/state/admin-portal-connection.json"
```

The expected mode is `600`, and the owner must be the user running
`scripts/admin_portal.sh`.

Inspect only the stored field names:

```bash
jq -r 'keys[]' "$HOME/.kube-agent/state/admin-portal-connection.json"
```

Use the UI's **Disconnect** action to remove the lease through the normal
lifecycle.
