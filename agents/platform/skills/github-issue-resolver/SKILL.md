---
name: github-issue-resolver
description:
  Autonomously poll, triage, investigate, and resolve unaddressed open issues on
  our target GitHub repository strictly within authorized scope.
---

# Skill: github-issue-resolver

> [!CAUTION] **INVIOLABLE SAFETY RED LINE:** NEVER inspect, comment on, edit,
> close, or modify any issue labeled `status:escalation-needed`, `agent:ignore`,
> or `agent:audit`. Issues labeled `status:escalation-needed` are locked for
> human intervention and must NEVER be modified or closed autonomously. Issues
> labeled `agent:audit` are `fleet-audit` ledgers, rewritten in place by that
> skill on every run — touching one corrupts a report the audit owns.

This skill delegates all deterministic GitHub CLI operations, label creation,
stale sweeps, and safe comment uploading to the helper script
`"$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py`. The LLM's
role is strictly constrained to **reasoning, diagnostic investigation, and root
cause determination**.

The script path is spelled out from `$HERMES_HOME` rather than as `./skills/…`
because you now reach this skill from a kanban card as well as from a cron turn,
and a card dispatch starts you in the task's workspace, not the profile
directory. `$HERMES_HOME` is the profile directory in both.

## Procedure

### Step 1: Poll Unaddressed Issues

Run the deterministic polling script to sweep stale investigations and check for
new unaddressed open issues:

```bash
"$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py poll
```

Run it even when a kanban card sent you here already naming an issue. The
`github-repo-watcher` cron job polls on your behalf and files that card, but the
card is a pointer written minutes ago, not a transcript: the issue may have been
claimed, closed, or labelled `agent:ignore` since. Re-reading the truth costs one
API call. It also performs the stale sweep, which the card cannot.

- If the script outputs `{"status": "NO_ISSUES", ...}`, there is nothing to do.
  End the turn per [Ending the turn](#ending-the-turn). Arriving here from a card
  is normal and is not a fault — it means the issue was addressed between the
  poll and your dispatch.
- If the script outputs `{"status": "NOT_CONFIGURED"}`, this deployment has no
  target repository. That is a supported state, not a fault. End the turn per
  [Ending the turn](#ending-the-turn).
- If the script outputs `{"status": "ERROR", "reason": <reason>, ...}`, the
  resolver could not run. This is a fault that would otherwise recur silently on
  every poll, so it is never silent: alert the chat room with
  `⚠️ **GitHub issue resolver is not running:** <reason>`, then end the turn per
  [Ending the turn](#ending-the-turn) — on a card, `kanban_block` rather than
  `kanban_complete`.
- If the script outputs `{"status": "FOUND", "issue_number": <number>, ...}`,
  proceed to Step 2.

### Step 2: Claim the Issue

Immediately claim the issue before starting your investigation so other agents
or engineers do not duplicate work:

```bash
"$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py claim --issue <number>
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
     "$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition --issue <number> --state resolved --report-file /opt/data/scratch/report_<number>.md
     ```
     - Then end the turn per [Ending the turn](#ending-the-turn).

   - **Case B: Human Review / SRE Action Needed (`status:escalation-needed`)**:
     ```bash
     "$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition --issue <number> --state escalation-needed --report-file /opt/data/scratch/report_<number>.md
     ```
     - You MUST message the chat room to alert the on-call engineer:
       `🚨 **Human Escalation Required — Action Needed:**`
       `- [#<number> (<Title>)](https://github.com/<owner>/<repo>/issues/<number>) — *<1-sentence summary of root cause requiring human intervention>*`
     - Then end the turn per [Ending the turn](#ending-the-turn).

## Ending the turn

Two callers reach this skill, and they end differently. Check `$HERMES_KANBAN_TASK`.

- **Dispatched from a kanban card** (`$HERMES_KANBAN_TASK` is set) — the usual
  case, because `github-repo-watcher` files a card for every issue it finds. Call
  `kanban_complete(result=..., summary=...)`, or `kanban_block(kind=...)` if you
  could not finish. **Never end a card run without one of them**, whatever the
  outcome and however little there was to do: a worker that just stops exits
  rc=0, is reaped as a `protocol_violation`, and burns one of the card's
  attempts. `result` is the only field the requester receives, so put the outcome
  there — the issue number and
  what you did, or one line saying there was nothing to do. Do **not** answer
  `[SILENT]`: the card is the channel, and a completed card notifies nobody who
  was not already subscribed.
- **Any other caller** — a cron turn, or a person asking in chat. Where the steps
  above say the outcome is silent (`NO_ISSUES`, `NOT_CONFIGURED`,
  `status:resolved`), your final turn response MUST BE exactly `[SILENT]`, to
  suppress chat noise.

Either way an `ERROR` from Step 1 and an escalation from Step 3 are never silent:
post the chat message the step names first, then end the turn.

## MANDATORY ISSUE TURN COMPLETION CHECKLIST

Before ending any turn where an issue `#<number>` was claimed, you MUST verify:

1. **Deterministic Transition Called:** `"$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition` was executed
   with your report file (`/opt/data/scratch/report_<number>.md`).
2. **Chat Alert Handled:** If `status:escalation-needed`, you posted the chat
   alert.
3. **The Turn Is Ended Correctly:** per [Ending the turn](#ending-the-turn) —
   `kanban_complete` / `kanban_block` on a card, `[SILENT]` otherwise. This
   applies to every exit from this skill, including the ones with nothing to
   report.
