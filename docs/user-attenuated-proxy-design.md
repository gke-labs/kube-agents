# User-Attenuated Security via Permission Intersection (User RBAC ∩ Agent SA RBAC)

This document details the architecture, data flow, component specifications, and implementation plan for enforcing **user-attenuated security** in `kube-agents` (Kage) using a **Dual-Authorization / Permission Intersection** model (User RBAC $\cap$ Agent SA RBAC).

---

## 1. Overview & Core Principles

In an agentic harness, an AI agent acts as an autonomous entity on behalf of human users. To balance operational flexibility with security:

1. **Permission Intersection (Dual Authorization)**: An operation is permitted if and only if **BOTH** of the following conditions are met:
   - **User RBAC Check (User Attenuation)**: The initiating end-user (`user_email` + `user_groups`) has permission to perform the requested operation (checked via `SubjectAccessReview` or `--as=<user> --dry-run=server`).
   - **Agent SA RBAC Check (Agent Upper Bound)**: The Agent's ServiceAccount has permission to perform the requested operation (checked natively by the Kubernetes API Server upon execution).
   $$\text{Allowed Operation} = (\text{User RBAC via SAR/Dry-Run}) \;\text{AND}\; (\text{Agent SA RBAC at API Server})$$

2. **Agent SA Audit Log Attribution**: All K8s API server calls are physically executed and logged under the **Agent's ServiceAccount identity** (`principalEmail: platform-agent-sa`) in GKE / Cloud Logging audit logs. User-level attribution is maintained at the Hermes Session and OpenTelemetry span layer.

3. **Configurable Agent Permission Boundary**: The Agent SA's permissions can be configured anywhere from **Read-Only** (e.g. `get, list, watch` only) to **Operator-Admin** or **Cluster-Admin**. The Agent SA acts as a hard upper bound: even if a `cluster-admin` user asks a Read-Only Agent to delete a namespace, the operation will be denied by the Agent SA's RBAC.

---

## 2. End-to-End Data Flow

```text
Google Chat Event
  │ (Sender email: alice@example.com)
  ▼
Hermes Gateway Message (Platform Agent Ingress)
  │
  ├─► pre_gateway_dispatch Hook (session_store plugin)
  │     │
  │     ├─► IdentityResolver (identity_resolver.py)
  │     │     │  Queries Cloud Identity API (TTL 15m cache)
  │     │     └─► Returns user_groups: ["sre-team@example.com"]
  │     │
  │     └─► Persists to SQLite: /var/lib/kube-agents/session/session_kv.db
  │           (session_id, user_email, user_groups)
  │
  ▼
Multi-Agent Delegation (SessionManager)
  │  Forwards headers: X-Hermes-Session-Id, X-Hermes-User-Email, X-Hermes-User-Groups
  ▼
Downstream Agent Execution (e.g. Operator Agent)
  │
  ├─► Pre-Tool Hook (double_dryrun_hook.py)
  │     │
  │     ├──► CHECK 1: User RBAC Dry-Run Check (Native GKE Dry-Run)
  │     │      kubectl <cmd> --as=alice@example.com --as-group=sre-team --dry-run=server
  │     │      If Exit Code != 0 -> ❌ Abort & Return [USER_RBAC_DENIED]
  │     │
  │     ├──► CHECK 2: Agent SA RBAC Dry-Run Check (Native GKE Dry-Run)
  │     │      kubectl <cmd> --dry-run=server
  │     │      If Exit Code != 0 -> ❌ Abort & Return [AGENT_SA_DENIED]
  │     │
  │     └──► STEP 3: Actual Execution (As Agent SA)
  │            kubectl <cmd> (No dry-run, no --as flag)
  │
  ▼
GKE API Server (Agent SA RBAC Check & Audit Log)
  ├─► 1. Mutates etcd (Deployment deleted / created)
  └─► 2. Writes GKE Audit Log (principalEmail: platform-agent-sa)
```

---

## 3. Permission Intersection Matrix

The following table demonstrates how the dual-authorization model handles various user and agent permission combinations:

