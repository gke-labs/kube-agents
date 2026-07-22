---
title: Uninstall
description: Remove the Platform Agent, operator, and provisioned GCP resources.
---

The shipping teardown mirrors `./provision.sh` in reverse.

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

## Related work

An expanded uninstall + teardown guide is proposed in [PR #345](https://github.com/gke-labs/kube-agents/pull/345) with more detail on the cleanup steps for each integration.
