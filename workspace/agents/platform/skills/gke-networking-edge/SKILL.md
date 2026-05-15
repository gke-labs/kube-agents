---
name: gke-networking-edge
description: Workflows for configuring edge networking, ingress, and security on GKE.
---

# GKE Networking Edge Skill

This skill provides workflows for exposing applications running on GKE securely to the internet or internal networks.

## Workflows

### 1. Configure Gateway API (Recommended)

The Gateway API is the modern way to manage routing in Kubernetes.

**Prerequisites**: Gateway API must be enabled on the cluster (enabled by default in GKE 1.24+).

**Example Gateway Manifest:**

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: my-gateway
  namespace: my-namespace
spec:
  gatewayClassName: gke-l7-global-external-managed # GKE managed external L7 load balancer
  listeners:
    - name: http
      protocol: HTTP
      port: 80
```

**Example HTTPRoute Manifest:**

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-route
  namespace: my-namespace
spec:
  parentRefs:
    - name: my-gateway
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: my-service
          port: 80
```

### 2. Configure Standard GKE Ingress

Use standard Ingress for simpler use cases or legacy setups.

**Example Ingress Manifest:**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-ingress
  namespace: my-namespace
  annotations:
    kubernetes.io/ingress.class: "gce"
spec:
  rules:
    - http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: my-service
                port:
                  number: 80
```

### 3. Secure with Cloud Armor

Cloud Armor provides WAF and DDoS protection.

**Enable Cloud Armor via BackendConfig:**

1. Create a Security Policy in Cloud Armor (usually via gcloud or Terraform).
2. Reference it in a `BackendConfig` in GKE.

**Example BackendConfig:**

```yaml
apiVersion: cloud.google.com/v1
kind: BackendConfig
metadata:
  name: my-backend-config
  namespace: my-namespace
spec:
  securityPolicy:
    name: my-cloud-armor-policy
```

3. Associate `BackendConfig` with your `Service` via annotations.

### 4. Configure Google-Managed SSL Certificates

Automatically provision and renew SSL certificates.

**Example ManagedCertificate (Legacy Ingress):**

```yaml
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: my-certificate
spec:
  domains:
    - example.com
```

Reference it in Ingress annotations: `networking.gke.io/managed-certificates: my-certificate`.

**Gateway API Approach:**
Use the `gateway.networking.k8s.io` API with certificate management integration.

### 5. Enable Container-Native Load Balancing (Recommended)

Container-native load balancing allows load balancers to target Kubernetes Pods directly, rather than targeting nodes. This improves latency and distribution.

**Prerequisites**: Cluster must be VPC-native.

**How it works**:

- For GKE Ingress and Gateway API, container-native load balancing is enabled by default via Network Endpoint Groups (NEGs).
- To verify or explicitly enable it for a Service, use the `cloud.google.com/neg` annotation.

**Example Service Manifest:**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
  annotations:
    cloud.google.com/neg: '{"ingress": true}' # Enabled for Ingress
spec:
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8080
  selector:
    app: my-app
  type: ClusterIP
```

### 6. Configure Private Service Connect (PSC)

Private Service Connect allows you to expose services in one VPC to consumers in another VPC securely, without VPC peering.

**Steps:**

1. Create an internal load balancer for your service.
2. Create a `ServiceAttachment` referencing the load balancer.

**Example ServiceAttachment Manifest:**

```yaml
apiVersion: networking.gke.io/v1
kind: ServiceAttachment
metadata:
  name: my-psc-attachment
  namespace: my-namespace
spec:
  connectionPreference: ACCEPT_AUTOMATIC
  natSubnets:
    - my-psc-nat-subnet # Subnet dedicated for PSC NAT
  targetService:
    name: my-service
    namespace: my-namespace
```

Share the `ServiceAttachment` URI with consumers to create a PSC endpoint in their VPC.

## Best Practices

1. **Prefer Gateway API**: It offers more flexibility and role separation than Ingress.
2. **Enable Cloud Armor**: Always protect public-facing endpoints with Cloud Armor.
3. **Use Managed Certificates**: Avoid managing certificate renewals manually.
4. **Use Container-Native Load Balancing**: Always use NEGs for HTTP(S) load balancing to reduce latency and improve traffic distribution.
