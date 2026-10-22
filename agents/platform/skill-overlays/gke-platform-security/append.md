<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Application-layer secrets encryption and Security Posture

The description and the gke-basics and gke-workload-security routing notes send these two flags
here.

**Secrets encryption (`--database-encryption-key`)** envelope-encrypts Secrets in etcd with a
Cloud KMS key. The key must be in the cluster's region (`KMS_LOCATION`; for a zonal cluster, the
region that contains its zone, since Cloud KMS has no zonal locations), and the GKE service agent
of the cluster's project (`CLUSTER_PROJECT_NUMBER`, not the key project's number) needs
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on it before the cluster can use it. Both go out as
Config Connector YAML through `gcp-config-connector`: the key's IAM binding for
`service-CLUSTER_PROJECT_NUMBER@container-engine-robot.iam.gserviceaccount.com`, and on the
cluster's `ContainerCluster` the key
(`projects/KMS_PROJECT_ID/locations/KMS_LOCATION/keyRings/KEYRING_NAME/cryptoKeys/KEY_NAME`) in
`spec.databaseEncryption.keyName` with `spec.databaseEncryption.state: ENCRYPTED`. Verify:

```bash
# CURRENT_STATE_ENCRYPTED once encryption completes (`state` is the requested setting, not the observed one)
gcloud container clusters describe CLUSTER_NAME --location LOCATION \
  --format="value(databaseEncryption.currentState)"
```

**Security Posture (`--security-posture`)** turns on configuration auditing (`standard`) and,
with `--workload-vulnerability-scanning`, OS vulnerability scanning of running workloads. Both
are changed as Config Connector YAML on the cluster's `ContainerCluster` through
`gcp-config-connector`: `spec.securityPostureConfig.mode: BASIC` and
`spec.securityPostureConfig.vulnerabilityMode: VULNERABILITY_BASIC` (Config Connector's values,
not gcloud's `standard`).
