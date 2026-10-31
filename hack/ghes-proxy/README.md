# GHES proxy

The GHES proxy is an eval fixture. It is an HTTPS forwarding proxy that has the shape of a GitHub Enterprise Server (GHES). It forwards each request to github.com, and it gives back the answers with the URLs that a GHES at its own hostname gives. It is not a product component, and an install never uses it.

## Why it exists

kube-agents can declare its GitHub forge at a GHES host: the forge support is on main since gke-labs/kube-agents#3032, and the installer support is #3104. The eval lanes must test that path. A build without that support refuses a GitHub forge at a host other than github.com. No open-source GHES exists, and a real GHES instance costs too much to keep up to date. The proxy lets an eval install use a GHES host name, a private CA and the GHES URL layout, and it keeps the data on github.com, in the eval repositories that the lanes already use and reset.

The proxy does not copy what GHES does. It copies only what kube-agents reads: the API paths, the host in the URLs that the agent shows, and the version header. A test that passes through the proxy shows that kube-agents uses the GHES host and the GHES paths. It does not show that kube-agents works with each GHES release.

## What it does

The proxy listens on HTTPS with a certificate from a private CA. It forwards these paths, and it refuses all other paths with `404`:

| Path on the proxy                                                                           | Upstream                              |
| ------------------------------------------------------------------------------------------- | ------------------------------------- |
| `/api/v3/<path>`                                                                            | `https://api.github.com/<path>`       |
| `/api/graphql`                                                                              | `https://api.github.com/graphql`      |
| `GET /<owner>/<repo>.git/info/refs?service=git-upload-pack` and `?service=git-receive-pack` | the same path on `https://github.com` |
| `POST /<owner>/<repo>.git/git-upload-pack`                                                  | the same path on `https://github.com` |
| `POST /<owner>/<repo>.git/git-receive-pack`                                                 | the same path on `https://github.com` |

The query string goes to the upstream unchanged. The proxy streams request and response bodies, so a large git pack is not held in memory.

The proxy sends the `Authorization` header to the upstream unchanged: a Bearer token for the REST and GraphQL APIs, the token minter's App JWT, and Basic credentials for git. The proxy has no credential of its own.

The proxy changes only these parts of a response:

- In a JSON response from the API, the string value of each `html_url` and `access_tokens_url` field, at any depth. `https://github.com/` becomes `https://<host>/`, `https://api.github.com/graphql` becomes `https://<host>/api/graphql`, and all other `https://api.github.com/` URLs become `https://<host>/api/v3/`. The agent shows `html_url` as the link to what it wrote. The token minter sends its token request to `access_tokens_url`.
- The `Location` header, with the same two host changes.
- The URLs in the `Link` header, with the same two host changes.
- It adds the header `X-GitHub-Enterprise-Version` (default `3.17.0`).

All other bytes stay as GitHub sent them. The proxy does not change issue, comment or pull request bodies, or diff text, because the model reads that text. Other URL fields, for example `url` and `clone_url`, keep the github.com hosts. A JSON body larger than `-max-rewrite-bytes` goes through unchanged, and so does a JSON body that the upstream sends compressed in a form that the proxy cannot decode.

The proxy does not follow redirects. It gives a redirect back to the client, with the `Location` header changed as above.

The proxy logs one line for each request: the method, the route, the path without the query string, the status, the byte count and the time. It never logs a header value, a query string or a body, because each of them can hold a token.

## Configuration

Each flag reads its default from an environment variable.

| Flag                  | Environment variable            | Default                       |
| --------------------- | ------------------------------- | ----------------------------- |
| `-listen`             | `GHES_PROXY_LISTEN`             | `:8443`                       |
| `-cert-file`          | `GHES_PROXY_CERT_FILE`          | `/etc/ghes-proxy/tls/tls.crt` |
| `-key-file`           | `GHES_PROXY_KEY_FILE`           | `/etc/ghes-proxy/tls/tls.key` |
| `-public-host`        | `GHES_PROXY_PUBLIC_HOST`        | none, required                |
| `-upstream-api-host`  | `GHES_PROXY_UPSTREAM_API_HOST`  | `api.github.com`              |
| `-upstream-web-host`  | `GHES_PROXY_UPSTREAM_WEB_HOST`  | `github.com`                  |
| `-enterprise-version` | `GHES_PROXY_ENTERPRISE_VERSION` | `3.17.0`                      |
| `-max-rewrite-bytes`  | `GHES_PROXY_MAX_REWRITE_BYTES`  | `67108864` (64 MiB)           |

`-public-host` is the name that the clients use, `host` or `host:port`. The server certificate must name that host.

## Files

