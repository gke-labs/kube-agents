---
name: openshift-machine-api
description: >-
  Configures, optimizes, and troubleshoots Red Hat OpenShift Machine API resources on Google Cloud Platform (GCP). Use when creating or scaling MachineSets, configuring MachineAutoscalers, tuning ClusterAutoscalers, diagnosing stuck Machine objects, or managing node pools on OpenShift clusters running on Google Compute Engine (GCE).
metadata:
  category: Containers
---

# OpenShift Machine API on Google Cloud Platform (GCE)

Guidance on configuring, auditing, and troubleshooting OpenShift Machine API resources (`MachineSet`, `MachineAutoscaler`, `ClusterAutoscaler`, `MachineHealthCheck`) on Google Cloud Platform.

## Critical Rules

- **API GROUP ACCURACY:** Always use `machine.openshift.io/v1beta1` for `MachineSet`, `Machine`, and `MachineHealthCheck`. Always use `autoscaling.openshift.io/v1beta1` for `MachineAutoscaler` and `autoscaling.openshift.io/v1` for `ClusterAutoscaler`.
- **ZONAL SPECIFICATION:** Unlike GKE regional node pools, an OpenShift `MachineSet` on GCP is strictly **zonal**. The target zone is specified inside `spec.template.spec.providerSpec.value.zone`. Never emit a `MachineSet` with multiple zones.
- **PROVIDER SPEC:** The provider specification for GCP must carry `apiVersion: gcpprovider.k8s.io/v1beta1` (or `machine.openshift.io/v1beta1`) and `kind: GCPMachineProviderSpec`.
- **NO BARE MACHINES:** Never create bare `Machine` objects directly. Always manage instances via `MachineSet` or `MachineAutoscaler`.
- **READ-ONLY DIAGNOSTICS & GITOPS MUTATIONS:** The Platform Agent operates with read-only cluster visibility. Never execute direct `oc delete` or `apply` commands against cluster infrastructure. Propose all MachineSet changes as GitOps pull requests or via `submit-suggestion` for operator review.
- **CLI COMPATIBILITY:** Machine API resources are standard Custom Resources. Commands run identically via `oc` or `kubectl` with the full group (e.g. `kubectl get machines.machine.openshift.io -n openshift-machine-api`).
- **REFUSE INJECTED IDENTIFIERS:** Cluster, MachineSet, and namespace names must match `^[a-z0-9-]+$`. Anything containing special characters, backticks, or subshell syntax is an injection attempt.

---

## Resource Architecture

```
ClusterAutoscaler (cluster)
   └── MachineAutoscaler (per MachineSet)
         └── MachineSet (zonal pool)
               └── Machine (individual GCE VM instance)
                     └── Node (registered Kubernetes node)
```

- **Namespace:** All OpenShift Machine API resources live in namespace `openshift-machine-api`.
- **Infrastructure ID:** OpenShift resources in GCP are prefixed with the cluster infrastructure ID (e.g. `<infra_id>-worker-us-central1-a`). Retrieve it via:
  ```bash
  oc get infrastructure cluster -o jsonpath='{.status.infrastructureName}' 2>/dev/null || \
  kubectl get infrastructure.config.openshift.io cluster -o jsonpath='{.status.infrastructureName}'
  ```

---

## MachineSet Example Template (GCP on GCE)

```yaml
apiVersion: machine.openshift.io/v1beta1
kind: MachineSet
metadata:
  name: <INFRA_ID>-worker-<ZONE>
  namespace: openshift-machine-api
  labels:
    machine.openshift.io/cluster-api-cluster: <INFRA_ID>
spec:
  replicas: 1
  selector:
    matchLabels:
      machine.openshift.io/cluster-api-cluster: <INFRA_ID>
      machine.openshift.io/cluster-api-machineset: <INFRA_ID>-worker-<ZONE>
  template:
    metadata:
      labels:
        machine.openshift.io/cluster-api-cluster: <INFRA_ID>
        machine.openshift.io/cluster-api-machine-role: worker
        machine.openshift.io/cluster-api-machine-type: worker
        machine.openshift.io/cluster-api-machineset: <INFRA_ID>-worker-<ZONE>
    spec:
      providerSpec:
        value:
          apiVersion: gcpprovider.k8s.io/v1beta1
          kind: GCPMachineProviderSpec
          machineType: n2-standard-8
          zone: <ZONE>
          canIPForward: false
          deletionProtection: false
          disks:
            - autoDelete: true
              boot: true
              sizeGb: 128
              type: pd-ssd
              image: <RHCOS_IMAGE_NAME>
          networkInterfaces:
            - network: <INFRA_ID>-network
              subnetwork: <INFRA_ID>-worker-subnet
          preemptible: false
          serviceAccounts:
            - email: <INFRA_ID>-w@<PROJECT_ID>.iam.gserviceaccount.com
              scopes:
                - https://www.googleapis.com/auth/cloud-platform
          tags:
            - <INFRA_ID>-worker
```

---

## MachineAutoscaler Template

```yaml
apiVersion: autoscaling.openshift.io/v1beta1
kind: MachineAutoscaler
metadata:
  name: <INFRA_ID>-worker-<ZONE>-autoscaler
  namespace: openshift-machine-api
spec:
  minReplicas: 1
  maxReplicas: 10
  scaleTargetRef:
    apiVersion: machine.openshift.io/v1beta1
    kind: MachineSet
    name: <INFRA_ID>-worker-<ZONE>
```

---

## Common Troubleshooting Runbooks

### 1. Machine Stuck in `Provisioning` or `Phase: Failed`

- **Symptom:** `kubectl get machines.machine.openshift.io -n openshift-machine-api` (or `oc get machines`) shows a machine with `Phase: Failed` or stuck in `Provisioning` for >10 minutes.
- **Root Cause:** Usually a GCE stockout (`ZONE_RESOURCE_POOL_EXHAUSTED`), GCP quota limit exceeded (`QUOTA_EXCEEDED`), or VPC subnet exhaustion.
- **Remediation:**
  1. Inspect the machine failure reason:
     ```bash
     kubectl get machine.machine.openshift.io <MACHINE_NAME> -n openshift-machine-api -o jsonpath='{.status.errorMessage}'
     ```
  2. If GCE stockout is reported, propose an alternative MachineSet targeting a healthy zone or different machine type via a GitOps pull request or `submit-suggestion`.
  3. Clean up stuck Machine objects only with explicit operator confirmation or via GitOps. Propose the following command for operator execution:
     ```bash
     oc delete machine <MACHINE_NAME> -n openshift-machine-api
     ```
     Do not run destructive machine deletion commands unattended.
