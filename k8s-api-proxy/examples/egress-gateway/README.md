# Shared proxy behind an egress gateway

One `k8s-api-proxy` Deployment serves the sandboxes of every tenant. A sandbox holds no
credential: its `kubectl` sends a placeholder token, the sandbox runtime's egress gateway replaces
it with a signed token it mints for that sandbox, and the proxy verifies that token, applies its
policy and forwards the request with a credential of its own. The flags are described in the
[module README](../../README.md); this page covers what the deployment around them has to get
right.

[`proxy.yaml`](proxy.yaml) holds the Kubernetes objects and is the only file to apply.
[`egress-rule.yaml`](egress-rule.yaml) lists the values to enter in your gateway's own rule, in no
product's schema. [`sandbox-kubeconfig.yaml`](sandbox-kubeconfig.yaml) is the kubeconfig a sandbox
uses.

Before applying, replace:

- the image tag;
- the `podCertificate` volume's `signerName`;
- `--jwt-issuer`, `--jwt-audiences` and `--jwt-partition-claim`, to match the tokens your gateway
  mints, adding `--jwt-jwks-url` when the issuer is not an `https://` URL that serves OpenID
  Connect metadata;
- the tenants in `--jwt-allowed-partitions` and the subject prefix in `--jwt-required-sub-prefix`,
  or delete either flag;
- the NetworkPolicy's selectors, to match your gateway's namespace and pod labels;
- the `shared-readonly` namespace and the RoleBinding in it, to grant what sandboxes may read;
- the kubeconfig's `certificate-authority` path, to where your runtime mounts its trust bundle.

A wrong `--jwt-issuer` that is still an `https://` URL does not stop the proxy from starting. The
key set is fetched when the first token arrives, so the pod goes Ready either way: a token naming
another issuer is answered `401`, and one naming an issuer whose key set or metadata cannot be
fetched is answered `503`. Send a request through the gateway before relying on the deployment.
An issuer that is not an `https://` URL, or one with a query or fragment, stops the proxy at
startup unless `--jwt-jwks-url` is set.

## HTTPS on every hop

The gateway injects a bearer token, so the rule that carries it must be `https`; expect a gateway
to refuse to inject a credential into a plaintext rule. The proxy therefore serves TLS. The example
mounts a `podCertificate` projected volume, whose `credentialBundlePath` writes the private key
and the certificate chain to one file, and names that file in both `--tls-cert-file` and
`--tls-key-file`. The proxy reloads the file when the kubelet rotates it.

