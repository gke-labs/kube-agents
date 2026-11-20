# `proxy --size L` scenario (watching the shell) on `main` (build `b3d78bc8`), 2026-11-04

The harness's `report.md`. The scenario set the shell to this harness's L preset, 8 CPU and 8 GB, through `spec.harness.experimental.shellSandbox.resources`. Fleet: kwok-01 and kwok-02 with about 2,900 KWOK nodes and 86,000 to 89,000 ready Pods each. The two `kubectl` processes in the last sample were 3,857 and 3,500 MiB.

## OOMKills: 1

Samples: 754 from 1 pod(s) of `shell`: platform-agent-shell-0.

- 2026-11-04T04:39:11Z platform-agent-shell-0/shell exit 137 (restart 1)

### shell, kill at 2026-11-04T04:39:11Z

Last sample 04:39:09 UTC: cgroup 7386 MiB, of which the sampler 10 MiB (not in the shares below).

| Share | MiB  | Process                                    |
| ----- | ---- | ------------------------------------------ |
| 98.7% | 7358 | `kubectl get deploy,sts,ds,cronjob,pod -A` |
| 0.8%  | 60   | `collect.py`                               |
| 0.4%  | 33   | `other`                                    |