| File                        | What it is                                                  |
| --------------------------- | ----------------------------------------------------------- |
| `main.go`, `proxy.go`       | The proxy. It uses only the Go standard library.            |
| `proxy_test.go`             | The tests: `go test ./...` in this directory.               |
| `make-certs.sh`             | Makes a private CA and a server certificate, at run time.   |
| `Dockerfile`                | The image.                                                  |
| `manifests/*.yaml.template` | The Secret, the Deployment and the Service, for `envsubst`. |

## Make the certificates

```bash
hack/ghes-proxy/make-certs.sh ghes.eval.internal ~/dev/ghes-proxy-certs
```

The script writes `ca.crt`, `tls.crt` and `tls.key` to the directory. It deletes the CA private key after it signs the server certificate, so nothing can sign a second certificate with that CA. It refuses a directory in this repository, so that you cannot commit a key. The certificates expire after 30 days; give a third argument to change this.

## Run it on a workstation

```bash
cd hack/ghes-proxy
go run . -listen 127.0.0.1:8443 -public-host ghes.eval.internal:8443 \
  -cert-file ~/dev/ghes-proxy-certs/tls.crt -key-file ~/dev/ghes-proxy-certs/tls.key
```

Then, in a second shell:

```bash
curl --cacert ~/dev/ghes-proxy-certs/ca.crt --resolve ghes.eval.internal:8443:127.0.0.1 \
  https://ghes.eval.internal:8443/api/v3/repos/gke-labs/kube-agents
git -c http.sslCAInfo=$HOME/dev/ghes-proxy-certs/ca.crt \
  -c http.curloptResolve=ghes.eval.internal:8443:127.0.0.1 \
  ls-remote https://ghes.eval.internal:8443/gke-labs/kube-agents.git HEAD
```

The `html_url` in the first answer starts with `https://ghes.eval.internal:8443/`. The bench cannot grade links with a port (`BENCH_GITHUB_HOST` is a bare hostname), so an eval run uses the proxy on port 443, as in the cluster steps below.

## Run it in a cluster with a private DNS name

These steps are for GKE and Cloud DNS. They give the proxy an internal address in the VPC and a private DNS name. The clients are the platform agent's credential broker and the token minter. They must resolve the name, trust the CA and reach the proxy on TCP 443.

1. Make the certificates for the name, as above. In these steps the name is `ghes.eval.internal`.

2. Build the image and push it to a registry that the cluster can pull from:

   ```bash
   docker build --platform linux/amd64 -t "$REGISTRY/ghes-proxy:$TAG" hack/ghes-proxy
   docker push "$REGISTRY/ghes-proxy:$TAG"
   ```

3. Apply the Secret, the Deployment and the Service. Do not keep a rendered copy of the Secret:

   ```bash
   export NAMESPACE=ghes-proxy GHES_PUBLIC_HOST=ghes.eval.internal
   export GHES_PROXY_IMAGE="$REGISTRY/ghes-proxy:$TAG"
   kubectl create namespace "$NAMESPACE"
   TLS_CRT_B64=$(base64 -w0 < ~/dev/ghes-proxy-certs/tls.crt) \
   TLS_KEY_B64=$(base64 -w0 < ~/dev/ghes-proxy-certs/tls.key) \
     envsubst < hack/ghes-proxy/manifests/secret.yaml.template | kubectl apply -f -
   envsubst < hack/ghes-proxy/manifests/deployment.yaml.template | kubectl apply -f -
   envsubst < hack/ghes-proxy/manifests/service.yaml.template | kubectl apply -f -
   ```

   On macOS, use `base64 < file | tr -d '\n'` in place of `base64 -w0 < file`.

4. Get the internal address of the Service. The load balancer can take some minutes:

   ```bash
   kubectl -n "$NAMESPACE" get service ghes-proxy -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
   ```

