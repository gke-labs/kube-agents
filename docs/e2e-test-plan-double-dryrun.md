# End-to-End (E2E) Test Plan: User-Attenuated Security via Double Dry-Run

This document specifies the end-to-end (E2E) test plan for verifying the **User-Attenuated Security via Double Dry-Run Pre-Flight** architecture in `kube-agents` (Kage).

---

## 1. Objective & Scope

The objective of this E2E test plan is to validate that:
1. **Permission Intersection ($User RBAC \cap Agent SA RBAC$)**: A cluster mutation is permitted if and only if **BOTH** the initiating user and the Agent ServiceAccount have sufficient RBAC permissions.
2. **User Attenuation (No Privilege Escalation)**: A user with restricted RBAC cannot use the Agent to execute operations beyond their own RBAC boundary.
3. **Agent SA Upper Bound**: An Agent SA configured with restricted RBAC (e.g. Read-Only) cannot be coerced by a `cluster-admin` user into modifying cluster state.
4. **GKE Audit Log Attribution**: All physical cluster mutations are attributed to `platform-agent-sa` in GKE Cloud Logging audit logs, while user identity is preserved in OpenTelemetry spans and Hermes session store.
5. **Zero Cold-Start Latency**: Validations execute in-process within 100–200ms without proxy sidecars or pod cold-starts.

---

## 2. Test Environment & Prerequisites

### 2.1 Cluster Setup
- A Kubernetes cluster (GKE or local Kind cluster with RBAC enabled).
- Two test namespaces created: `demo-prod` and `demo-dev`.

### 2.2 User Identities & Groups
| User Email | Assigned Group | Intended RBAC Scope |
| :--- | :--- | :--- |
| `alice@example.com` | `sre-team@example.com` | **Admin / SRE**: Full CRUD on Deployments, Pods, Services in `demo-prod` & `demo-dev`. |
| `bob@example.com` | `dev-team@example.com` | **Developer**: Read-only (`get`, `list`, `watch`) on Pods in `demo-dev`; **NO DELETE/UPDATE access**. |
| `anonymous` | (None) | **Unauthenticated**: No email in session context. |

### 2.3 Agent ServiceAccount Personas
| Agent Persona | K8s ClusterRole | Assigned RBAC Scope |
| :--- | :--- | :--- |
| **Platform Explorer** | `kubeagents:explorer` | **Read-Only**: `get`, `list` on nodes, pods, namespaces. |
| **Platform Operator** | `kubeagents:operator` | **Full Admin**: Full CRUD on deployments, pods, services, statefulsets. |

---

## 3. Test Matrix & Concrete Scenarios

```text
                                       +-----------------------------------+
                                       |            USER RBAC              |
                                       +------------------+----------------+
                                       |     ALLOWED      |    DENIED      |
+-------------------+------------------+------------------+----------------+
|                   |  ALLOWED (Admin) |  SCENARIO 1      |  SCENARIO 2    |
|   AGENT SA RBAC   |                  |  (PASSED)        |  (REJECTED)    |
|                   +------------------+------------------+----------------+
|                   |  DENIED (Read)   |  SCENARIO 3      |  SCENARIO 2    |
|                   |                  |  (REJECTED)      |  (REJECTED)    |
+-------------------+------------------+------------------+----------------+
```

---

### Scenario 1: Positive Test (ALLOW) - Authorized User & Authorized Agent SA

- **Goal**: Verify successful pre-flight and execution when both User and Agent SA have RBAC.
- **User**: `alice@example.com` (Group: `sre-team@example.com`)
- **Agent SA**: `kubeagents:operator` (Full Admin)
- **Target Command**: `kubectl delete deployment nginx-app -n demo-prod`
- **Pre-Flight Checks**:
  - **Check 1 (User Dry-Run)**: `kubectl delete deployment nginx-app -n demo-prod --as=alice@example.com --as-group=sre-team@example.com --dry-run=server`
    - **Result**: `exit 0` (`deployment.apps/nginx-app deleted (server dry run)`)
  - **Check 2 (Agent SA Dry-Run)**: `kubectl delete deployment nginx-app -n demo-prod --dry-run=server`
    - **Result**: `exit 0` (`deployment.apps/nginx-app deleted (server dry run)`)
- **Actual Execution**: `kubectl delete deployment nginx-app -n demo-prod`
- **Expected Outcome**:
  - Command executes successfully.
  - Deployment `nginx-app` is deleted from `demo-prod`.
  - GKE Audit Log records `principalEmail: platform-agent-sa@...`.

---

### Scenario 2: Negative Test (REJECT - User Attenuation) - Unauthorized User

- **Goal**: Verify user attenuation prevents privilege escalation.
- **User**: `bob@example.com` (Group: `dev-team@example.com` - Read-Only)
- **Agent SA**: `kubeagents:operator` (Full Admin)
- **Target Command**: `kubectl delete deployment nginx-app -n demo-prod`
- **Pre-Flight Checks**:
  - **Check 1 (User Dry-Run)**: `kubectl delete deployment nginx-app -n demo-prod --as=bob@example.com --as-group=dev-team@example.com --dry-run=server`
    - **Result**: `exit 1` (`Error from server (Forbidden): deployments.apps "nginx-app" is forbidden: User "bob@example.com" cannot delete resource "deployments"...`)
