<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Before scheduling a GPU/TPU batch job with a deadline

Before recommending a start time, zone, or capacity path for a GPU/TPU or large-shape batch job —
especially one that must finish inside a horizon — load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its **Future windows**
section: verify the regional quota for the exact accelerator metric, probe
`gcloud beta compute advice calendar-mode` once per candidate region for the job's shape, count,
duration, and horizon, and rank the returned windows. That skill owns the probe's flags, the
chips-per-node arithmetic, the ranking rule, and the paired ProvisioningRequest + LocalQueue
shapes; follow it rather than restating them here.
