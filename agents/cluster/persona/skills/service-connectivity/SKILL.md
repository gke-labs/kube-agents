---
name: service-connectivity
description: Finds why one workload cannot reach another service in the cluster or hits DNS resolution failures. Use for connection timeouts, "dial tcp i/o timeout", "connection refused" to a Service, a Service with no endpoints, traffic blocked by a NetworkPolicy, and DNS resolution timeouts ("lookup ... i/o timeout", "no such host", "Temporary failure in name resolution", "SERVFAIL") or kube-dns saturation during rapid node scaling. Reads the Service, its endpoints, NetworkPolicies, and kube-system DNS components (kube-dns, kube-dns-autoscaler, node-local-dns) plus cluster DNS settings, and proposes the fix.
---

# Task

Name why traffic from a source workload to a target Service fails or why DNS resolution times out, quote the evidence, and propose the fix.

# Workflow

## Service-to-service connectivity

1. Read the target Service and its ready endpoints:

   ```bash
   kubectl get service <target_service> -n <target_namespace> -o custom-columns='NAME:.metadata.name,TYPE:.spec.type,SELECTOR:.spec.selector,PORTS:.spec.ports[*].port,TARGET:.spec.ports[*].targetPort'
   kubectl get endpointslices -n <target_namespace> -l kubernetes.io/service-name=<target_service> -o custom-columns='NAME:.metadata.name,ADDRESSES:.endpoints[*].addresses[*],READY:.endpoints[*].conditions.ready,PORTS:.ports[*].port'
   ```

2. If there are no ready endpoints, the target itself is failing or its selector matches nothing. Check the selector against the target pods' labels, then load the skill the target's state calls for (`pod-crashloop-oom`, `pod-pending-scheduling`, `pod-container-creating`):

   ```bash
   kubectl get pods -n <target_namespace> -l <selector> -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready'
   ```

3. If endpoints are ready, read the NetworkPolicies on both sides, projected to selectors and rules:

   ```bash
   kubectl get networkpolicies -n <source_namespace> -o json | jq '[.items[] | {name: .metadata.name, podSelector: .spec.podSelector, policyTypes: .spec.policyTypes, egress: .spec.egress, ingress: .spec.ingress}]'
   ```

   Repeat for `<target_namespace>` when it differs.

## DNS resolution timeouts and kube-dns saturation

When workloads report DNS errors (`lookup ... i/o timeout`, `no such host`, `Temporary failure in name resolution`, `SERVFAIL`) or DNS timeouts during rapid node scaling, inspect `kube-system` directly without skipping it:

1. Read cluster mode, DNS provider, and NodeLocal DNSCache settings together with `kube-system` DNS components (`--ignore-not-found` prevents optional `kube-dns` / `node-local-dns` reads from aborting before `kubectl get nodes` on Autopilot or `CLOUD_DNS` clusters):

   ```bash
   gcloud container clusters describe <cluster_name> --location <cluster_location> --project <project_id> --format="json(autopilot,networkConfig.dnsConfig,addonsConfig.dnsCacheConfig)"
   kubectl get deployment kube-dns kube-dns-autoscaler -n kube-system --ignore-not-found -o custom-columns='NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas'
   kubectl get configmap kube-dns-autoscaler -n kube-system --ignore-not-found -o jsonpath='{.data}'
   kubectl get daemonset node-local-dns -n kube-system --ignore-not-found -o custom-columns='NAME:.metadata.name,DESIRED:.status.desiredNumberScheduled,READY:.status.numberReady'
   kubectl get nodes -o custom-columns='NAME:.metadata.name,CPU:.status.capacity.cpu'
   ```

   GKE Autopilot defaults to `CLOUD_DNS` with mandatory NodeLocal DNSCache and does not support editing `ConfigMap/kube-dns-autoscaler`. On Standard, an omitted `addonsConfig.dnsCacheConfig` key or `enabled: false` means NodeLocal DNSCache is disabled (Standard 1.34.1+ enables NodeLocal DNSCache by default, though it can be disabled). When `networkConfig.dnsConfig.clusterDns` is `CLOUD_DNS`, `Deployment/kube-dns` is absent on clusters created with `CLOUD_DNS` (or scaled to 0 after migration), whereas Standard clusters migrated from `kube-dns` continue running `kube-dns` until manually scaled to 0.

