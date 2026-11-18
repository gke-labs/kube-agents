# `watcher --size L --runc` scenario on `main` (build `b3d78bc8`), 2026-11-04

The harness's `report.md`. The scenario set `agent-api-auth` to this harness's L preset, 2 CPU and 4 GB. Fleet: four clusters with 900,000 to 990,000 Events each: 400,000 with short messages (`events --count 400000`) and 500,000 padded to 1 KB (`events --count 500000 --pad-to 1024`), injected within the hour before the run.

## OOMKills: 3

Samples: 104 from 2 pod(s) of `agent-api-auth`: platform-agent-gateway-5ccc5cd646-jjsmr, platform-agent-gateway-6d7c7f5f77-zbdg5.

- 2026-11-04T05:26:00Z platform-agent-gateway-6d7c7f5f77-zbdg5/agent-api-auth exit 137 (restart 1)
- 2026-11-04T05:27:02Z platform-agent-gateway-6d7c7f5f77-zbdg5/agent-api-auth exit 137 (restart 2)
- 2026-11-04T05:28:19Z platform-agent-gateway-6d7c7f5f77-zbdg5/agent-api-auth exit 137 (restart 3)

### agent-api-auth, kill at 2026-11-04T05:26:00Z

Last sample 05:25:59 UTC: cgroup 3771 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.5% | 3722 | `k8s-event-watcher`   |
| 1.5%  | 55   | `credential_proxy.py` |

### agent-api-auth, kill at 2026-11-04T05:27:02Z

Last sample 05:27:01 UTC: cgroup 3734 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.5% | 3685 | `k8s-event-watcher`   |
| 1.5%  | 56   | `credential_proxy.py` |

### agent-api-auth, kill at 2026-11-04T05:28:19Z

Last sample 05:28:18 UTC: cgroup 3811 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.5% | 3762 | `k8s-event-watcher`   |
| 1.5%  | 56   | `credential_proxy.py` |
