---
name: gke-ingress-networking
description: Inspect and audit GKE networking, services, Ingress resources, load balancers, and firewall rules. Use when verifying external IPs, target ports, Ingress host/path routing, load balancer health status, or GKE network policy/topology settings.
---

# gke-ingress-networking - GKE Ingress & Network Routing Audit

This skill allows the agent to inspect, audit, and analyze GKE services, Ingress configurations, Google Cloud Load Balancer (GCLB) status, and cluster firewall rules.

## When to Use

- **Service IP & Port Mapping**: Triggered when a user needs to verify the external IP address or target ports of a GKE Service.
- **Ingress Configuration Auditing**: Triggered when checking Ingress path routings, hostnames, SSL/TLS certificates, or listing Ingress resources.
- **Load Balancer Health Check**: Triggered when looking for load balancer health checks, metrics, or forwarding rules created by GKE.
- **Firewall & Topology Diagnostics**: Triggered when diagnosing if firewall rules or network policies are blocking pod-to-pod or internet-to-pod communication.

## Execution Instructions

Perform the following steps to audit GKE networking configuration:

### Step 1: Query Service External IP & Target Ports

1. **Get Service External IP**:
   ```bash
   kubectl get service <SERVICE_NAME> -n <NAMESPACE> -o jsonpath='{.status.loadBalancer.ingress[*].ip}'
   ```
   *If the field is empty, the service is either not of type `LoadBalancer` or the load balancer is still provisioning.*

2. **Retrieve target ports and port mapping configurations**:
   ```bash
   kubectl get service <SERVICE_NAME> -n <NAMESPACE> -o json | jq '.spec.ports[] | {port: .port, targetPort: .targetPort, protocol: .protocol}'
   ```

---

### Step 2: List Ingress Resources & Path Routing

1. **List all Ingress resources in the cluster**:
   ```bash
   kubectl get ingress -A -o wide
   ```

2. **Inspect hostnames and path routing mappings for a specific Ingress**:
   ```bash
   kubectl get ingress <INGRESS_NAME> -n <NAMESPACE> -o json | jq '.spec.rules[] | {host: .host, paths: [.http.paths[] | {path: .path, backend: .backend}]}'
   ```

---

### Step 3: Audit GCP Load Balancer Status

GKE Service controller creates GCP forwarding rules, target proxies, and backend services. To analyze GKE network-created load balancers:

1. **Find GCP load balancer tags / names from annotations**:
   ```bash
   kubectl get service <SERVICE_NAME> -n <NAMESPACE> -o json | jq '.metadata.annotations'
   ```
   *Note: GKE ingress annotations like `ingress.kubernetes.io/forwarding-rule` contain the exact GCP resource names.*

2. **List GCP Forwarding Rules matching the Service IP**:
   ```bash
   IP_ADDRESS=$(kubectl get service <SERVICE_NAME> -n <NAMESPACE> -o jsonpath='{.status.loadBalancer.ingress[*].ip}')
   if [ ! -z "$IP_ADDRESS" ]; then
     gcloud compute forwarding-rules list --filter="IPAddress=$IP_ADDRESS"
   else
     echo "No external IP associated with the service."
   fi
   ```

---

### Step 4: GKE Cluster Firewall & Open Ports Audit

GKE automatically creates VPC firewall rules to allow node-to-node communication, master-to-node webhooks, and health checks.

1. **Find cluster network tag**:
   Get the VPC network name and tags associated with the cluster:
   ```bash
   gcloud container clusters describe <CLUSTER_NAME> --zone=<ZONE> --format="value(network, networkConfig.subnetwork)"
   ```

2. **List all GKE-managed firewall rules**:
   ```bash
   gcloud compute firewall-rules list --filter="targetTags:gke-<CLUSTER_NAME>-*" --format="table(name, direction, priority, sourceRanges.list(), allowed[].list())"
   ```

3. **Verify specific port accessibility**:
   Check if a firewall rule exists that opens a target port (e.g. `80`, `443`, `8443`):
   ```bash
   gcloud compute firewall-rules list --filter="targetTags:gke-<CLUSTER_NAME>-* AND allowed.ports:<PORT_NUMBER>"
   ```

---

### Step 5: Pod-to-Pod Communication & Network Topology

1. **Check if Network Policies are enabled**:
   ```bash
   gcloud container clusters describe <CLUSTER_NAME> --zone=<ZONE> --format="value(addonsConfig.networkPolicyConfig)"
   ```

2. **List all active Network Policies**:
   ```bash
   kubectl get networkpolicies -A
   ```

3. **Check for Pod Security / Calico configuration**:
   Verify if the `NetworkPolicy` addon is active at the node level:
   ```bash
   kubectl get pods -n kube-system -l 'k8s-app in (calico-node, anetd)'
   ```
