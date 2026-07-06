# Platform Agent SOP - GitHub Issue Resolver

This procedure outlines the steps for the Platform Agent to autonomously detect the repository target, poll, triage, investigate, and directly resolve open issues from the GitHub issue tracker.

## Procedure

1. **Verify Authentication & Target Repository Context**:
   - Read the target Git repository URL from `/opt/data/SETTINGS.md` (injected by the K8s operator from `spec.integration.gitHub.gitRepo` as `- **Git Repo:** https://github.com/owner/repository.git`).
   - Extract the `owner/repo` string and export it as `GH_REPO`:
     ```bash
     export GH_REPO=$(grep -i "Git Repo:" /opt/data/SETTINGS.md | awk '{print $NF}' | sed -E 's|https://github.com/||; s|\.git$||')
     ```
   - Verify that the GitHub CLI (`gh`) is authenticated and can access the target repository:
     ```bash
     gh auth status
     gh repo view "$GH_REPO" --json nameWithOwner
     ```
   - If unauthenticated or if `GH_REPO` is missing/None, log an error in `memory/` and abort the routine.

2. **Poll Unaddressed Open Issues & Recover Stale Investigations**:
   - First, perform a **2-Hour Stale In-Progress Sweep**: query open issues labeled `status:in-progress` that have not been updated in over 2 hours:
     ```bash
     gh issue list -R "$GH_REPO" --label "status:in-progress" --json number,updatedAt
     ```
     If an issue's `updatedAt` timestamp is older than 2 hours, remove the label so the issue can be retried or escalated:
     ```bash
     gh issue edit <number> -R "$GH_REPO" --remove-label "status:in-progress"
     gh issue comment <number> -R "$GH_REPO" --body "⚠️ **Investigation Timed Out:** The previous automated investigation exceeded the 2-hour threshold without resolution. Removing \`status:in-progress\` for re-triage."
     ```
   - Query up to 5 oldest open issues in `$GH_REPO` using server-side search to exclude active custom status labels (`status:in-progress` or `status:escalation-needed`):
     ```bash
     gh issue list -R "$GH_REPO" --search "is:issue is:open -label:status:in-progress -label:status:escalation-needed" --limit 5 --json number,title,body,labels,assignees,comments,updatedAt
     ```
   - If no actionable unaddressed issues exist, terminate the routine cleanly (`NO_REPLY`).

3. **Lock Issue via State Labeling (Idempotency)**:
   - For the first actionable unaddressed issue `#<number>`, immediately apply the custom status label `status:in-progress` (creating the label first if it does not exist in the repo) and assign `@me`:
     ```bash
     gh label create "status:in-progress" -R "$GH_REPO" --color "FBCA04" --description "Work in progress by AI Agent" 2>/dev/null || true
     gh issue edit <number> -R "$GH_REPO" --add-label "status:in-progress" --add-assignee "@me"
     ```
   - Post an initial acknowledgment comment:
     ```bash
     gh issue comment <number> -R "$GH_REPO" --body "🤖 **Platform Agent Triaging:** I have picked up this issue, assigned `@me`, and marked it as \`status:in-progress\`. Beginning root cause investigation..."
     ```

4. **Triage & Direct Resolution by Platform Agent**:
   - Analyze the issue title, body, and labels to diagnose the root cause directly using GKE read-only tools and local Git repository inspection.
   - **Case A: Code / Manifest Correction Required**:
     - Inspect relevant manifests or scripts directly using workspace tools or by navigating to the local Git repository clone (`./repo/`).
     - Create a local branch (`fix/issue-<number>`), apply the necessary manifest correction or code fix, and commit:
       ```bash
       git checkout -b fix/issue-<number>
       git add <modified-files>
       git commit -m "fix: resolve issue #<number> - <short description>"
       ```
     - Propose the change through the active declarative workflow via Pull Request (e.g., invoking the **`submit-suggestion`** skill or using `gh pr create` linking `Closes #<number>`).
   - **Case B: Cluster Health / Operational Inspection**:
     - Perform direct cluster inspection (`kubectl get`, log queries, telemetry checks). If an operational adjustment or ConfigMap/ResourceQuota fix is required, propose it declaratively via PR.

5. **Evaluate Findings & Transition State**:
   - Once investigation or repair proposals are complete, evaluate the outcome and update the GitHub issue accordingly:
     - **Case 1: Fix Available / PR Created / Issue Resolved**
       - Post a comprehensive closing comment with **verifiable proof** (e.g., test execution logs, `kubectl` rollout verification, or PR link):
         ```bash
         gh issue comment <number> -R "$GH_REPO" --body "🤖 **Issue Resolved:** A correction has been implemented and verified.
         
         ### Verification Proof & Findings:
         - **Diagnosis:** <detailed root cause>
         - **Resolution:** <what was changed / PR #<pr-num>>
         - **Ground Truth Proof:**
         \`\`\`
         <insert raw test output, cli status, or git commit diff summary>
         \`\`\`"
         ```
       - Apply custom status label `status:resolved`, remove `status:in-progress`, and close the issue:
         ```bash
         gh label create "status:resolved" -R "$GH_REPO" --color "0E8A16" 2>/dev/null || true
         gh issue edit <number> -R "$GH_REPO" --add-label "status:resolved" --remove-label "status:in-progress"
         gh issue close <number> -R "$GH_REPO" --reason "completed"
         ```
     - **Case 2: No Change Needed / False Alarm / Irrelevant**
       - If investigation proves the issue is invalid or already resolved in the latest release, post a closing comment with **explicit diagnostic proof** justifying why no change is needed:
         ```bash
         gh issue comment <number> -R "$GH_REPO" --body "🤖 **Closing Issue (No Action Required):** Investigation confirmed that no modifications are necessary.
         
         ### Justification & Proof:
         \`\`\`
         <insert log proof showing healthy cluster state or non-reproducible error>
         \`\`\`"
         ```
       - Remove `status:in-progress` and close the issue (`gh issue close <number> -R "$GH_REPO" --reason "not planned"`).
     - **Case 3: Human Decision or Escalation Required**
       - If the ticket requires destructive cluster mutations, missing permissions, or architectural decisions beyond agent red lines, apply custom status label `status:escalation-needed` to flag it for human review and exclude it from further automated polling:
         ```bash
         gh label create "status:escalation-needed" -R "$GH_REPO" --color "B60205" 2>/dev/null || true
         gh issue edit <number> -R "$GH_REPO" --add-label "status:escalation-needed" --remove-label "status:in-progress"
         gh issue comment <number> -R "$GH_REPO" --body "🚨 **Escalation Needed:** Investigation complete, but human approval is required before applying changes... <details>"
         ```

6. **Log to Memory**:
   - Record the issue triage and final state transition in the daily memory log (`memory/YYYY-MM-DD.md`).
