---
name: gke-gateway-api-diagnostics
description: Workflows for diagnosing GKE Gateway API routes, HTTPRoute statuses, TLS certificate expirations, and Cloud Armor security policies.
---

# GKE Gateway API Diagnostics

This skill provides diagnostic workflows for troubleshooting GKE Gateway API resources (`Gateway`, `HTTPRoute`, `GCPBackendPolicy`), TLS certificate issues, and Cloud Armor integration.

## Workflows

### 1. Audit Gateway & HTTPRoute Status

Check the operational status of all Gateways and HTTPRoutes across namespaces to identify routing errors or unresolved parent references.

**Command:**

```bash
kubectl get gateway,httproute -A
```

### 2. Diagnose Backend Policies & Health Checks

Inspect `GCPBackendPolicy` and `HealthCheckPolicy` custom resources for failing GKE ingress backends.

**Command:**

```bash
kubectl get gcpbackendpolicy,healthcheckpolicy -A
```

### 3. Verify Managed TLS Certificate Status

Check ManagedCertificate status and expiration dates.

**Command:**

```bash
kubectl get managedcertificate -A
```
