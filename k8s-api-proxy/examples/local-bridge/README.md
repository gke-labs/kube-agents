# Local bridge

One `k8s-api-proxy` process per sandbox, listening on loopback in the sandbox's network namespace
and forwarding to a local endpoint that already reaches the API server, such as a port the sandbox
runtime forwards. The proxy adds its verb policy, Secret redaction and audit
records to that endpoint. Run it from a release archive or as a sidecar container; the flags are
described in the [module README](../../README.md).

```bash
k8s-api-proxy \
  --listen-addr=127.0.0.1:8443 \
  --tls-cert-file="" \
  --tls-key-file="" \
  --auth-mode=none \
  --upstream-url=http://127.0.0.1:8001 \
  --deny-streaming \
  --cache-max-bytes=0
```

`--auth-mode=none` admits every peer that can reach the listener, so the loopback address is the
access control for who reaches the proxy: it refuses to start in this mode unless `--listen-addr`
is given explicitly. The two empty TLS flags replace default certificate paths that do not exist here and
make the proxy serve plain HTTP. `--upstream-url` is the local endpoint, `8001` in this example;
bare `/api/...` paths route to it, so any `kubectl` version works. `--deny-streaming` refuses
watches, log follows and protocol upgrades, which suits a bridge that lives for one command, and
`--cache-max-bytes=0` turns off the response cache.

The upstream must not be reachable by the sandbox's processes except through the proxy. Here the
proxy and `127.0.0.1:8001` share the sandbox's network namespace, so a process that dials the
upstream port directly skips the verb policy, the Secret redaction and the audit record, and the
proxy's policy is advisory. Close that path in one of three ways: run the bridge and its upstream
in a network namespace the sandbox's processes are not in, with only the proxy's listener exposed
to them; use an `https://` upstream that demands a credential only the bridge holds, given to
the proxy with `--upstream-token-file`; or firewall the upstream port so that only the proxy's
user can connect to it.

The sandbox needs no kubeconfig file:

```bash
kubectl --server=http://127.0.0.1:8443 get pods
```

As a sidecar, the same flags go in the container's `args`:

```yaml
- name: k8s-api-proxy
  # Replace X.Y.Z with a kube-agents release.
  image: ghcr.io/gke-labs/kube-agents/kube-proxy:X.Y.Z
  args:
    - --listen-addr=127.0.0.1:8443
    - --tls-cert-file=
    - --tls-key-file=
    - --auth-mode=none
    - --upstream-url=http://127.0.0.1:8001
    - --deny-streaming
    - --cache-max-bytes=0
  securityContext:
    runAsNonRoot: true
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop:
        - ALL
```
