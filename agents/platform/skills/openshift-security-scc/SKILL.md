---
name: openshift-security-scc
metadata:
  category: Security
description: >-
  Audits, inspects, and manages OpenShift SecurityContextConstraints (SCCs),
  ServiceAccount security permissions, and pod admission security contexts. Use
  when troubleshooting pod admission failures on OpenShift (e.g., unable to
  validate against any security context constraint), configuring non-root or
  arbitrary UID execution, granting SCCs to ServiceAccounts (nonroot, anyuid,
  privileged), or inspecting namespace UID ranges. Don't use for standard GCP IAM
  roles or cluster-wide network policies.
---

# OpenShift SecurityContextConstraints (SCC) Skill

This skill provides workflows for auditing, configuring, and troubleshooting
SecurityContextConstraints (SCCs) on Red Hat OpenShift clusters.

OpenShift uses SCCs to govern what actions pods can perform and what system
resources they can access, acting as an admission controller comparable to
Kubernetes Pod Security Standards but with granular per-ServiceAccount bindings.

## Critical Rules

- **READ-ONLY DIAGNOSTICS & PRIVILEGE ELEVATION:** The Platform Agent operates with read-only cluster visibility. When an admission rejection is diagnosed, recommend the minimal-privilege SCC grant (`nonroot` over `anyuid`, `anyuid` over `privileged`). Present SCC grants as declarative RoleBinding manifests or propose the `oc adm policy` command via `submit-suggestion` for explicit operator approval.
- **CLI COMPATIBILITY:** Namespace UID annotations, pod events, and admitted pod annotations are queryable via both `kubectl` and `oc`.
- **DYNAMIC UID INJECTION:** Always recommend omitting explicit `runAsUser` and `fsGroup` fields from manifests when deploying to OpenShift, allowing the admission controller to dynamically assign UIDs from the namespace's allocated range.

## Core Concepts & Standard SCCs

Every pod admitted to OpenShift is matched against an available SCC:

| SCC Name | Description | Default Use Case |
| :--- | :--- | :--- |
| `restricted-v2` | Default for user workloads. Disallows root (`runAsUser: MustRunAsRange`), drops all capabilities, requires dynamic namespace UID. | Production non-root microservices |
| `nonroot` | Allows any non-root UID (e.g. fixed UID `10000`, `1000`), requires `runAsNonRoot: true`. | Third-party container images with baked unprivileged UIDs |
| `anyuid` | Allows arbitrary UIDs including root (`runAsUser: 0`), but restricts host access and privileges. | Workloads requiring root startup or legacy containers |
| `hostmount-anyuid` | Allows host volume mounts and arbitrary UIDs. | Logging agents, local storage operators |
| `privileged` | Unrestricted execution (root, host network, host PID, host IPC, all capabilities). | Storage drivers, CNI daemons, virtualization |

---

## Workflows

### 1. Diagnose Pod Admission SCC Failures

When a pod or deployment is rejected during admission:

```bash
# Check deployment / replicaset events for admission denial
oc get events -n {namespace} --field-selector reason=FailedCreate --sort-by='.metadata.creationTimestamp'

# Check the namespace's allocated UID range
oc get ns {namespace} -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}{"\n"}'
```

Common admission error:
`pods "app-xyz" is forbidden: unable to validate against any security context constraint: [provider "restricted-v2": ... runAsUser: Invalid value: 10000: is not an allowed group]`

**Diagnosis:**
The container specifies a fixed UID (e.g. `runAsUser: 10000`), but the namespace enforces `restricted-v2` with an auto-allocated high UID range (e.g. `1000670000/10000`).

---

### 2. Grant an SCC to a Workload ServiceAccount

To permit a workload to run with a specific security posture without modifying cluster-wide defaults:

```bash
# Permitting baked unprivileged UIDs (e.g. UID 10000 or 1000)
oc adm policy add-scc-to-user nonroot -z {serviceaccount_name} -n {namespace}

# Permitting root execution (e.g. runAsUser: 0)
oc adm policy add-scc-to-user anyuid -z {serviceaccount_name} -n {namespace}

# Permitting privileged execution (e.g. daemonsets requiring host access)
oc adm policy add-scc-to-user privileged -z {serviceaccount_name} -n {namespace}
```

*Verification:*
```bash
# Verify the role binding was created
oc get rolebindings -n {namespace} -o wide | grep scc

# Force pod restart to trigger new admission review
oc rollout restart deployment/{deployment_name} -n {namespace}
```

---

### 3. Verify Active SCC on Running Pods

To determine which SCC admitted a running pod:

```bash
oc get pod {pod_name} -n {namespace} -o jsonpath='{.metadata.annotations.openshift\.io/scc}{"\n"}'
```

---

### 4. Authoring OpenShift-Compliant Workload Security Contexts

To design manifests that pass default `restricted-v2` admission without requiring custom SCC grants:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: compliant-workload
  namespace: {namespace}
spec:
  replicas: 1
  template:
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: app
          image: {image}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
            readOnlyRootFilesystem: true
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
```

> **Note:** Omit `runAsUser`, `runAsGroup`, and `fsGroup` from the YAML to allow OpenShift's admission controller to dynamically assign valid UIDs within the namespace's allocated range.
