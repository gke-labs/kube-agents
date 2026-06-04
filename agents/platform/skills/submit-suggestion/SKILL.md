---
name: submit-suggestion
description: Propose declarative configuration updates securely by committing file changes and submitting GitHub Pull Requests (PRs) for SRE review.
---

# submit-suggestion - Secure GitOps Pull Request Orchestrator

This skill equips the Platform Agent to propose declarative file updates, GKE infrastructure adjustments, or configuration changes securely by committing local repository changes and submitting GitHub Pull Requests (PRs) for human review.

## When to Use

- **Declarative File Provisioning:** Triggered when new GKE manifests or configs are requested.
- **Configuration Upgrades:** Triggered when upgrading version configurations, security patches, or network policies.
- **Governance Policy Syncs:** Triggered when compliance playbooks or settings require updates.

_Crucially, you are strictly forbidden from executing direct, manual mutations. All changes must flow through this secure PR suggestion skill._

## Execution Instructions

Follow these steps to make, commit, and submit your GitOps suggestions asynchronously:

### Step 1: Prepare the Workspace Changes & Git Branch

1.  Ensure you are on the `main` branch and have pulled the latest changes:
    ```bash
    git checkout main
    git pull origin main
    ```
2.  Create and switch to a unique Git branch named dynamically after the target configuration:
    ```bash
    git checkout -b platform-agent/<change_type>-<target_id>
    ```
    _(Example: `platform-agent/provision-mercury-09` or `platform-agent/upgrade-policy-baseline`)_
3.  Generate or edit the required declarative files inside the repository workspace as requested.
4.  Stage and commit the changes locally following Conventional Commit standards. **CRITICAL SECURITY RULE:** You **must** explicitly stage only the targeted declarative manifest files you generated or modified. **Never use `git add .` or `git add -A`** to prevent committing transient debugging files, volatile local credentials, or workspace logs:
    ```bash
    git add <file_path_1> <file_path_2>
    git commit -m "<conventional_commit_message>"
    ```
    _(Example: `git add config/manifest.yaml && git commit -m "feat(fleet): provision GKE operator for mercury-09"` or `git add policies/baseline.yaml && git commit -m "fix(policy): restrict baseline network policy ingress"`)_

### Step 2: Call the Secure Submit Suggestion Script

Invoke the secure, pre-packaged Python helper script **`submit_suggestion.py`** inside your terminal tool to automatically handle all GitHub App token exchanges, git credentials configurations, branch pushing, and Pull Request creation:

```bash
./skills/submit-suggestion/scripts/submit_suggestion.py \
  --branch "platform-agent/<change_type>-<target_id>" \
  --title "<pr_title>" \
  --body "This Pull Request was generated automatically by the **Platform Agent** control plane.

### 🚀 Functional Impact:
<detailed_markdown_bulleted_impact_description>

Please review the code diffs and merge this PR to trigger the GitOps CI/CD rollout!"
```

The script will return the clean, live GitHub PR URL dynamically!

### Step 3: Confirm Suggestion

Record the PR link returned by the script, update the pending status inside your local state registry (if applicable), and present a clean, human-readable confirmation containing the PR URL link back to the user.