- **Expected Outcome**:
  - Execution **ABORTS immediately** after Check 1.
  - Return error to LLM/User: `[USER_RBAC_DENIED] User 'bob@example.com' is not authorized: ...`
  - Deployment `nginx-app` remains **UNDELETED** in the cluster.

---

### Scenario 3: Negative Test (REJECT - Agent SA Upper Bound) - Restricted Agent SA

- **Goal**: Verify Agent SA RBAC acts as a hard upper bound even for `cluster-admin` users.
- **User**: `alice@example.com` (Group: `sre-team@example.com` - Cluster Admin)
- **Agent SA**: `kubeagents:explorer` (Read-Only Agent)
- **Target Command**: `kubectl delete deployment nginx-app -n demo-prod`
- **Pre-Flight Checks**:
  - **Check 1 (User Dry-Run)**: `kubectl delete deployment nginx-app -n demo-prod --as=alice@example.com --as-group=sre-team@example.com --dry-run=server`
    - **Result**: `exit 0` (Alice has permission).
  - **Check 2 (Agent SA Dry-Run)**: `kubectl delete deployment nginx-app -n demo-prod --dry-run=server`
    - **Result**: `exit 1` (`Error from server (Forbidden): User "system:serviceaccount:..." cannot delete...`)
- **Expected Outcome**:
  - Execution **ABORTS after Check 2**.
  - Return error to LLM/User: `[AGENT_SA_DENIED] Agent SA is restricted from this action: ...`
  - Deployment `nginx-app` remains **UNDELETED**.

---

### Scenario 4: Security Edge Case (REJECT - Missing User Email)

- **Goal**: Verify fail-closed security when session context lacks a verified user email.
- **User**: Unauthenticated / Missing email in session store.
- **Agent SA**: `kubeagents:operator`
- **Target Command**: `kubectl delete deployment nginx-app -n demo-prod`
- **Expected Outcome**:
  - Execution **ABORTS immediately** before running any dry-run command.
  - Return error: `[SECURITY_DENIED] Cannot execute kubectl without a verified user email in session context`.

---

### Scenario 5: Read-Only Bypass & Performance (ALLOW)

- **Goal**: Verify non-mutating commands (`get`, `list`, `describe`) bypass dry-run checks for maximum performance.
- **User**: `bob@example.com`
- **Target Command**: `kubectl get pods -n demo-dev`
- **Expected Outcome**:
  - Pre-flight dry-run is **SKIPPED**.
  - Command executes directly under Agent SA identity within <50ms.

---

## 4. Verification Procedure & Step-by-Step Instructions

### Step 1: Apply Test RBAC Manifests
Create test ClusterRoles and ClusterRoleBindings in the test cluster:

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: demo-prod
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: sre-admin-role
rules:
- apiGroups: ["*"]
  resources: ["*"]
  verbs: ["*"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: sre-admin-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: sre-admin-role
subjects:
- kind: Group
  name: sre-team@example.com
  apiGroup: rbac.authorization.k8s.io
EOF
```

### Step 2: Run Automated E2E Test Suite
Execute the automated E2E Python test suite:

```bash
python3 -m unittest agents/shared/defaults/scripts/test_double_dryrun_e2e.py
```

### Step 3: Verify GKE Audit Logs
For GKE cluster deployments, query Cloud Logging to confirm audit log attribution:

```sql
resource.type="k8s_cluster"
protoPayload.methodName="io.k8s.apps.v1.deployments.delete"
protoPayload.authenticationInfo.principalEmail="platform-agent-sa@..."
```

**Assertion**:
- `protoPayload.authenticationInfo.principalEmail` MUST equal `platform-agent-sa@...`.
- `protoPayload.response.status` MUST equal `Success` for Scenario 1.
- No audit log entry for `delete` should exist for Scenarios 2, 3, or 4.

---

## 5. Test Suite Summary & Status

| Test Suite | File Location | Status |
| :--- | :--- | :--- |
| **Cloud Identity Resolver Tests** | `agents/shared/defaults/scripts/test_identity_resolver.py` | **PASSED (3/3)** |
| **Session Store Groups Tests** | `agents/shared/defaults/scripts/test_session_store_groups.py` | **PASSED (2/2)** |
| **Double Dry-Run Pre-Flight Hook Tests** | `agents/shared/defaults/scripts/test_double_dryrun_hook.py` | **PASSED (7/7)** |
| **E2E Integration Test Suite** | `agents/shared/defaults/scripts/test_double_dryrun_e2e.py` | **PASSED (3/3)** |
| **Kubernetes Operator Manifest Tests** | `k8s-operator/internal/controller/platformagent_manifests_test.go` | **PASSED** |
