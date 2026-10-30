---
name: service-connectivity
description: Finds why one workload cannot reach another service in the cluster. Use for connection timeouts, "dial tcp i/o timeout", "connection refused" to a Service, a Service with no endpoints, and traffic blocked by a NetworkPolicy. Reads the Service, its endpoints and the namespace's NetworkPolicies, and proposes the fix.
---

# Task

Name why traffic from a source workload to a target Service fails, quote the evidence, and propose the fix.

# Workflow

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

4. Record the finding as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes. Stop once the cause, the evidence and the patch are written.

# Signatures

- Empty endpoints with matching pods Ready: the Service selector or `targetPort` is wrong; correct it.
- An `Egress` policy on the source pods with no rule allowing the target's pod labels and port: add the egress rule. Include DNS (UDP and TCP 53 to `kube-dns`) if egress is default-deny.
- An `Ingress` policy on the target pods with no rule for the source: add the ingress rule.
- `connection refused` with ready endpoints: the container is not listening on `targetPort`; compare it with the container's port and command.

# Rules

- Budget about five commands. Do not `exec` into pods to test connectivity: the boundary is read-only.
- If a live read fails or is refused, do not retry the cluster in a loop or write a patch from workspace manifests; block the card as `needs_input` as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) ("Cannot proceed") describes, quoting the exact stderr line.
- Do not run `gcloud container clusters get-credentials`. Preflight has pinned the kubeconfig.