That volume needs pod certificates enabled on the cluster (the `podCertificate` projected volume
source and the PodCertificateRequest API; check your cluster's version and feature gates) and a
signer that issues serving certificates for the Service's DNS name. On a cluster without either,
keep a certificate for that name in a TLS Secret, replace the `tls` volume, and point the two
flags at the Secret's keys:

```yaml
containers:
  - name: k8s-api-proxy
    args:
      - --tls-cert-file=/var/run/secrets/k8s-api-proxy/tls/tls.crt
      - --tls-key-file=/var/run/secrets/k8s-api-proxy/tls/tls.key
    volumeMounts:
      - name: tls
        mountPath: /var/run/secrets/k8s-api-proxy/tls
        readOnly: true
volumes:
  - name: tls
    secret:
      secretName: k8s-api-proxy-tls
```

## One Service name everywhere

Use `k8s-api-proxy.sandbox-proxy.svc`, the three-label `<service>.<namespace>.svc` form, in the
kubeconfig's `server:`, in the gateway rule's host, and as the token's audience. TLS verification
compares the name a client dials with the names in the certificate, so the form has to be the one
your signer issues: a certificate for `<service>.<namespace>.svc` does not verify for
`<service>.<namespace>.svc.cluster.local`. The audience is compared as a string with
`--jwt-audiences`, so it must equal what the gateway puts in `aud`.

## Who reaches the proxy

The NetworkPolicy admits pods that carry the gateway's labels in the gateway's namespace, on port
8443, and no other pod. It needs a network plugin that enforces NetworkPolicy, and host-network
pods and the nodes themselves are typically not subject to it. The token check is the boundary: a
request that arrives without a token the issuer signed is refused whatever its source.

## Tenants

`--jwt-partition-claim=tenant` reads each caller's tenant from the token and refuses a token
without it, and `--partition-by-caller=partition` keeps cached responses and coalesced in-flight
fetches within one tenant. `--jwt-allowed-partitions` and `--jwt-required-sub-prefix` narrow who
is admitted.

The partition isolates the proxy's cache only. The upstream credential is the same for every
tenant, so every admitted sandbox reads whatever the proxy's ServiceAccount can read. The example
binds that ServiceAccount to the `view` role in one namespace, `shared-readonly`. Add a
RoleBinding per namespace to grant more; a ClusterRoleBinding to `view` grants every tenant read
access to every namespace, and is the choice to make deliberately.

Capacity is shared too. `--max-in-flight` (24 by default) caps the requests one replica forwards
at once across all tenants, so one tenant's concurrent requests can get another's answered `429`.
The example does not set `--max-in-flight-per-caller` or `--max-long-running-per-caller`, which
bound what one username holds of that capacity and how many watches it opens.

## A gateway that authenticates with its own certificate

Some gateways mint no token: they authenticate the sandbox themselves, connect to the proxy with
a client certificate, and forward the sandbox's identity in request headers. For such a gateway,
delete `--auth-mode=jwt` and every `--jwt-*` flag from the container's `args` and add the flags
below; `--partition-by-caller=partition` stays, and now separates tenants by the partition header.

```yaml
args:
  - --auth-mode=requestheader
  - --requestheader-client-ca-file=/var/run/secrets/k8s-api-proxy/gateway-ca/ca.crt
  - --requestheader-name-source=uri
  - --requestheader-allowed-names=spiffe://example.test/ns/egress/sa/gateway
  - --requestheader-username-headers=X-Sandbox-User
  - --requestheader-partition-headers=X-Sandbox-Tenant
  - --requestheader-allowed-partitions=tenant-a,tenant-b
```

Mount at that path the bundle of the CA that signs the gateway's client certificate, and nothing
else: the proxy believes the headers of every request from a certificate under that CA with an
allowed name, so that certificate, not a token, is the boundary. Replace the name with the one in
your gateway's certificate (`--requestheader-name-source` picks the field) and the header names
with ones your gateway overwrites on every request. The proxy asks for the certificate in the TLS
handshake and never forwards the headers. The
[module README](../../README.md) has the rules, among them that a connection with a client
certificate is never admitted on a bearer token.

## The sandbox kubeconfig and kubectl versions

`token: placeholder` gives `kubectl` an `Authorization` header for the gateway to replace, and
`certificate-authority` is the trust bundle the sandbox runtime mounts for connections through its
gateway. The `server:` URL has no path, because `--upstream-url` routes bare `/api/...` paths to the
single upstream. That works with any `kubectl` version.

## The upstream, and routing to GKE clusters instead

The example forwards to the API server of the cluster the proxy runs in:
`--upstream-url=https://kubernetes.default.svc`, with the proxy's projected ServiceAccount token
in `--upstream-token-file` and the cluster CA in `--upstream-ca-file`. It needs no cloud
credentials and runs on any conformant cluster. Point those flags at another API server's URL,
token and CA to serve a different single cluster.

To reach several GKE clusters, delete `--upstream-url`, `--upstream-token-file` and
`--upstream-ca-file` from the container's `args` and add the two flags below. They are required:
without `--upstream-url` the proxy reads its own cluster's token and CA at startup, from the
default ServiceAccount paths unless told otherwise, and this pod mounts them in the `upstream`
volume instead.

```yaml
args:
  - --kube-token-file=/var/run/secrets/k8s-api-proxy/upstream/token
  - --kube-ca-file=/var/run/secrets/k8s-api-proxy/upstream/ca.crt
```

Then give the proxy Google credentials (Workload Identity for its ServiceAccount) that can
describe and read the clusters; it starts without them, and only a request to a `gke_` cluster
fails until they are there. Each cluster is addressed by path, so a kubeconfig entry's `server:`
becomes `https://k8s-api-proxy.sandbox-proxy.svc:8443/clusters/gke_<project>_<location>_<cluster>`,
or `.../clusters/in-cluster` for the proxy's own cluster, and a request without the
`/clusters/<id>` prefix is refused. The sandbox then needs `kubectl` 1.34 or later: earlier
versions drop the URL's path on `kubectl get --raw`
([routing](../../../docs/designs/kube-proxy.md#3-url-routing-and-multi-cluster-addressing)).
