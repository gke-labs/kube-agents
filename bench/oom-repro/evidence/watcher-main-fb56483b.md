# `watcher` scenario on `main` (build `fb56483b`), 2026-11-04

The harness's `report.md` from `watcher` on install build `fb56483b`, 2026-11-04. Fleet: kwok-01 and kwok-02 with about 2,900 KWOK nodes and 80,000 Pods each; kwok-03 and kwok-04 with Events only; 400,000 Events in each of the four. An earlier revision of the harness wrote this report, before it added the `Samples:` line.

## OOMKills: 3

- 2026-11-04T03:18:29Z platform-agent-gateway-59f987f6dd-6xzfz/agent-api-auth exit 137 (restart 1)
- 2026-11-04T03:19:27Z platform-agent-gateway-59f987f6dd-6xzfz/agent-api-auth exit 137 (restart 2)
- 2026-11-04T03:20:37Z platform-agent-gateway-59f987f6dd-6xzfz/agent-api-auth exit 137 (restart 3)

### agent-api-auth, kill at 2026-11-04T03:18:29Z

Last sample 03:18:26 UTC: cgroup 1955 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 97.2% | 1917 | `k8s-event-watcher`   |
| 2.8%  | 55   | `credential_proxy.py` |

### agent-api-auth, kill at 2026-11-04T03:19:27Z

Last sample 03:19:25 UTC: cgroup 2017 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 97.3% | 1979 | `k8s-event-watcher`   |
| 2.7%  | 55   | `credential_proxy.py` |

### agent-api-auth, kill at 2026-11-04T03:20:37Z

Last sample 03:20:36 UTC: cgroup 2024 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process               |
| ----- | ---- | --------------------- |
| 97.3% | 1987 | `k8s-event-watcher`   |
| 2.7%  | 55   | `credential_proxy.py` |
