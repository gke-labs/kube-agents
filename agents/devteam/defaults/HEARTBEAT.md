# HEARTBEAT.md - Development Team Agent Scheduled Tasks

As the production-safety coach and workload custodian for your team's services, you execute routine diagnostics and audits via a scheduled routine. To limit token burn and avoid redundant API calls, you must track your execution times and maintain state inside `memory/heartbeat-state.json`.

Each time you receive a heartbeat poll (triggered periodically by the agent harness), you must check `memory/heartbeat-state.json` to see which tasks are due based on their schedules, execute them, and update the timestamps.

---

## Automated Tasks (Cron Jobs)

### GitOps & Drift Reconciliation

- **Schedule**: Every 5 minutes
- **Function**: Audit both the tracking GitHub branch (e.g., `main`) and the live cluster namespace. If configuration drift is detected—either because new commits have been merged to GitHub (cluster is lagging behind) or because manual, out-of-band changes have been made to the live namespace—immediately correct the drift. If the live namespace was modified out-of-band, you **must revert the changes to the latest GitHub code/manifest** so there is zero configuration drift, maintaining GitHub as the absolute source of truth.

### Deployment Watch

- **Schedule**: Every 5 minutes
- **Function**: Monitor active rollouts and deployments of team-owned workloads. Alert the team immediately on stalled or degraded service status.

### Error Rate Monitor

- **Schedule**: Every 15 minutes
- **Function**: Analyze error counts and exception logs across team services to identify and alert on sudden spikes before they escalate into major incidents.

### Service Heartbeat Check

- **Schedule**: Every 30 minutes
- **Function**: Perform a focused, lightweight health check of all team-owned services to maintain constant situational awareness of application state.

### SLO Compliance Monitor

- **Schedule**: Hourly (Every 60 minutes)
- **Function**: Calculate service-level objectives (SLOs) and error budget burn rates based on metrics. Alert the team proactively before critical breaches occur.

### Cost Efficiency Audit

- **Schedule**: Daily (Every 24 hours)
- **Function**: Analyze workload resource requests vs. actual usage (historical telemetry) to suggest right-sizing opportunities and identify idle resources.

### Performance Baseline Check

- **Schedule**: Daily (Every 24 hours)
- **Function**: Compare current P99 latency and throughput metrics against historical baselines to detect performance regressions.

### Policy/Compliance Scan

- **Schedule**: Weekly (Every 7 days)
- **Function**: Verify team workload configurations against platform security and best-practice policies, providing developers with remediation guidance.

---

## State Management & Heartbeat Output Contract

On every heartbeat poll, you **must** execute the GitOps and drift reconciliation audits, and write your execution state inside `memory/heartbeat-state.json` using this exact contract schema:

```json
{
  "lastHeartbeatPollAt": 1703275200,
  "gitCommit": "HEAD_SHA_HERE",
  "originUrl": "git@github.com:dshnayder/demo.git",
  "reconciled": true,
  "blocker": null
}
```

### Heartbeat Fields Specifications

- `lastHeartbeatPollAt`: The Unix timestamp representing when the heartbeat check completed.
- `gitCommit`: The exact git `HEAD` commit SHA that was fetch/reconciled from `origin/main`.
- `originUrl`: The target Git repository URL configured for this workspace.
- `reconciled`: Set to `true` if the cluster namespace is synchronized and matching Git (drift is zero). Set to `false` if GitOps reconciliation is pending or blocked.
- `blocker`: Set to `null` if healthy and reconciled. If blocked (due to authentication, path missing, manifest schema violation, or validation failures), populate this field with a concise object details:
  ```json
  {
    "command": "failed git/kubectl command",
    "path": "missing manifest or repository directory path",
    "remediation": "exact manual step required to resolve the issue"
  }
  ```

### Execution Rules

1. **Continuous Drift Reconciliation**: On every heartbeat poll (Every 5 minutes), fetch the latest commits from Git, check cluster namespace resources, and reconcile to maintain GitHub as the absolute source of truth.
2. **Standard State Writing**: You **must** write to `memory/heartbeat-state.json` on every execution turn. Do not skip writing.
3. **Alerting & Silence**: If completely reconciled and healthy, reply to the heartbeat poll with exactly `NO_REPLY` to limit chat noise. If blocked or drift correction fails, fail loudly by returning the blockers details.
