# Detailed Build Plan: User-Attenuated Security via Double Dry-Run Pre-Flight

This document provides an unambiguous, file-by-file build plan for implementing **User-Attenuated Security via Double Dry-Run Pre-Flight** in `kube-agents` (Kage).

Every step, file path, class name, and method signature in this plan is directly derived from and grounded in the source code of the `kube-agents` repository.

---

## 1. Architecture Overview & Core Design

The build plan implements the **Permission Intersection (Dual Authorization)** model:

$$\text{Allowed Operation} = (\text{User RBAC Check via GKE Dry-Run}) \;\text{AND}\; (\text{Agent SA RBAC Check via GKE Dry-Run})$$

### End-to-End Data Flow:

```text
Google Chat Event (alice@example.com)
  │
  ▼
Platform Agent Ingress (session_store plugin)
  │  1. Pre-gateway hook calls CloudIdentityResolver (identity_resolver.py)
  │  2. Resolves user_groups: ["sre-team@example.com"] (TTL 15m SQLite cache)
  │  3. Persists (session_id, user_email, user_groups) to /var/lib/kube-agents/session/session_kv.db
  ▼
Inter-Agent Delegation (SessionManager)
  │  Forwards headers: X-Hermes-User-Email, X-Hermes-User-Groups
  ▼
Tool Execution (e.g. Operator Agent / Platform Agent)
  │
  ├─► Pre-Tool Execution Hook (double_dryrun_hook.py)
  │     │
  │     ├──► CHECK 1: User RBAC Dry-Run Check (Native GKE Dry-Run)
  │     │      kubectl <cmd> --as=alice@example.com --as-group=sre-team --dry-run=server
  │     │      Exit Code != 0 -> ❌ Abort & Return [USER_RBAC_DENIED]
  │     │
  │     ├──► CHECK 2: Agent SA RBAC Dry-Run Check (Native GKE Dry-Run)
  │     │      kubectl <cmd> --dry-run=server
  │     │      Exit Code != 0 -> ❌ Abort & Return [AGENT_SA_DENIED]
  │     │
  │     └──► STEP 3: Actual Execution (As Agent SA)
  │            kubectl <cmd> (No dry-run, no --as flag)
  │
  ▼
GKE API Server
  │  1. Mutates etcd (Deployment deleted / created)
  └─►2. Writes GKE Audit Log (principalEmail: platform-agent-sa)
```

---

## 2. File-by-File Implementation Plan

### Milestone 1: Cloud Identity Group Membership Resolver

#### File 1: `agents/shared/defaults/scripts/identity_resolver.py` (New File)
- **Purpose**: Query Google Cloud Identity API (`groups.memberships.searchTransitiveGroups`) via Workload Identity and cache user email $\rightarrow$ groups in `session_kv.db` for 15 minutes.
- **Class**: `CloudIdentityResolver`
- **Methods**:
  - `__init__(self, db_path: Optional[str] = None)`: Initialise SQLite database table `user_group_cache (user_email PRIMARY KEY, groups_json TEXT, expires_at TIMESTAMP)`.
  - `get_user_groups(self, user_email: str) -> List[str]`: Public entrypoint. Checks SQLite cache; if cache miss or expired, queries Cloud Identity API and updates cache.
  - `_get_cached_groups(self, user_email: str) -> Optional[List[str]]`: Reads valid cache entry from SQLite if `expires_at > UTC now`.
  - `_set_cached_groups(self, user_email: str, groups: List[str]) -> None`: Inserts/replaces cache entry with `expires_at = UTC now + 15m`.
  - `_query_cloud_identity(self, user_email: str) -> List[str]`: Calls Google Cloud Identity API via `google.auth.default()`.

#### File 2: `agents/shared/defaults/scripts/test_identity_resolver.py` (New File)
- **Purpose**: Unit test suite for `CloudIdentityResolver`.
- **Tests**:
  - `test_empty_email`: Verify empty email returns `[]`.
  - `test_cache_miss_and_hit`: Verify first call queries API and second call returns from cache without calling API.
  - `test_cache_expiration`: Verify expired cache entry triggers re-query of Cloud Identity API.

---

### Milestone 2: Session Store & KV Extension for `user_groups`

