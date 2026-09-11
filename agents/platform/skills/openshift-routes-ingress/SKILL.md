---
name: openshift-routes-ingress
metadata:
  category: Networking
description: >-
  Configures and troubleshoots OpenShift native Routes (route.openshift.io/v1),
  HAProxy edge ingress, TLS termination (edge, passthrough, re-encrypt), and
  service exposing. Use when exposing services externally on OpenShift,
  configuring HTTP-to-HTTPS redirects, setting custom hostnames, resolving
  router 503 "Application is not available" errors, or configuring OpenShift
  Service Serving Certificates. Don't use for GKE Gateway API, GKE Ingress, or
  Cloud Armor policies.
---

# OpenShift Routes & Ingress Skill

This skill provides workflows for exposing services on Red Hat OpenShift using
native OpenShift `Route` resources (`route.openshift.io/v1`), configured with
OpenShift's default HAProxy Ingress Controller (`router-default`).

On OpenShift, Routes are the standard, lightweight alternative to Kubernetes
Ingress and GKE Gateway API, providing automated DNS resolution, TLS
termination, and traffic routing.

## Critical Rules

- **READ-ONLY DIAGNOSTICS & GITOPS MUTATIONS:** The Platform Agent operates with read-only cluster visibility. Do not run direct mutating commands (`oc create route`, `oc annotate service`) unattended. Propose all Route manifests and Service annotations as declarative GitOps pull requests or present them via `submit-suggestion` for operator confirmation.
- **CLI COMPATIBILITY:** Route resources (`route.openshift.io/v1`) are standard Custom Resources. All inspection commands run identically via `oc` or `kubectl` (e.g. `kubectl get routes.route.openshift.io -n {namespace}`).
- **DEFAULT ROUTE HOSTING:** When `spec.host` is omitted, OpenShift automatically assigns `<route_name>-<namespace>.apps.<domain>`. Do not hardcode domain names unless explicitly requested.

## TLS Termination Modes

| Mode | Termination Point | Backend Communication | Typical Use Case |
| :--- | :--- | :--- | :--- |
| `edge` | HAProxy Router | Plain HTTP | Standard web apps, APIs, microservices |
| `passthrough` | Target Pod | Direct HTTPS/TLS (end-to-end) | Workloads with dedicated certificates or mTLS |
| `reencrypt` | HAProxy Router & Target Pod | HTTPS to Router, re-encrypted HTTPS to Pod | Zero-trust internal networks |

---

## Workflows

### 1. Expose a Service via Edge Route (Recommended Default)

Exposes an internal Service over HTTPS, terminating TLS at the router using the cluster's default wildcard certificate (`*.apps.<domain>`), and redirecting plain HTTP to HTTPS:

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: {route_name}
  namespace: {namespace}
spec:
  to:
    kind: Service
    name: {service_name}
    weight: 100
  port:
    targetPort: {service_port_name_or_number}
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
```

---

### 2. Configure Service Serving Certificates (Internal TLS)

OpenShift provides an internal Service CA that automatically generates certificates for cluster services:

1. Propose annotating the Service in the GitOps manifest:
   ```yaml
   metadata:
     annotations:
       service.beta.openshift.io/serving-cert-secret-name: {secret_name}
   ```
2. Mount `{secret_name}` into the application pod for TLS listening (contains `tls.crt` and `tls.key`).
3. Create a `reencrypt` Route so HAProxy verifies the internal cert:
   ```yaml
   apiVersion: route.openshift.io/v1
   kind: Route
   metadata:
     name: {route_name}
     namespace: {namespace}
   spec:
     to:
       kind: Service
       name: {service_name}
     port:
       targetPort: https
     tls:
       termination: reencrypt
       insecureEdgeTerminationPolicy: Redirect
   ```

---

### 3. Expose via Passthrough Route (mTLS / End-to-End Encryption)

Sends encrypted traffic directly to the backend pod without HAProxy decrypting:

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: {route_name}
  namespace: {namespace}
spec:
  to:
    kind: Service
    name: {service_name}
  port:
    targetPort: {https_port}
  tls:
    termination: passthrough
```

---

### 4. Troubleshoot Route Issues (503 "Application is not available")

When curl or a browser returns:
`503 Service Unavailable: Application is not available`

**Step 1: Check Route Host & DNS Resolution**
```bash
# Retrieve the assigned canonical hostname
HOST=$(kubectl get routes.route.openshift.io {route_name} -n {namespace} -o jsonpath='{.spec.host}' 2>/dev/null || oc get route {route_name} -n {namespace} -o jsonpath='{.spec.host}')
echo "Route Host: $HOST"

# Verify DNS resolution matches the ingress router IP
dig +short "$HOST"
```

**Step 2: Inspect Service Endpoints**
HAProxy returns 503 if the backend Service has 0 healthy endpoints:
```bash
kubectl get endpoints {service_name} -n {namespace}
```
If endpoints list is `<none>`, inspect backend pod readiness probes and labels:
```bash
kubectl get pods -n {namespace} -l {selector}
```

**Step 3: Check Ingress Controller Status**
```bash
kubectl get ingresscontroller.operator.openshift.io default -n openshift-ingress-operator
kubectl get pods -n openshift-ingress
```
