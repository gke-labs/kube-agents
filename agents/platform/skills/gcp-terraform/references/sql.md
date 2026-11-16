# google_sql_database_instance

From the `google` provider's source at 8.x (September 2026).

- **No `gcloud sql` read.** The command policy has no read for Cloud SQL,
  so the repository is the only record. An instance the repository does not
  describe: ask the user whether it exists, and for its settings, before an
  import or a create.
- **Forces replacement:** `name`, `region`, `master_instance_name` on a
  replica, `settings.collation`, `encryption_key_name`, unsetting
  `settings.ip_configuration.private_network`, and others the provider page
  marks.
- **Updatable in place:** `settings.tier`, backups
  (`settings.backup_configuration`), `settings.availability_type`, flags,
  maintenance window, and `database_version` for a major version upgrade
  (4.34.0 and later). Whether a tier or version change restarts the
  instance is not recorded here; say in the PR body that it may.
- **`deletion_protection` defaults to `true` (3.44.0 and later)**, checked
  at delete time against state, and `deletion_policy = "ABANDON"` gets past
  it. Never change either.
- **A read replica** is a second `google_sql_database_instance` with
  `master_instance_name` set to the primary's name (or a reference to it, in
  the repository's style), and `replica_configuration` only if the
  repository's other replicas carry it. Copy its `database_version` from the
  primary unless the user asks otherwise.
- **Secrets:** a `google_sql_user` password never goes into HCL; use the
  repository's existing reference or stop and ask.
