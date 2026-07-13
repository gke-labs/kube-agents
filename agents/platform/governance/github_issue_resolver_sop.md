# Platform Agent SOP - GitHub Issue Resolver

> [!CAUTION]
> **INVIOLABLE SAFETY RED LINE:** NEVER inspect, comment on, edit, close, or modify any issue labeled `status:escalation-needed` or `agent:ignore`. Issues labeled `status:escalation-needed` are locked for human intervention and must NEVER be modified or closed autonomously. When querying issues with `gh issue list`, you MUST ALWAYS include `--search "is:issue is:open -label:status:in-progress -label:status:escalation-needed -label:agent:ignore"`.

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
   - Query up to 5 oldest open issues in `$GH_REPO` using server-side search to exclude active custom status labels (`status:in-progress`, `status:escalation-needed`, or `agent:ignore`):
     ```bash
     gh issue list -R "$GH_REPO" --search "is:issue is:open -label:status:in-progress -label:status:escalation-needed -label:agent:ignore" --limit 5 --json number,title,body,labels,assignees,comments,updatedAt
     ```
   - If no actionable unaddressed issues exist, terminate the routine cleanly by responding with exactly `[SILENT]` (nothing else). Do not generate escalation notices or chat reports for issues that already have `status:escalation-needed` applied.

3. **Multi-Issue Batch Processing Loop & State Locking**:
   - Iterate sequentially through **EACH actionable unaddressed issue `#<number>`** returned by Step 2 in a batch loop. Do NOT stop after the first issue.
   - For each issue `#<number>` in the batch, immediately apply `status:in-progress` lock and assign `@me`:
     ```bash
     gh label create "status:in-progress" -R "$GH_REPO" --color "FBCA04" --description "Work in progress by AI Agent" 2>/dev/null || true
     gh issue edit <number> -R "$GH_REPO" --add-label "status:in-progress" --add-assignee "@me"
     ```
   - **MANDATORY WORKLOG TRACKING RULE:** You MUST log all triage findings, diagnostic steps, and updates within each GitHub issue's comments (`gh issue comment <number> -R "$GH_REPO" --body "..."`) BEFORE reporting back in chat! The GitHub issue comment thread is the source of truth for full tracking of the worklog.
   - Post the initial triage acknowledgment and audit log comment:
     ```bash
     gh issue comment <number> -R "$GH_REPO" --body "🤖 **Platform Agent Triaging:** Issue marked \`status:in-progress\`. Beginning root cause investigation and recording worklog..."
     ```

