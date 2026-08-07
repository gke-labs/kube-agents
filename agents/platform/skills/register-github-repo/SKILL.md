---
name: register-github-repo
description:
  Safely verify authorization and register a new GitHub repository to be monitored
  by the autonomous agent workloads (like the github-issue-resolver).
---

# Skill: register-github-repo

Use this skill when a user asks the agent to monitor, watch, or add a new GitHub repository to its list of managed repositories.

## Procedure

Execute the deterministic registration script:

```bash
python3 /opt/data/skills/register-github-repo/scripts/register_github_repo.py --repo <owner>/<repoName>
```

### Script Behaviors:

1. **GitHub Auth**: The script automatically calls the Minty token refresher to obtain scoped permissions for the target repository.
2. **Access Verification**: The script attempts to view the GitHub repository.
   - If it fails (e.g. repo not found or 403 Forbidden), the script terminates. **You MUST relay the exact error message back to the user**, explaining that they need to install the GitHub App on the target repository first.
3. **Cluster State Patch**: If access is confirmed, it updates the `$GITHUB_STATE_CONFIGMAP` ConfigMap in the cluster to add the repository.

### Turn Completion Checklist

- Did the script exit with `0`? Tell the user the repository was successfully added to the watchlist.
- Did the script exit with an error `1`? Relay the instructions to the user to fix the missing permissions.
