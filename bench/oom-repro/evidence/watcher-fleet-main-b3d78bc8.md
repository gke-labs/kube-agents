# `watcher --size fleet --runc` scenario on `main` (build `b3d78bc8`), 2026-11-04

The harness's `report.md`. The scenario set `agent-api-auth` to the sizing page's fleet tier: a 4 GiB memory limit, CPU limit 1. Fleet: four clusters with about 900,000 Events each (400,000 short, 500,000 padded to 1 KB), the same Event batches as the `--size L` watcher run, which ended 6 minutes before this run started.

## OOMKills: 3

Samples: 174 from 2 pod(s) of `agent-api-auth`: platform-agent-gateway-5ccc5cd646-5pfpd, platform-agent-gateway-76c4db5cb5-k45d2.

- 2026-11-04T05:36:53Z platform-agent-gateway-76c4db5cb5-k45d2/agent-api-auth exit 137 (restart 1)
- 2026-11-04T05:38:50Z platform-agent-gateway-76c4db5cb5-k45d2/agent-api-auth exit 137 (restart 2)
- 2026-11-04T05:40:54Z platform-agent-gateway-76c4db5cb5-k45d2/agent-api-auth exit 137 (restart 3)

### agent-api-auth, kill at 2026-11-04T05:36:53Z

Last sample 05:36:51 UTC: cgroup 4053 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.6% | 4005 | `k8s-event-watcher`   |
| 1.4%  | 56   | `credential_proxy.py` |

### agent-api-auth, kill at 2026-11-04T05:38:50Z

Last sample 05:38:49 UTC: cgroup 4072 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.6% | 4023 | `k8s-event-watcher`   |
| 1.4%  | 56   | `credential_proxy.py` |

### agent-api-auth, kill at 2026-11-04T05:40:54Z

Last sample 05:40:52 UTC: cgroup 4028 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 98.6% | 3980 | `k8s-event-watcher`   |
| 1.4%  | 55   | `credential_proxy.py` |