2. Inspect DNS pod status, restarts, resource requests/limits, live CPU/memory usage, recent `kube-system` Warning events, and logs (if `kubectl top` is unavailable because Metrics Server is absent, note that and rely on pod status, limits, restarts, and logs; `-c dnsmasq` applies to legacy multi-container `kube-dns` on GKE 1.35 and earlier, whereas GKE 1.36+ runs CoreDNS in the single `kubedns` container; on Autopilot, or on `CLOUD_DNS` clusters where `Deployment/kube-dns` is absent or scaled to 0, focus on `node-local-dns` pods and `kube-system` Warning events rather than `kube-dns-autoscaler`):

   ```bash
   kubectl get pods -n kube-system -l k8s-app=kube-dns -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,CPU_REQ:.spec.containers[*].resources.requests.cpu,CPU_LIM:.spec.containers[*].resources.limits.cpu,MEM_REQ:.spec.containers[*].resources.requests.memory,MEM_LIM:.spec.containers[*].resources.limits.memory,NODE:.spec.nodeName'
   kubectl top pods -n kube-system -l k8s-app=kube-dns
   kubectl get events -n kube-system --field-selector type=Warning
   kubectl logs -n kube-system -l k8s-app=kube-dns -c kubedns --tail=100
   kubectl logs -n kube-system -l k8s-app=kube-dns -c dnsmasq --tail=100
   ```

   If `DaemonSet/node-local-dns` exists and has unready or restarting pods, also inspect `kubectl get pods -n kube-system -l k8s-app=node-local-dns` and its logs. If only one namespace sees DNS failures, check that namespace's `NetworkPolicy` egress rules for UDP/TCP 53 to `kube-system` DNS pods (`kube-dns` / `node-local-dns`, or an `ipBlock` for the `kube-dns` Service `ClusterIP/32` on non-Dataplane-V2 clusters with legacy NetworkPolicy) and to `169.254.20.10` / `169.254.169.254`.

3. Branch the diagnosis and recommendations by cluster DNS provider:
   - **GKE Autopilot, or Standard `CLOUD_DNS` with no running `Deployment/kube-dns` (`networkConfig.dnsConfig.clusterDns: CLOUD_DNS` and `Deployment/kube-dns` absent or scaled to 0):** Confirm `CLOUD_DNS` and NodeLocal DNSCache (`DaemonSet/node-local-dns` / `addonsConfig.dnsCacheConfig.enabled: true`) are active, explain that `kube-dns` autoscaling does not apply, and do **not** propose `ConfigMap/kube-dns-autoscaler` edits or Standard-cluster `kube-dns-autoscaler` tuning blocks (Autopilot does not support them — even if legacy `kube-dns` / `kube-dns-autoscaler` system objects still exist in `kube-system` — and `CLOUD_DNS` with no active `kube-dns` bypasses `kube-dns`). Note the resolver path (`CLOUD_DNS` + NodeLocal DNSCache sends pod queries to `169.254.20.10` and forwards cache misses to `169.254.169.254`; `CLOUD_DNS` without NodeLocal DNSCache resolves directly against `169.254.169.254`) and instead inspect `NetworkPolicy` egress to `169.254.20.10` / `169.254.169.254` (UDP/TCP 53), `node-local-dns` pod health and logs, and Cloud DNS quotas, forwarding zones, or upstream resolver latency. If a Standard cluster shows `CLOUD_DNS` while `Deployment/kube-dns` still has running replicas mid-migration, pods on un-recreated node pools still resolve through `kube-dns`, so continue inspecting `kube-dns` and `ConfigMap/kube-dns-autoscaler` under the Standard `kube-dns` branch below and note completing node-pool recreation.
   - **Standard cluster on `kube-dns`:** Compare the observed `kube-dns` replica count, pod resource headroom, and `ConfigMap/kube-dns-autoscaler` formula (`replicas = max(ceil(cores / coresPerReplica), ceil(nodes / nodesPerReplica))`, clamped to `[min, max]`) against rapid node scaling bursts. GKE Standard's default (`coresPerReplica: 256`, `nodesPerReplica: 16`, `preventSinglePointFailure: true`, `includeUnschedulableNodes: true`; `min` unset/1, so a 1-node cluster runs 1 replica and 2+ nodes get a 2-replica floor via `preventSinglePointFailure`) scales slowly during sudden node bursts and leaves pods exposed to `kube-dns` CPU/memory throttling, `dnsmasq` concurrent-query limits, and node UDP conntrack saturation (amplified by `ndots:5` when NodeLocal DNSCache is off). Propose concrete read-only YAML and configuration changes:
     - Tune `ConfigMap/kube-dns-autoscaler` `linear` JSON: lower `nodesPerReplica` (for example `4`–`8`) and/or `coresPerReplica` (for example `64`–`128`), set both an explicit `min` (for example `4`) for baseline burst headroom and an explicit `max` cap (up to node count, capped at `500` on large clusters so `kube-dns` API watches do not overload the control plane), and keep `preventSinglePointFailure: true`.
     - Enable NodeLocal DNSCache (`addonsConfig.dnsCacheConfig.enabled: true`) when disabled — or confirm its active status if already enabled — to cache lookups locally on each node (`kube-dns` + NodeLocal DNSCache keeps the `kube-dns` Service ClusterIP in `/etc/resolv.conf` intercepted locally and upgrades upstream queries to TCP, eliminating per-lookup UDP conntrack exhaustion; enabling it on Standard recreates nodes, so schedule a maintenance window).
     - Recommend migrating node pools to Cloud DNS for GKE (`networkConfig.dnsConfig.clusterDns: CLOUD_DNS`) when scaling velocity routinely exceeds `kube-dns` autoscaler headroom (`CLOUD_DNS` + NodeLocal DNSCache uses `169.254.20.10` forwarding misses to `169.254.169.254`, while `CLOUD_DNS` without NodeLocal DNSCache queries `169.254.169.254` directly; existing Standard node pools must be recreated during migration, and `Deployment/kube-dns` continues running on migrated clusters until manually scaled to 0).