5. Make a private Cloud DNS zone for the name on the cluster's VPC network, and an `A` record to that address:

   ```bash
   gcloud dns managed-zones create ghes-proxy --dns-name=ghes.eval.internal. \
     --visibility=private --networks="$VPC_NETWORK" --description="GHES proxy for evals"
   gcloud dns record-sets create ghes.eval.internal. --zone=ghes-proxy \
     --type=A --ttl=60 --rrdatas="$PROXY_IP"
   ```

   Pods on GKE resolve a name outside the cluster through the metadata server, which reads the private zones of the VPC. See [Cloud DNS private zones](https://cloud.google.com/dns/docs/zones#create-private-zone).

6. Check from a pod in the cluster:

   ```bash
   kubectl run ghes-check --rm -i --restart=Never --image=curlimages/curl -- \
     curl -sS -o /dev/null -w '%{http_code}\n' --insecure https://ghes.eval.internal/api/v3/meta
   ```

   The command prints `200`. The check uses `--insecure` because the pod does not have the CA. The clients in the next step get it.

7. Declare the forge on the install at `ghes.eval.internal`, with the CA in `caBundleRef`, as the "GitHub Enterprise Server" section of `INSTALL.md` tells. This needs a build that includes #3032; a build without it refuses the host and `caBundleRef`. `install.sh` writes this forge with `--gitops-host` and `--gitops-ca-file`, or use the Terraform and Helm steps in that section. Use `ca.crt` from step 1. The GitHub App is the github.com App that the eval repositories already use, because the data is on github.com. The repository entry is `https://ghes.eval.internal/<owner>/<repo>`.

8. If the cluster enforces NetworkPolicy, make sure that the credential broker and the token minter can reach the proxy, and that the proxy can reach github.com on TCP 443. The proxy runs in the same cluster as its clients, so a policy does not see the address and port that the client dials. On GKE Dataplane V2, traffic to a Service address, a ClusterIP or the internal load balancer of step 4, goes to the proxy pod. The policy sees a pod in the cluster on the container port `8443`, not an outside address on `443`. Thus the shipped minter policy, which allows TCP 443 to any address for a real instance outside the cluster, does not allow the proxy. Add an egress rule for each client whose policy limits egress (the minter's does; the broker's shipped policy limits ingress only):

   ```bash
   kubectl apply -f - <<'NP'
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: github-token-minter-to-ghes-proxy
     namespace: kubeagents-system
   spec:
     podSelector:
       matchLabels:
         app: github-token-minter
     policyTypes: [Egress]
     egress:
       - to:
           - namespaceSelector:
               matchLabels:
                 kubernetes.io/metadata.name: ghes-proxy
             podSelector:
               matchLabels:
                 app.kubernetes.io/name: ghes-proxy
         ports:
           - {protocol: TCP, port: 8443}
   NP
   ```

   Use the minter's pod labels on your install (`kubectl -n kubeagents-system get deploy github-token-minter -o jsonpath='{.spec.template.metadata.labels}'`). For a broker policy that limits egress, add the same rule with the broker's pod labels.

## Run it in the cluster with an in-cluster DNS name

These steps give the proxy a DNS name inside one cluster, with no Cloud DNS zone and no load balancer. Use them when the cluster is on a VPC that other people share: a private zone is visible on the whole VPC, and these objects go with the cluster. They are for a GKE cluster that uses kube-dns (with or without NodeLocal DNSCache). A cluster that uses Cloud DNS for GKE does not read the kube-dns ConfigMap; use the Cloud DNS steps above for it.

1. Do steps 1 and 2 of the section above, and step 3 without its last command, which makes the load balancer. Then apply the Service as a ClusterIP Service:

   ```bash
   envsubst < hack/ghes-proxy/manifests/service.yaml.template \
     | sed -e '/load-balancer-type/d' -e '/^  annotations:/d' -e 's/type: LoadBalancer/type: ClusterIP/' \
     | kubectl apply -f -
   PROXY_IP=$(kubectl -n "$NAMESPACE" get service ghes-proxy -o jsonpath='{.spec.clusterIP}')
   ```

2. Run a small CoreDNS server that answers the name with that address:

   ```bash
   kubectl apply -f - <<EOF
   apiVersion: v1
   kind: ConfigMap
   metadata: {name: ghes-dns, namespace: ${NAMESPACE}}
   data:
     Corefile: |
       ${GHES_PUBLIC_HOST}:53 {
         hosts {
           ${PROXY_IP} ${GHES_PUBLIC_HOST}
           fallthrough
         }
         errors
       }
   ---
   apiVersion: apps/v1
   kind: Deployment
   metadata: {name: ghes-dns, namespace: ${NAMESPACE}}
   spec:
     replicas: 1
     selector: {matchLabels: {app: ghes-dns}}
     template:
       metadata: {labels: {app: ghes-dns}}
       spec:
         automountServiceAccountToken: false
         containers:
           - name: coredns
             image: registry.k8s.io/coredns/coredns:v1.11.3
             args: ["-conf", "/etc/coredns/Corefile"]
             ports:
               - {name: dns, containerPort: 53, protocol: UDP}
               - {name: dns-tcp, containerPort: 53, protocol: TCP}
             resources:
               requests: {cpu: 20m, memory: 32Mi}
               limits: {memory: 128Mi}
             volumeMounts:
               - {name: cfg, mountPath: /etc/coredns}
         volumes:
           - name: cfg
             configMap: {name: ghes-dns}
   ---
   apiVersion: v1
   kind: Service
   metadata: {name: ghes-dns, namespace: ${NAMESPACE}}
   spec:
     selector: {app: ghes-dns}
     ports:
       - {name: dns, port: 53, protocol: UDP, targetPort: 53}
       - {name: dns-tcp, port: 53, protocol: TCP, targetPort: 53}
   EOF
   ```

3. Send the name's zone to that server with a kube-dns stub domain. `stubDomains` is one JSON string, and other zones can already be in it. The commands below read the value, add the entry to it with `jq`, and write the result back, so they keep all the entries that are there. If the cluster has no `kube-dns` ConfigMap in `kube-system`, they make it. They need `jq`.

   ```bash
   DNS_IP=$(kubectl -n "$NAMESPACE" get service ghes-dns -o jsonpath='{.spec.clusterIP}')
   if kubectl -n kube-system get configmap kube-dns >/dev/null 2>&1; then
     CURRENT=$(kubectl -n kube-system get configmap kube-dns -o jsonpath='{.data.stubDomains}')
     echo "stubDomains before: ${CURRENT:-<none>}"
     if MERGED=$(jq -nc --arg cur "$CURRENT" --arg host "$GHES_PUBLIC_HOST" --arg ip "$DNS_IP" \
       '($cur | if . == "" then {} else fromjson end) + {($host): [$ip]}'); then
       kubectl -n kube-system patch configmap kube-dns --type merge \
         -p "$(jq -nc --arg v "$MERGED" '{data: {stubDomains: $v}}')"
     fi
   else
     kubectl -n kube-system create configmap kube-dns \
       --from-literal=stubDomains="$(jq -nc --arg host "$GHES_PUBLIC_HOST" --arg ip "$DNS_IP" '{($host): [$ip]}')"
   fi
   kubectl -n kube-system get configmap kube-dns -o jsonpath='{.data.stubDomains}'; echo
   ```

   If the value that is there is not valid JSON, `jq` fails and the commands change nothing. The last command prints the new value: it has the entries from before and the `${GHES_PUBLIC_HOST}` entry. kube-dns and NodeLocal DNSCache read the change in about 30 seconds. See [Setting up custom resolvers for kube-dns](https://cloud.google.com/kubernetes-engine/docs/how-to/kube-dns#stub-domains).

4. Check from a pod, with the CA, as `INSTALL.md` tells ("The check" in "GitHub Enterprise Server"):

   ```bash
   kubectl run ghes-dns-check -n default --rm -i --restart=Never --image=busybox:1.36 -- nslookup ghes.eval.internal
   kubectl run ghes-https-check -n default --rm -i --restart=Never --image=curlimages/curl:8.10.1 \
     --command -- sh -c 'cat > /tmp/ca.crt && curl -sS --cacert /tmp/ca.crt -o /dev/null -w "%{http_code}\n" https://ghes.eval.internal/api/v3/meta' \
     < ~/dev/ghes-proxy-certs/ca.crt
   ```

   The first command prints the ClusterIP of the proxy, and the second prints `200`.

5. Do steps 7 and 8 of the section above. Step 8 applies here too: the policy sees the proxy pod on `8443`.

## Grade a run through the proxy

The bench reads the data from api.github.com, where it is. Set `BENCH_FORGE=github` and `BENCH_GITHUB_HOST=ghes.eval.internal`, so that the forge checks accept only `https://ghes.eval.internal/...` links in the agent's reply. To grade through the proxy as well, also set `BENCH_GITHUB_API_ROOT=https://ghes.eval.internal/api/v3` and `BENCH_FORGE_CA_FILE` to the path of `ca.crt`. See `bench/CUSTOM-TASKS.md`, "Grading on a GitHub Enterprise Server host", and the case `bench/tasks/ghes-pdb-remediation-pr/`.

## Clean up

For the Cloud DNS steps:

```bash
gcloud dns record-sets delete ghes.eval.internal. --zone=ghes-proxy --type=A
gcloud dns managed-zones delete ghes-proxy
kubectl delete namespace ghes-proxy
kubectl -n kubeagents-system delete networkpolicy github-token-minter-to-ghes-proxy
rm -rf ~/dev/ghes-proxy-certs
```

For the in-cluster DNS steps, delete the namespace, the NetworkPolicy and the certificates as above. Then remove the `ghes.eval.internal` entry from `stubDomains` in the `kube-dns` ConfigMap in `kube-system`, and keep the other entries. If it was the only entry, remove the `stubDomains` key:

```bash
CURRENT=$(kubectl -n kube-system get configmap kube-dns -o jsonpath='{.data.stubDomains}')
if REST=$(jq -nc --arg cur "$CURRENT" --arg host ghes.eval.internal \
  '($cur | if . == "" then {} else fromjson end) | del(.[$host])'); then
  if [ "$REST" != "{}" ]; then
    kubectl -n kube-system patch configmap kube-dns --type merge \
      -p "$(jq -nc --arg v "$REST" '{data: {stubDomains: $v}}')"
  elif [ -n "$CURRENT" ]; then
    kubectl -n kube-system patch configmap kube-dns --type json \
      -p '[{"op": "remove", "path": "/data/stubDomains"}]'
  fi
fi
```

If step 3 made the `kube-dns` ConfigMap, and it now has no other data, you can delete it.
