<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Application-layer secrets encryption and Security Posture

The description and the gke-basics and gke-workload-security routing notes send these two flags
here.

**Secrets encryption (`--database-encryption-key`)** envelope-encrypts Secrets in etcd with a
Cloud KMS key. The key must be in the cluster's region (`KMS_LOCATION`; for a zonal cluster, the
region that contains its zone, since Cloud KMS has no zonal locations), and the GKE service agent
of the cluster's project (`CLUSTER_PROJECT_NUMBER`, not the key project's number) needs
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on it before the cluster can use it:

```bash
gcloud kms keys add-iam-policy-binding KEY_NAME \
  --keyring KEYRING_NAME --location KMS_LOCATION --project KMS_PROJECT_ID \
  --member serviceAccount:service-CLUSTER_PROJECT_NUMBER@container-engine-robot.iam.gserviceaccount.com \
  --role roles/cloudkms.cryptoKeyEncrypterDecrypter

gcloud container clusters update CLUSTER_NAME --location LOCATION \
  --database-encryption-key projects/KMS_PROJECT_ID/locations/KMS_LOCATION/keyRings/KEYRING_NAME/cryptoKeys/KEY_NAME

# CURRENT_STATE_ENCRYPTED once encryption completes (`state` is the requested setting, not the observed one)
gcloud container clusters describe CLUSTER_NAME --location LOCATION \
  --format="value(databaseEncryption.currentState)"
```

**Security Posture (`--security-posture`)** turns on configuration auditing (`standard`) and,
with `--workload-vulnerability-scanning`, OS vulnerability scanning of running workloads:

```bash
gcloud container clusters update CLUSTER_NAME --location LOCATION \
  --security-posture=standard --workload-vulnerability-scanning=standard
```
