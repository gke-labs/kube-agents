---
name: github-issue-resolver
description:
  Autonomously poll, triage, investigate, and resolve unaddressed open issues on
  our target GitHub repository strictly within authorized scope.
---

# Skill: github-issue-resolver

> [!CAUTION] **INVIOLABLE SAFETY RED LINE:** NEVER inspect, comment on, edit,
> close, or modify any issue labeled `status:escalation-needed` or
> `agent:ignore`. Issues labeled `status:escalation-needed` are locked for human
> intervention and must NEVER be modified or closed autonomously.

This skill delegates all deterministic GitHub CLI operations, label creation,
stale sweeps, and safe comment uploading to the helper script
`scripts/resolver.py`. The LLM's
role is strictly constrained to **reasoning, diagnostic investigation, and root
cause determination**.

## Procedure

### Step 1: Poll Unaddressed Issues

Run the deterministic polling script to sweep stale investigations and check for
new unaddressed open issues:

```bash
python3 scripts/resolver.py poll
```

- If the script outputs `{"status": "NO_ISSUES", ...}`, your final response MUST
  BE exactly `[SILENT]` to suppress chat noise. Terminate the turn immediately.
- If the script outputs `{"status": "FOUND", "issue_number": <number>, ...}`,
  proceed to Step 2.

### Step 2: Claim the Issue

Immediately claim the issue before starting your investigation so other agents
or engineers do not duplicate work:

```bash
python3 scripts/resolver.py claim --issue <number>
```

### Step 3: Investigate & Diagnose (Reasoning Phase)

Use your available read-only diagnostic tools (`kubectl`, `gcloud`,
`skill_view`, etc.) and system logs (`/opt/data/`) to investigate the root cause
of the issue:

- Extract symptoms, cluster names, and stack traces from the issue title, body, and comments returned during polling.
- If the issue matches a known operational scenario (e.g. an "Unhealthy Config
  Controller Instance" alert), check if there is an existing diagnostic skill
  and execute its diagnostic checks.
- Formulate a clear, executive forensic analysis with exact evidence.

### Step 4: Report Findings & Transition State

Once your investigation is complete:

1. **Write your Executive Triage Report to a temporary file:** Use the
   `write_to_file` tool to write your formatted Markdown report to
   `/opt/data/scratch/report_<number>.md`.
2. **Execute the deterministic transition script:** The script safely uploads
   your report directly to GitHub via `-F` (preventing any shell escaping,
   ampersand backgrounding errors, or quote syntax bugs) and transitions the
   ticket:

   - **Case A: Issue Resolved / False Alarm (`status:resolved`)**:

     ```bash
     python3 scripts/resolver.py transition --issue <number> --state resolved --report-file /opt/data/scratch/report_<number>.md
     ```
     - Your final turn response MUST BE exactly `[SILENT]`.

   - **Case B: Human Review / SRE Action Needed (`status:escalation-needed`)**:
     ```bash
     python3 scripts/resolver.py transition --issue <number> --state escalation-needed --report-file /opt/data/scratch/report_<number>.md
     ```
     - You MUST message the chat room to alert the on-call engineer:
       `🚨 **Human Escalation Required — Action Needed:**`
       `- [#<number> (<Title>)](https://github.com/<owner>/<repo>/issues/<number>) — *<1-sentence summary of root cause requiring human intervention>*`

## MANDATORY ISSUE TURN COMPLETION CHECKLIST

Before ending any turn where an issue `#<number>` was claimed, you MUST verify:

1. **Deterministic Transition Called:** `resolver.py transition` was executed
   with your report file (`/opt/data/scratch/report_<number>.md`).
2. **Chat Alert Handled:** If `status:escalation-needed`, you posted the chat
   alert. If `status:resolved` or `NO_ISSUES`, your final response is exactly
   `[SILENT]`.