4. **Triage & Direct Resolution by Platform Agent**:
   - **MANDATORY SKILL CONTINUATION RULE:** When you invoke or execute any specialized diagnostic or triage skill during investigation, that skill's output is an **intermediate finding**. You MUST NEVER terminate your execution turn early after a skill finishes. You MUST ALWAYS take the findings from the skill and proceed to **Step 5 (Evaluate Findings & Transition State)** to post the full investigation report as a comment on the GitHub bug (`gh issue comment <number> -F <file>`) and update the issue status (`status:resolved` or `status:escalation-needed`).
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
   - **CRITICAL FILE WRITING & SHELL ESCAPE RULES:**
     1. **Unique Issue Comment Paths (`/tmp/report_<number>.md`):** NEVER write issue comments to a generic `/tmp/report.md`. Always write to an issue-scoped temporary file (`/tmp/report_<number>.md`, e.g., `/tmp/report_50.md`). This guarantees you never post stale leftover text from a previous issue turn if a file-writing step fails.
     2. **Sanitize Backgrounding Tokens (`&&` / `&`):** NEVER include naked `&` or `&&` tokens inside terminal commands or heredoc blocks when writing files (`cat << 'EOF' > ...`), as terminal safety guardrails mistake `&` for process backgrounding syntax (`Foreground command uses "&" backgrounding`) and abort execution. Replace literal `&&` or `&` symbols with `;` or `AND` inside markdown text/blocks.
     3. **Safe Comment Posting:** Always post your formatted report from disk (`gh issue comment <number> -R "$GH_REPO" -F /tmp/report_<number>.md`). NEVER pass inline backticks inside `--body "..."`.

   - Once investigation or repair proposals are complete, evaluate the outcome and format your GitHub issue comment strictly according to the executive structured templates below:
     - **Case 1: Fix Available / PR Created / Issue Resolved**
       - Post a comprehensive closing comment using this structured executive format:

         ````markdown
         🤖 **Platform Agent Triage Report — Issue Resolved**

         > [!NOTE]
         > **Resolution:** <Concise 1-2 sentence executive summary of fix applied / PR created>

         ### 📌 Resolution Overview

         | Attribute           | Value                        |
         | :------------------ | :--------------------------- |
         | **Target Resource** | `<resource name>`            |
         | **Resolution Type** | Code/Manifest Correction     |
         | **Pull Request**    | `PR #<pr-num>`               |
         | **Final Action**    | Resolved (`status:resolved`) |

         ---

         ### 🔍 Ground-Truth Verification Proof

         ```text
         $ <EXACT TERMINAL COMMAND EXECUTED>
         <PASTE EXACT RAW STDOUT PROVING FIX>
         ```
         ````

         ```

         ```

       - Apply label `status:resolved`, remove `status:in-progress`, and close the issue (`gh issue close <number> -R "$GH_REPO" --reason "completed"`).

     - **Case 2: No Change Needed / False Alarm / Decommissioned Cluster (Auto-Close)**
       - Post the closing comment using this structured executive format:

         ````markdown
         🤖 **Platform Agent Triage Report — Auto-Closed (False Alarm / No Action Required)**

         > [!NOTE]
         > **Resolution:** <Concise summary explaining why no action is needed, e.g. decommissioned cluster 404 Not Found>

         ### 📌 Verification Summary

         | Attribute            | Value                           |
         | :------------------- | :------------------------------ |
         | **Target Resource**  | `<resource name>`               |
         | **Diagnostic State** | `<e.g. NOT_FOUND / HEALTHY>`    |
         | **Final Action**     | Auto-Closed (`status:resolved`) |

         ---

         ### 🔍 Ground-Truth Verification Proof

         ```text
         $ <EXACT TERMINAL COMMAND EXECUTED, e.g. gcloud container clusters describe ...>
         <PASTE EXACT RAW STDOUT PROVING HEALTHY/DECOMMISSIONED STATE>
         ```
         ````

         ***

         ### 💡 Recommendation

         <Suggested operational step, e.g. silencing stale monitoring alert policy>

         ```

         ```

       - Apply label `status:resolved`, remove `status:in-progress`, and close the issue (`gh issue close <number> -R "$GH_REPO" --reason "not planned"`).

     - **Case 3: Human Decision or Escalation Required**
       - Post the escalation comment using this structured executive format:

         ````markdown
         🚨 **Platform Agent Triage Report — Human Escalation Required**

         > [!CAUTION]
         > **Executive Summary:** <Concise 1-2 sentence statement of active infrastructure degradation or user deletion>

         ### 📌 Incident Overview

         | Attribute              | Diagnosed Value                        |
         | :--------------------- | :------------------------------------- |
         | **Target Cluster**     | `<cluster_name>` (`<location>`)        |
         | **Affected Namespace** | `<namespace>`                          |
         | **Degraded Component** | `<resource_name>`                      |
         | **Automated Status**   | Escalated (`status:escalation-needed`) |

         ---

         ### 🔍 Forensic Audit Trace & Diagnostic Evidence

         ```yaml
         # Paste structured audit trace or failure stack trace
         Principal: <actor email>
         Timestamp: <timestamp>
         Method: <API operation>
         Client: <user agent / client>
         ```
         ````

         ***

         ### 🛠️ Recommended Remediation

         <Actionable remediation steps and verification commands>
         *Note: Adding label `agent:ignore` permanently excludes this issue from automated monitoring.*
         ```

       - Apply custom status label `status:escalation-needed`, remove `status:in-progress`, and LEAVE THE ISSUE OPEN for human review.

6. **Log to Memory**:
   - Record the issue triage and final state transition in the daily memory log (`memory/YYYY-MM-DD.md`).

## MANDATORY ISSUE TURN COMPLETION CHECKLIST (PRE-RESPONSE ENFORCEMENT)

Before you generate any final text output or stop calling tools on any turn where you triaged an issue `#<number>`, you MUST verify that you have completed both of the following CLI commands:

1. **Comment on Issue:** Post your full diagnostic report directly to the issue via `gh issue comment <number> -R "$GH_REPO" -F <file>` (always using a single-quoted heredoc `cat << 'EOF' > /tmp/msg.md` to avoid shell backtick interpolation).
2. **Transition Issue Label:** Apply either `status:resolved` (if false alarm/repaired) or `status:escalation-needed` (if human review/SRE action needed) (`gh issue edit <number> -R "$GH_REPO" --add-label <label> --remove-label status:in-progress`).
   Do NOT stop tool execution or respond in chat until BOTH commands have successfully executed against the ticket.