| Scenario | User RBAC (Alice) | Agent SA RBAC | Result | Reason |
| :--- | :--- | :--- | :--- | :--- |
| **1. Read Pods** | Allowed (`get pods`) | Allowed (`get pods`) | **ALLOWED** | Both User and Agent SA have permission. |
| **2. Delete Namespace (User is Dev)** | Denied (`delete ns`) | Allowed (`cluster-admin`) | **DENIED** | User RBAC check fails. User cannot elevate privileges. |
| **3. Delete Node (User is Admin, Agent is ReadOnly)** | Allowed (`delete node`) | Denied (`get/list` only) | **DENIED** | Agent SA RBAC fails. Agent cannot exceed its configured boundary. |
| **4. Delete Pod (Both SRE & Agent Admin)** | Allowed (`delete pod`) | Allowed (`delete pod`) | **ALLOWED** | Both permitted. Action logged as `platform-agent-sa`. |

---

## 4. Component Architecture & Specifications

### 4.1 Cloud Identity Group Membership Resolver (`identity_resolver.py`)

**File Path:** `agents/shared/defaults/scripts/identity_resolver.py`

Resolves user email $\rightarrow$ Google Workspace / Cloud Identity groups (e.g. `alice@example.com` $\rightarrow$ `["sre-team@example.com"]`) and caches results in `session_kv.db` for 15 minutes.

```python
import os
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import google.auth
from googleapiclient.discovery import build

logger = logging.getLogger("kube_agents.identity_resolver")

DEFAULT_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")
CACHE_TTL_MINUTES = 15

class CloudIdentityResolver:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_group_cache (
                    user_email TEXT PRIMARY KEY,
                    groups_json TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL
                )
            """)

    def get_user_groups(self, user_email: str) -> List[str]:
        if not user_email:
            return []

        cached = self._get_cached_groups(user_email)
        if cached is not None:
            return cached

        groups = self._query_cloud_identity(user_email)
        self._set_cached_groups(user_email, groups)
        return groups

    def _get_cached_groups(self, user_email: str) -> Optional[List[str]]:
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                row = conn.execute(
                    "SELECT groups_json, expires_at FROM user_group_cache WHERE user_email = ?",
                    (user_email,)
                ).fetchone()
                if row:
                    groups_json, expires_at_str = row
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if datetime.now(timezone.utc) < expires_at:
                        return json.loads(groups_json)
        except Exception as exc:
            logger.warning("Group cache read failure for %s: %s", user_email, exc)
        return None

    def _set_cached_groups(self, user_email: str, groups: List[str]) -> None:
        try:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=CACHE_TTL_MINUTES)).isoformat()
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO user_group_cache (user_email, groups_json, expires_at) VALUES (?, ?, ?)",
                    (user_email, json.dumps(groups), expires_at)
                )
        except Exception as exc:
            logger.warning("Group cache write failure for %s: %s", user_email, exc)

    def _query_cloud_identity(self, user_email: str) -> List[str]:
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-identity.groups.readonly"]
            )
            service = build("cloudidentity", "v1", credentials=credentials)
            response = service.groups().memberships().searchTransitiveGroups(
                parent="groups/-",
                query=f"member_key_id == '{user_email}'"
            ).execute()

            groups = []
            for item in response.get("memberships", []):
                group_id = item.get("groupKey", {}).get("id")
                if group_id:
                    groups.append(group_id)
            return groups
        except Exception as exc:
            logger.error("Failed to query Cloud Identity API for %s: %s", user_email, exc)
            return []
```

---

### 4.2 Double Dry-Run Pre-Flight Hook (`double_dryrun_hook.py`)

**File Path:** `agents/shared/defaults/scripts/double_dryrun_hook.py`

This hook intercepts tool calls before execution and runs Check 1 (User Dry-Run) and Check 2 (Agent SA Dry-Run) before permitting actual execution.

