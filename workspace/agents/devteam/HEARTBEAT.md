# HEARTBEAT.md - Development Team Agent Scheduled Tasks

As the production-safety coach and workload custodian for your team's services, you execute routine diagnostics and audits via a scheduled routine. To limit token burn and avoid redundant API calls, you must track your execution times and maintain state inside `memory/heartbeat-state.json`.

Each time you receive a heartbeat poll (triggered periodically by the gateway or host harness), you must check `memory/heartbeat-state.json` to see which tasks are due based on their schedules, execute them, and update the timestamps.

---

## Automated Tasks (Cron Jobs)

### 1. Deployment Watch
- **Schedule**: Every 5 minutes
- **Function**: Monitor active rollouts and deployments of team-owned workloads. Alert the team immediately on stalled or degraded service status.

### 2. Error Rate Monitor
- **Schedule**: Every 15 minutes
- **Function**: Analyze error counts and exception logs across team services to identify and alert on sudden spikes before they escalate into major incidents.

### 3. Heartbeat
- **Schedule**: Every 30 minutes
- **Function**: Perform a focused, lightweight health check of all team-owned services to maintain constant situational awareness of application state.

### 4. SLO Compliance Monitor
- **Schedule**: Hourly (Every 60 minutes)
- **Function**: Calculate service-level objectives (SLOs) and error budget burn rates based on metrics. Alert the team proactively before critical breaches occur.

### 5. Cost Efficiency Audit
- **Schedule**: Daily (Every 24 hours)
- **Function**: Analyze workload resource requests vs. actual usage (historical telemetry) to suggest right-sizing opportunities and identify idle resources.

### 6. Performance Baseline Check
- **Schedule**: Daily (Every 24 hours)
- **Function**: Compare current P99 latency and throughput metrics against historical baselines to detect performance regressions.

### 7. Policy/Compliance Scan
- **Schedule**: Weekly (Every 7 days)
- **Function**: Verify team workload configurations against platform security and best-practice policies, providing developers with remediation guidance.

---

## State Management & Rotation

Track your task execution state in `memory/heartbeat-state.json` using this schema:

```json
{
  "lastChecks": {
    "deployment_watch": null,
    "error_rate_monitor": null,
    "service_heartbeat": null,
    "slo_monitor": null,
    "cost_audit": null,
    "performance_check": null,
    "compliance_scan": null
  }
}
```

### Execution Rules
1. **Schedule Compliance**: Before running any task, compare the current timestamp against the last checked timestamp. Only run a task if the required duration (5m, 15m, 30m, 1h, 24h, 7d) has elapsed.
2. **Batching**: Batch multiple due tasks together in a single heartbeat turn when possible.
3. **Alerting & Safety**: Direct critical alerts (like deployment stalls or SLO budget exhaustion) to development team communication channels (Slack). For security compliance gaps (like missing NetworkPolicy), automatically patch the manifest in staging/repo if possible.
4. **Silence Rule (NO_REPLY)**: If all checked tasks are successful, no anomalies are found, and error rates/SLOs are well within healthy thresholds, reply with exactly `NO_REPLY` to respect quiet time.
