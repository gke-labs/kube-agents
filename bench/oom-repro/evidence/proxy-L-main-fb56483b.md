# Credential proxy at the L limits on `main` (build `fb56483b`), 2026-11-04

The harness's `report.md` from an earlier revision of the harness (before the `Samples:` line and `--watch`; it watched the credential proxy), from a build before the Kubernetes API proxy, where `kubectl` ran inside the credential proxy. Fleet: kwok-01 and kwok-02 with about 2,900 KWOK nodes and 80,000 Pods each. The two `kubectl` children in the last sample were 4,272 and 3,238 MiB.

## OOMKills: 1

- 2026-11-04T03:24:50Z platform-agent-credential-proxy-555fb97dc4-8hnpc/envoy-credential-proxy exit 137 (restart 1)

### envoy-credential-proxy, kill at 2026-11-04T03:24:50Z

Last sample 03:24:50 UTC: cgroup 7626 MiB, of which the sampler 11 MiB (not in the shares below).

| Share | MiB  | Process                                    |
| ----- | ---- | ------------------------------------------ |
| 97.1% | 7511 | `kubectl get deploy,sts,ds,cronjob,pod -A` |
| 1.6%  | 125  | `envoy`                                    |
| 1.2%  | 91   | `credential_proxy.py`                      |
| 0.2%  | 11   | `hermes`                                   |