## Reporting

Record the finding as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes. Stop once the cause, the evidence and the patch are written.

# Signatures

- Empty endpoints with matching pods Ready: the Service selector or `targetPort` is wrong; correct it.
- An `Egress` policy on the source pods with no rule allowing the target's pod labels and port: add the egress rule. Include DNS (UDP and TCP 53 to `kube-dns`) if egress is default-deny.
- An `Ingress` policy on the target pods with no rule for the source: add the ingress rule.
- `connection refused` with ready endpoints: the container is not listening on `targetPort`; compare it with the container's port and command.
- `lookup ... i/o timeout`, `no such host`, `Temporary failure in name resolution`, or `SERVFAIL` during rapid node scaling on a Standard `kube-dns` cluster with `kube-dns` high CPU/memory utilization, restarts, `dnsmasq` concurrent-query errors, or coarse `ConfigMap/kube-dns-autoscaler` ratios (`nodesPerReplica` / `coresPerReplica` / low `min`): propose a tighter `ConfigMap/kube-dns-autoscaler` `linear` override (with explicit `min` and `max`) and recommend enabling NodeLocal DNSCache (`addonsConfig.dnsCacheConfig.enabled: true`) or migrating to Cloud DNS (`CLOUD_DNS`).
- DNS timeouts during scale-outs with `DaemonSet/node-local-dns` absent and `addonsConfig.dnsCacheConfig` omitted or disabled: every pod UDP lookup to the `kube-dns` ClusterIP takes a node conntrack entry (multiplied by `ndots:5`) and saturates `kube-dns` before `kube-dns-autoscaler` adds replicas; recommend enabling NodeLocal DNSCache (in a maintenance window on Standard) or migrating to Cloud DNS (`CLOUD_DNS`), plus tighter `kube-dns-autoscaler` overrides if staying on `kube-dns`.
- DNS timeouts on GKE Autopilot, or on a `CLOUD_DNS` cluster with `Deployment/kube-dns` absent or scaled to 0: confirm `CLOUD_DNS` and NodeLocal DNSCache status, do not propose `ConfigMap/kube-dns-autoscaler` edits, and check `NetworkPolicy` egress to `169.254.20.10` / `169.254.169.254`, `node-local-dns` pod health/logs, and Cloud DNS quotas or forwarding latency.
- `Deployment/kube-dns-autoscaler` below desired replicas or crashing: `kube-dns` cannot scale out with new nodes; inspect `kube-dns-autoscaler` pods and events.

# Rules

- Budget about five commands for service-to-service checks, or up to eight batched read-only commands for DNS diagnostics. Do not `exec` into pods to test connectivity, and never run mutating `kubectl` (`patch`, `apply`, `edit`, `scale`, `delete`) or `gcloud` (`--update-addons`, `update`) commands — not even to probe whether Autopilot or RBAC blocks them; the boundary is read-only. Once `gcloud container clusters describe` confirms Autopilot, or confirms `CLOUD_DNS` with `Deployment/kube-dns` absent or scaled to 0, stop inspecting `kube-dns-autoscaler`.
- If a live read fails or is refused, do not retry the cluster in a loop or write a patch from workspace manifests: when the cluster API is unreachable before any evidence is gathered, stop and report as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) ("Cannot proceed") describes, quoting the exact stderr line; when an individual `kubectl` read mid-investigation is refused (`Error from server (Forbidden)`, HTTP 403), treat it as terminal for that query only and complete with what you read, naming that refusal as the gap.
