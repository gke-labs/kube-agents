import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from identity_resolver import CloudIdentityResolver

logger = logging.getLogger("kube_agents.double_dryrun_hook")

# Non-mutating read-only verbs that do not require server dry-run pre-flight
READ_ONLY_VERBS = {"get", "list", "watch", "describe", "cluster-info"}

# System groups that MUST NOT be passed to --as-group during dry-run
RESTRICTED_GROUPS = {
    "system:masters",
    "system:cluster-admins",
    "system:nodes",
    "system:serviceaccounts",
    "system:unauthenticated",
}


class DoubleDryRunHook:
    """Hermes Pre-Tool Execution Hook enforcing Permission Intersection via GKE Double Dry-Run."""

    def __init__(self, session_manager: Optional[Any] = None) -> None:
        self.session_manager = session_manager

    def process_tool_call(self, tool_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Intercepts tool execution and runs Check 1 (User Dry-Run) and Check 2 (Agent SA Dry-Run).
        """
        if not args or not isinstance(args, dict):
            return args

        command = args.get("command", "")
        if not command or not isinstance(command, str) or "kubectl" not in command:
            return args

        # 1. Resolve User Email & Groups from Session Context
        user_email = context.get("user_email")
        user_groups = context.get("user_groups")
        expires_at_str = context.get("user_groups_expires_at", "")

        if not user_email or not user_groups:
            metadata = context.get("metadata", {})
            user_email = user_email or metadata.get("user_email")
            user_groups = user_groups or metadata.get("user_groups", [])
            expires_at_str = expires_at_str or metadata.get("user_groups_expires_at", "")

        if not user_email and self.session_manager and context.get("session_id"):
            session_id = context["session_id"]
            meta = self.session_manager.metadata_for_session(session_id)
            user_email = meta.get("user_email")
            user_groups = meta.get("user_groups", [])
            expires_at_str = meta.get("user_groups_expires_at", "")

        if isinstance(user_groups, str):
            try:
                user_groups = json.loads(user_groups)
            except Exception:
                user_groups = []

        # Check Expiration & Refresh if Expired
        if user_email:
            should_refresh = False
            if expires_at_str:
                try:
                    exp_dt = datetime.fromisoformat(expires_at_str)
                    if datetime.now(timezone.utc) >= exp_dt:
                        should_refresh = True
                except Exception:
                    should_refresh = True

            if should_refresh or not user_groups:
                try:
                    user_groups = CloudIdentityResolver().get_user_groups(user_email, force_refresh=should_refresh)
                except Exception as exc:
                    logger.warning("Failed to refresh user groups for %s: %s", user_email, exc)

        # 2. Fail-Closed Security Policy: Rejects command if no verified user email is present
        if not user_email:
            raise PermissionError(
                "[SECURITY_DENIED] Cannot execute kubectl without a verified user email in session context"
            )

        # 3. Skip dry-run for read-only commands (get/list/describe) or unsupported streaming (exec/logs)
        verb = self._extract_kubectl_verb(command)
        if verb in READ_ONLY_VERBS or verb in ("exec", "logs", "port-forward", "attach"):
            return args

        # 4. CHECK 1: User RBAC Dry-Run Check
        user_ok, user_err = self._run_user_dryrun(command, user_email, user_groups)
        if not user_ok:
            raise PermissionError(f"[USER_RBAC_DENIED] {user_err}")

        # 5. CHECK 2: Agent SA RBAC Dry-Run Check
        agent_ok, agent_err = self._run_agent_dryrun(command)
        if not agent_ok:
            raise PermissionError(f"[AGENT_SA_DENIED] {agent_err}")

        logger.info("Pre-flight double dry-run PASSED for command: %s", command)
        return args

    def _run_user_dryrun(self, command: str, user_email: str, user_groups: List[str]) -> Tuple[bool, str]:
        safe_groups = [g for g in (user_groups or []) if g not in RESTRICTED_GROUPS]
        user_flags = f" --as={user_email} " + " ".join([f"--as-group={g}" for g in safe_groups])
        user_cmd = f"{command}{user_flags} --dry-run=server"

        res = self._exec(user_cmd)
        if res.returncode != 0:
            return False, f"User '{user_email}' is not authorized: {res.stderr.strip()}"
        return True, ""

    def _run_agent_dryrun(self, command: str) -> Tuple[bool, str]:
        agent_cmd = f"{command} --dry-run=server"
        res = self._exec(agent_cmd)
        if res.returncode != 0:
            return False, f"Agent SA is restricted from this action: {res.stderr.strip()}"
        return True, ""

    def _exec(self, cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True)

    def _extract_kubectl_verb(self, command: str) -> str:
        tokens = command.strip().split()
        for i, tok in enumerate(tokens):
            if tok == "kubectl" and i + 1 < len(tokens):
                return tokens[i + 1]
        return ""