```python
import json
import logging
import subprocess
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("kube_agents.double_dryrun_hook")

READ_ONLY_VERBS = {"get", "list", "watch", "describe", "cluster-info"}
RESTRICTED_GROUPS = {
    "system:masters",
    "system:cluster-admins",
    "system:nodes",
    "system:serviceaccounts",
    "system:unauthenticated",
}

class DoubleDryRunHook:
    def __init__(self, session_manager: Optional[Any] = None) -> None:
        self.session_manager = session_manager

    def process_tool_call(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        if not args or not isinstance(args, dict):
            return args

        command = args.get("command", "")
        if not command or not isinstance(command, str) or "kubectl" not in command:
            return args

        user_email = context.get("user_email") or context.get("metadata", {}).get("user_email")
        user_groups = context.get("user_groups") or context.get("metadata", {}).get("user_groups", [])

        if not user_email:
            raise PermissionError("[SECURITY_DENIED] Cannot execute kubectl without a verified user email in session context")

        verb = self._extract_kubectl_verb(command)
        if verb in READ_ONLY_VERBS or verb in ("exec", "logs", "port-forward", "attach"):
            return args

        user_ok, user_err = self._run_user_dryrun(command, user_email, user_groups)
        if not user_ok:
            raise PermissionError(f"[USER_RBAC_DENIED] {user_err}")

        agent_ok, agent_err = self._run_agent_dryrun(command)
        if not agent_ok:
            raise PermissionError(f"[AGENT_SA_DENIED] {agent_err}")

        logger.info("Pre-flight double dry-run PASSED for command: %s", command)
        return args

    def _run_user_dryrun(self, command: str, user_email: str, user_groups: List[str]) -> Tuple[bool, str]:
        safe_groups = [g for g in (user_groups or []) if g not in RESTRICTED_GROUPS]
        user_flags = f" --as={user_email} " + " ".join([f"--as-group={g}" for g in safe_groups])
        user_cmd = f"{command}{user_flags} --dry-run=server"
        res = subprocess.run(user_cmd, shell=True, capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"User '{user_email}' is not authorized: {res.stderr.strip()}"
        return True, ""

    def _run_agent_dryrun(self, command: str) -> Tuple[bool, str]:
        agent_cmd = f"{command} --dry-run=server"
        res = subprocess.run(agent_cmd, shell=True, capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"Agent SA is restricted from this action: {res.stderr.strip()}"
        return True, ""

    def _extract_kubectl_verb(self, command: str) -> str:
        tokens = command.strip().split()
        for i, tok in enumerate(tokens):
            if tok == "kubectl" and i + 1 < len(tokens):
                return tokens[i + 1]
        return ""
```

---

### 4.3 Kubernetes Operator & Agent SA RBAC Generator

**File Path:** `k8s-operator/internal/controller/platformagent_manifests.go`

The Kubernetes Operator provisions the Agent ServiceAccount and its ClusterRole (`buildPlatformExplorerRole`).

```go
// buildPlatformRole generates the configured ClusterRole for the Agent SA
func buildPlatformRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.ClusterRole {
	rules := []rbacv1.PolicyRule{
		{
			APIGroups: []string{""},
			Resources: []string{"nodes", "pods", "namespaces", "services", "configmaps"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{"apps"},
			Resources: []string{"deployments", "statefulsets"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{"authorization.k8s.io"},
			Resources: []string{"subjectaccessreviews"},
			Verbs:     []string{"create"},
		},
	}

	return &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRole",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("kubeagents:agent:%s:%s", agent.Namespace, agent.Name),
		},
		Rules: rules,
	}
}
```

---

## 5. Security & Audit Logging Verification

### 5.1 GKE Cloud Logging Audit Trail
During integration testing, run a `kubectl` command through the agent and query Cloud Logging:

```sql
resource.type="k8s_cluster"
protoPayload.authenticationInfo.principalEmail="platform-agent-sa@..."
```

Verify that:
1. `protoPayload.authenticationInfo.principalEmail` = `platform-agent-sa@...` (Attributing all cluster actions to the Agent SA).
2. The user identity (`alice@example.com`) is present in Hermes OTel span attributes and `session_kv.db`.
3. If Alice lacks RBAC permission, Check 1 fails with `[USER_RBAC_DENIED]`.
4. If Alice has RBAC permission but Agent SA lacks RBAC permission, Check 2 fails with `[AGENT_SA_DENIED]`.
