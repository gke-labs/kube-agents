# `proxy` scenario on `main` (build `fb56483b`), 2026-11-04

The harness's `report.md` from `proxy` on install build `fb56483b`, 2026-11-04. Fleet: kwok-01 and kwok-02 with about 2,900 KWOK nodes and 80,000 Pods each; kwok-03 and kwok-04 with Events only. An earlier revision of the harness wrote this report, before it added the `Samples:` line.

## OOMKills: 1

- 2026-11-04T02:57:41Z platform-agent-credential-proxy-7cbbb785c9-z96sd/envoy-credential-proxy exit 137 (restart 1)

### envoy-credential-proxy, kill at 2026-11-04T02:57:41Z

Last sample 02:57:39 UTC: cgroup 1780 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process                                    |
| ----- | ---- | ------------------------------------------ |
| 90.8% | 1720 | `kubectl get deploy,sts,ds,cronjob,pod -A` |
| 4.4%  | 84   | `credential_proxy.py`                      |
| 4.2%  | 78   | `envoy`                                    |
| 0.6%  | 11   | `hermes`                                   |
