# `proxy` scenario (watching the shell) on `main` (build `b3d78bc8`), 2026-11-04

The harness's report, re-rendered from the run's samples with the current process classes. The scenario set the shell to the operator's default limit, 2 CPU and 2 GiB, through `spec.harness.experimental.shellSandbox.resources`. Fleet: kwok-01 and kwok-02 with about 2,900 KWOK nodes and 86,000 to 89,000 ready Pods each. A sample shows which command held the memory, not which agent work started it.

## OOMKills: 3

Samples: 195 from 1 pod(s) of `shell`: platform-agent-shell-0.

- 2026-11-04T04:34:01Z platform-agent-shell-0/shell exit 137 (restart 8)
- 2026-11-04T04:34:38Z platform-agent-shell-0/shell exit 137 (restart 9)
- 2026-11-04T04:36:30Z platform-agent-shell-0/shell exit 137 (restart 10)

### shell, kill at 2026-11-04T04:34:01Z

Last sample 04:33:59 UTC: cgroup 1997 MiB, of which the sampler 10 MiB (not in the shares below).

| Share | MiB  | Process                  |
| ----- | ---- | ------------------------ |
| 94.3% | 1956 | `kubectl get pods -A`    |
| 3.2%  | 66   | `other`                  |
| 2.5%  | 51   | `kubectl get daemonsets` |

### shell, kill at 2026-11-04T04:34:38Z

Last sample 04:34:36 UTC: cgroup 2016 MiB, of which the sampler 10 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.4% | 2028 | `kubectl get pods -A` |
| 1.6%  | 33   | `other`               |

### shell, kill at 2026-11-04T04:36:30Z

Last sample 04:36:29 UTC: cgroup 2020 MiB, of which the sampler 10 MiB (not in the shares below).

| Share | MiB  | Process                                    |
| ----- | ---- | ------------------------------------------ |
| 95.5% | 2005 | `kubectl get deploy,sts,ds,cronjob,pod -A` |
| 2.9%  | 60   | `collect.py`                               |
| 1.6%  | 33   | `other`                                    |
