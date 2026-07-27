---
title: Uninstall
description: Remove the Platform Agent, operator, and provisioned GCP resources.
---

There are two levels of cleanup: removing just the Platform Agent (keeping the cluster and operator), or a full teardown of everything the provisioner created.

## Uninstall the Platform Agent only

Use this to remove the agent while leaving the GKE cluster and operator in place.

1. **Stop the heartbeat.** Delete or disable the recurring 1-minute cron in your agent harness so no new runs fire.
2. **Delete the `PlatformAgent` CR.**

   ```bash
   kubectl delete platformagent platform-agent -n kubeagents-system --ignore-not-found=true
   ```

   If deletion hangs on a controller finalizer (e.g. the operator or its webhook is offline), clear the finalizer and retry:

   ```bash
   kubectl patch platformagent platform-agent -n kubeagents-system \
     --type=merge -p '{"metadata":{"finalizers":null}}'
   ```

   **Note:** the `kubeagents.x-k8s.io/finalizer` finalizer is what deletes the agent's **cluster-scoped** RBAC — a ClusterRole and two ClusterRoleBindings that Kubernetes cannot garbage-collect via owner references. Bypassing it leaves these behind, so delete them manually (names are derived from the CR's namespace and name):

   ```bash
   kubectl delete clusterrolebinding \
     kubeagents:viewer:kubeagents-system:platform-agent \
     kubeagents:explorer:kubeagents-system:platform-agent --ignore-not-found=true
   kubectl delete clusterrole \
     kubeagents:explorer:kubeagents-system:platform-agent --ignore-not-found=true
   ```

3. **Delete the agent secrets.**

   ```bash
   kubectl delete secret platform-agent-secrets github-app-credentials \
     -n kubeagents-system --ignore-not-found=true
   ```

   (`github-app-credentials` only exists if you configured the GitHub integration.)

4. **Remove the workspace** — delete the `agents/platform` directory from your harness workspace if you installed it there.

Once the CR is gone, the operator's finalizer first removes the cluster-scoped RBAC (the ClusterRole and ClusterRoleBindings above), then Kubernetes garbage-collects the namespaced resources it owns — the agent's Deployment, Service, ServiceAccount, PersistentVolumeClaims, and ConfigMaps.

## Full teardown

```bash
cd k8s-operator/scripts
./teardown.sh
```

The script runs the `teardown_11_*.sh` through `teardown_01_*.sh` steps in order, undoing each provisioning step. It reads state from `vars.sh` (created during provisioning) so you don't need to re-answer prompts.

## Per-step teardown

You can also run individual `teardown_NN_*.sh` scripts to remove one layer at a time:

| Script                                   | Removes                                                         |
| ---------------------------------------- | --------------------------------------------------------------- |
| `teardown_11_deploy_inference_replay.sh` | Inference-replay proxy + PVC; restores original LiteLLM Service |
| `teardown_10_deploy_github_minter.sh`    | Minty deployment, GSAs, KMS resources                           |
| `teardown_09_deploy_litellm.sh`          | LiteLLM Gateway                                                 |
| `teardown_08_deploy_platform_agent.sh`   | `PlatformAgent` CR and rendered manifests                       |
| `teardown_07_gcp_k8s_secrets.sh`         | Kubernetes secrets in the target namespace                      |
| `teardown_06_slack.sh`                   | Slack tokens and state                                          |
| `teardown_05_gcp_gchat.sh`               | Google Chat Pub/Sub topic + subscription                        |
| `teardown_04_gcp_iam.sh`                 | GCP service accounts and Workload Identity bindings             |
| `teardown_03_gcp_gke_operator.sh`        | Operator manager deployment and CRDs                            |
| `teardown_02_gvisor_nodepool.sh`         | gVisor node pool only (optional)                                |
| `teardown_01_gcp_cluster.sh`             | GKE cluster and the local `vars.sh` state file                  |

Each script is idempotent — safe to re-run if it fails partway through.

## Where to go next

- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — what each `provision_NN_*` / `teardown_NN_*` step does.
- [Security & IAM](/kube-agents/reference/security-and-iam/) — the GCP service accounts and bindings removed by `teardown_04_gcp_iam.sh`.
