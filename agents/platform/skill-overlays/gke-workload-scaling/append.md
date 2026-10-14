<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Before recommending GPU/TPU or large-shape capacity for a scale-up

Before recommending capacity for a GPU/TPU or large-shape scale-up, load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its diagnostics: the
regional quota for the exact accelerator metric, then live obtainability advice for the requested
shape across zones and provisioning models — and, for a deadline-bound batch scale-up, its
**Future windows** section (`gcloud beta compute advice calendar-mode`). That skill owns what to
probe and how to report it; follow it rather than restating it here.