#### File 3: `agents/platform/defaults/plugins/session_store/store.py` (Modify Existing)
- **Target File**: [agents/platform/defaults/plugins/session_store/store.py](file:///usr/local/google/home/haoxuw/workspace/OSS/kube-agents/agents/platform/defaults/plugins/session_store/store.py)
- **Changes**:
  1. Update `SessionMetadata.KEYS` tuple to include `"user_groups"`.
  2. Update `SessionMetadata.__init__()` to accept `user_groups: Optional[List[str]] = None`.
  3. In `SessionMetadata.from_event()`, when `user_email` is present, call `CloudIdentityResolver().get_user_groups(user_email)` and assign to `user_groups`.
  4. In `SessionMetadata.to_dict()`, include `user_groups` in return dictionary.

#### File 4: `agents/shared/defaults/scripts/session_manager.py` (Modify Existing)
- **Target File**: [agents/shared/defaults/scripts/session_manager.py](file:///usr/local/google/home/haoxuw/workspace/OSS/kube-agents/agents/shared/defaults/scripts/session_manager.py)
- **Changes**:
  1. Add `"user_groups"` to `SessionManager.SESSION_METADATA_KEYS`.
  2. In `delegation_headers()`, serialize `metadata["user_groups"]` into JSON string under `X-Hermes-User-Groups`.

#### File 5: `agents/shared/defaults/scripts/test_session_store_groups.py` (New File)
- **Purpose**: Unit test suite verifying `SessionMetadata` serialization and `SessionManager.delegation_headers()` with `user_groups`.

---

### Milestone 3: Double Dry-Run Pre-Flight Execution Hook

#### File 6: `agents/shared/defaults/scripts/double_dryrun_hook.py` (New File)
- **Purpose**: Pre-tool execution hook enforcing User RBAC dry-run (Check 1) and Agent SA RBAC dry-run (Check 2) before allowing tool execution.
- **Class**: `DoubleDryRunHook`
- **Methods**:
  - `process_tool_call(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]`: Intercepts tool calls containing `kubectl`.
  - **Fail-Closed Security Check**: If `user_email` is missing in context or session $\rightarrow$ raise `PermissionError("[SECURITY_DENIED] ...")`.
  - **Read-Only Bypass**: If verb is in `{"get", "list", "watch", "describe", "cluster-info"}` or streaming `{"exec", "logs", "port-forward", "attach"}` $\rightarrow$ skip dry-run and return `args`.
  - **Check 1 (User RBAC)**: Execute `kubectl <cmd> --as=user_email --as-group=g1 ... --dry-run=server`. If exit code != 0 $\rightarrow$ raise `PermissionError("[USER_RBAC_DENIED] ...")`.
  - **Check 2 (Agent SA RBAC)**: Execute `kubectl <cmd> --dry-run=server`. If exit code != 0 $\rightarrow$ raise `PermissionError("[AGENT_SA_DENIED] ...")`.
  - **Return**: If both checks pass $\rightarrow$ return original `args` for execution under Agent SA.

#### File 7: `agents/shared/defaults/scripts/test_double_dryrun_hook.py` (New File)
- **Purpose**: Unit test suite for `DoubleDryRunHook`.
- **Tests**:
  - `test_non_kubectl_command_ignored`: Non-kubectl commands pass through.
  - `test_readonly_command_skips_dryrun`: `kubectl get pods` skips dry-run.
  - `test_fail_closed_on_missing_user_email`: Missing user email raises `PermissionError`.
  - `test_check1_user_rbac_denied`: User lacking RBAC fails at Check 1 with `USER_RBAC_DENIED`.
  - `test_check2_agent_sa_denied`: Agent SA lacking RBAC fails at Check 2 with `AGENT_SA_DENIED`.
  - `test_both_checks_pass`: Successful pre-flight allows actual execution.

---

### Milestone 4: Kubernetes Operator & Agent SA RBAC Generator

#### File 8: `k8s-operator/internal/controller/platformagent_manifests.go` (Modify Existing)
- **Target File**: [k8s-operator/internal/controller/platformagent_manifests.go](file:///usr/local/google/home/haoxuw/workspace/OSS/kube-agents/k8s-operator/internal/controller/platformagent_manifests.go)
- **Changes**:
  - Update `buildPlatformExplorerRole()` (lines 645-667) to include `authorization.k8s.io` `subjectaccessreviews` `create` rule for Agent SA.

#### File 9: `k8s-operator/internal/controller/platformagent_manifests_test.go` (Modify Existing)
- **Target File**: [k8s-operator/internal/controller/platformagent_manifests_test.go](file:///usr/local/google/home/haoxuw/workspace/OSS/kube-agents/k8s-operator/internal/controller/platformagent_manifests_test.go)
- **Changes**:
  - Update `TestBuildPlatformExplorerRole()` to verify the 3 PolicyRules (including `subjectaccessreviews`).

---

## 3. Execution & Testing Sequence

1. **Milestone 1**: Create `identity_resolver.py` and `test_identity_resolver.py`. Run `python3 -m unittest agents/shared/defaults/scripts/test_identity_resolver.py`.
2. **Milestone 2**: Update `store.py` and `session_manager.py`. Create `test_session_store_groups.py`. Run `python3 -m unittest agents/shared/defaults/scripts/test_session_store_groups.py`.
3. **Milestone 3**: Create `double_dryrun_hook.py` and `test_double_dryrun_hook.py`. Run `python3 -m unittest agents/shared/defaults/scripts/test_double_dryrun_hook.py`.
4. **Milestone 4**: Update `platformagent_manifests.go` and `platformagent_manifests_test.go`. Run `go test ./...` in `k8s-operator/`.

---

## 4. Verification & Audit Trail Checklist

- [ ] **User Attenuation**: Verify a user with read-only RBAC cannot use the agent to delete a pod (Check 1 fails with `[USER_RBAC_DENIED]`).
- [ ] **Agent SA Upper-Bound**: Verify a user with `cluster-admin` RBAC cannot use a read-only agent to delete a pod (Check 2 fails with `[AGENT_SA_DENIED]`).
- [ ] **GKE Audit Log Attribution**: Verify all cluster mutations are logged under `principalEmail: platform-agent-sa` in GKE Cloud Logging.
- [ ] **Zero Cold-Start Latency**: Verify tool calls execute immediately without proxy pod creation delays.
