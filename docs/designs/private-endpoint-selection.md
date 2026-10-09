# Reaching a fleet cluster over its private endpoint

The Platform Agent onboards a second cluster with `gcloud container clusters get-credentials`.
`agents/platform/scripts/gke_endpoint.py` decides which flags that command gets. Until this
design it knew two answers: `--dns-endpoint` when the cluster publishes a DNS endpoint that
accepts external traffic, and nothing otherwise, which leaves gcloud writing the cluster's
public IP whenever one exists.

An enterprise estate breaks that second answer. Clusters there run private nodes with Master
Authorized Networks restricted to corporate ranges, and the DNS endpoint closed to external
traffic. The agent pod reaches such a cluster's public IP through Cloud NAT, and the NAT
address is not on the list, so every `kubectl` times out. The same cluster's private endpoint
is one hop away on the VPC the agent pod already sits in, and `get-credentials --internal-ip`
would have written it. Nothing recorded which endpoint was chosen, so the operator saw a
timeout and had to work out the rest.

## The decision rule

`gke_endpoint.endpoint_decision()` reads the target cluster once and picks, in order:

1. **DNS endpoint**, `--dns-endpoint`, when `dnsEndpointConfig.endpoint` is set and
   `allowExternalTraffic` is `true`. Unchanged. It is first because the DNS endpoint ignores
   Master Authorized Networks and routes from anywhere.
2. **DNS endpoint without a flag** when `ipEndpointsConfig` is present, even empty, and
   `enabled` is not `true`. gcloud's own test is `not enabled`, so it writes the DNS host by itself in that shape
   and refuses `--internal-ip` (`IPEndpointsIsDisabledError`); authorized networks do not gate
   that host.
3. **Private endpoint**, `--internal-ip`, when the cluster publishes
   `privateClusterConfig.privateEndpoint` (gcloud's `MissingPrivateEndpointError` otherwise;
   the nested `ipEndpointsConfig.privateEndpoint` is deliberately not read, because gcloud
   reads only the first), its public endpoint is either absent
   (`ipEndpointsConfig.enablePublicEndpoint: false`) or gated by an enabled authorized-network
   list (a public endpoint nothing restricts works
   today, and moving it could only break it), and the private endpoint is both routable from
   the agent pod and willing to admit it:
   - **routable**: the target's `networkConfig.network` equals the agent's own cluster's
     network, and either the target is in the agent cluster's region or control-plane global
     access is on (`privateClusterConfig.masterGlobalAccessConfig.enabled` or
     `ipEndpointsConfig.globalAccess`). GKE answers a private
     endpoint only from its own region unless control-plane global access is on, and every
     VPC-native cluster reports a private endpoint, so without the region test an ordinary
     public cluster in another region would lose a working endpoint for one it cannot reach.
   - **admitted**: the authorized-network list is not enabled, or it explicitly does not gate
     the private endpoint (`privateEndpointEnforcementEnabled: false`; a cluster that omits the
     field is read as enforcing, because new clusters report `true` unasked and the server-side
     default for the rest is not documented), or the two clusters share a subnetwork, or the
     agent cluster's Pod range
     (`clusterIpv4Cidr`) lies inside a listed block. The shared-subnet clause rests on an
     observation, not a document: a cluster on the agent's subnet with enforcement on and a
     list that excluded every agent range still answered the agent pod on its private
     endpoint. The Pod range is what the traffic carries: GKE does not masquerade RFC 1918
     destinations. Without the admission test an estate that had listed the agent's NAT
     address would have lost a working public endpoint on upgrade.
4. **gcloud's default**, no flag. Unchanged. When rule 3 found the private endpoint routable but
   not admitted, the remedy names the Pod range to add.

Rule 3 reads the configuration up front, which keeps the module's existing contract: the flag
is never passed blind and never probed by attempting it.

A cluster on a peered VPC is not detected: the network paths differ and nothing short of a
routes query says whether the peering exports the control-plane route.

## How the agent knows its own cluster

The operator sets `GKE_PROJECT_ID`, `GKE_LOCATION` and `GKE_CLUSTER_NAME` on the agent
container, the shell sandbox forwards them, the credential proxy carries them too, and the
platform MCP server's env block in `agents/platform/config.yaml` names them, since Hermes hands
a stdio server only the keys named there (a contract test holds the block to
`OWN_CLUSTER_ENV`).
`gke_endpoint.own_cluster()` describes that cluster once with
`--format=value(networkConfig.network,networkConfig.subnetwork,clusterIpv4Cidr)`, derives its
region from `GKE_LOCATION` (the first two segments, which is the region of every GKE zone), and
keeps the answer for the life of the process. A value that is still the literal `${...}`
placeholder Hermes hands an MCP server for an unset variable counts as unset. The install's VPC
cannot change under a running pod, so this memo has no TTL, unlike the per-target decision,
which keeps its 60-second one. A describe that fails, or answers anything other than exactly
three fields with a non-empty network, is not cached, for the reason the module already gives
for the target describe: the credential proxy is a daemon.

With any of the three variables unset the answer is "unknown", and rule 3 never fires. That is
the position of every workstation and test caller, and it is what keeps the predicate copies
in agreement without changing them. A decision that needed the own cluster and did not get it
because the describe gave no usable answer (it failed, raised, or returned a short or empty
row) while the identity was there is returned marked `provisional`. It is cached for the usual
window like any other decision, mark included, and a failed own-cluster describe is itself not
retried for a minute, so an own cluster that cannot be described costs the agent's callers one
gcloud start a minute rather than two per call. The profile scaffold is the exception: it asks
once and writes a permanent record, so it asks past that backoff and past a cached provisional
answer (`retry_own`), and one transient failure does not blank the record of every cluster
scaffolded in the minute after it. An identity that is absent is a settled answer.
The credential proxy reads the mark: a managed kubeconfig written from a provisional decision, or from no
decision at all because the target describe failed (not because the module is missing, which
is as settled as a gcloud without the flag), by the proxy's own fetch or by a caller's fetch
the proxy tried to decide for, is served for one minute and then treated as a miss (a
`.provisional` marker beside the file, written before the file under a lock of its own so the
filing step never waits behind a cold read's gcloud runs; a later settled fetch, or a caller's
fetch run as given because it carried its own endpoint flag or named no full target, clears
it). When the refetch after the window fails and a file is on disk, the proxy serves that file
and, if the file is still marked, pushes the window out rather than refusing the request; a
settled file a caller filed meanwhile is served unmarked, and a provisional fetch that succeeds
never replaces such a file: each writer notes the managed file's modification time before it
decides and fetches, and drops a provisional result only when an unmarked file newer than that
note is on disk, which is what a settled answer landing during the fetch looks like. An
unmarked file that was simply already there is replaced and marked, so a re-run of the
onboarding still refreshes a stale kubeconfig. The describes behind the splice run under a
bound of their own, tighter than a kubectl's, because they sit inside the caller's bound on the
whole fetch. A cold fetch during a failed
describe therefore does not pin a public-IP kubeconfig until the pod restarts, and a describe
that keeps failing costs one refetch a minute rather than one a request.

A Shared VPC matches naturally: a service-project cluster reports the host project's network
resource (`projects/<host>/global/networks/<name>`) in `networkConfig.network`.

## What the decision carries

`endpoint_decision()` returns an `EndpointDecision`, or `None` when nothing could be decided
now (an incomplete identity, a describe that failed with nothing cached, a help probe that
could not run or did not answer). A gcloud that answered the probe and lacks `--dns-endpoint`
is a settled answer rather than `None`: an empty decision with no address, which no caller
records and the credential proxy does not treat as provisional, since the installed gcloud
cannot grow the flag while the pod runs; a probe that did not answer says nothing about which
gcloud is installed and is asked again next time:

| Field                 | Content                                                                           |
| --------------------- | --------------------------------------------------------------------------------- |
| `flags`               | `("--dns-endpoint",)`, `("--internal-ip",)` or `()`                               |
| `kind`                | `dns`, `internal-ip` or `ip`                                                      |
| `address`             | the hostname or IP the kubeconfig will name                                       |
| `same_network`        | `True`, `False`, or `None` when the agent's own network was not needed or unknown |
| `authorized_networks` | the listed CIDRs when the list is enabled (possibly empty), or `None`             |
| `remedy`              | one sentence naming what would make the cluster reachable, or `""`                |
| `provisional`         | `True` when the own-cluster describe gave no answer and the decision fell back    |

`dns_endpoint_args()` stays as the wrapper returning only `flags` as a list. Its callers,
`platform_mcp_server.py` and `stall_watch.py`, inherit rule 3 without a signature change;
`cluster_agent_profile.py` and `credential_proxy.py` call `endpoint_decision()` themselves,
the first because it writes the decision out and the second because it reads the
`provisional` mark. The describe widens to
`json(controlPlaneEndpointsConfig,privateClusterConfig,networkConfig.network,networkConfig.subnetwork,masterAuthorizedNetworksConfig,endpoint)`;
gcloud's `json()` projection drops any nested key not named, which is why `subnetwork` is
spelled out.

The remedies are the module's `REMEDY_*` constants, composed once so the scaffold log and the
preflight card read the same words:

- rule 3 found the private endpoint routable but not admitted: `REMEDY_ADMIT_POD_RANGE`, with
  the Pod range spliced in;
- `ip` on the agent's network but in another region without global access, when the list is
  enabled or there is no public endpoint: `REMEDY_OTHER_REGION`, which names control-plane
  global access;
- `ip` on another network with a private endpoint, when the list is enabled or there is no
  public endpoint: `REMEDY_OTHER_NETWORK`, which leads with the DNS endpoint because an
  address on the list cannot open a private endpoint the agent cannot route to;
- `ip` with the list enabled otherwise: `REMEDY_IP`;
- `dns`, `internal-ip`, or any other cluster whose list is not enabled: no remedy. An `internal-ip`
  decision never carries one, because it is only made where the list already admits the agent.

## The diagnostic

`cluster_agent_profile.create_profile()` already writes the cluster's identity into the
profile's `USER.md` as `- key: value` bullets and mirrors that file into the sandbox. The
decision joins it:

```
- endpoint: ip
- endpoint-address: 203.0.113.10
- authorized-networks: 203.0.113.5/32
- endpoint-remedy: Add 10.92.0.0/14 to this cluster's authorized networks ...
```

`authorized-networks` reads `unrestricted` when the list is not enabled and
`enabled, no ranges listed` when it is enabled and empty; the `endpoint-remedy` bullet is
written only when there is a remedy (so never beside `endpoint: internal-ip`), and when it is
written the file ends with a sentence saying it applies only if `kubectl` cannot reach the API
server, since `USER.md` is the Cluster Agent's startup context. No endpoint bullet is written when nothing
could be decided. After the mirror, the scaffold probes the cluster with
`kubectl version --request-timeout=5s` in the sandbox under the pinned kubeconfig, as the
`agent` login that owns the file. When kubectl exits non-zero it logs one line with the endpoint
kind, the address, the list, and kubectl's last line (just kubectl's last line when nothing was
decided), adding the remedy only when kubectl's
output is a connection failure (a timeout, no route, a refused dial, matched on those causes
rather than on kubectl's generic `Unable to connect to the server:` prefix, which also fronts
x509 and auth-plugin failures the list cannot fix) rather than an answer the server gave
(anything kubectl fronts with `Error from server`, a degraded etcd's "request timed out"
included) or a message the credential-proxy shim itself produced (its `credential proxy:`,
`credential proxy unavailable`, `credential proxy token unavailable` and `credential proxy
error` prefixes). When the probe itself does not finish inside its outer bound,
or cannot run, the log says that and nothing about the endpoint. The scaffold still returns
normally: a cluster that is unreachable now may be reachable after the operator acts, and a
scaffold that failed would only be retried on the next reconcile tick with the same result.

`cluster_preflight.sh` check 5 reads the bullets back, `endpoint` through the existing
`user_md_field()` and the other three through `user_md_text()`, which keeps a value's case and
spacing. It appends the endpoint and list to its `reason`, and prefixes its `remediation` with
the remedy when kubectl's output matches the same connection-failure pattern and the same
credential-proxy exclusion, both of which a test holds equal to the Python copies, and only
when kubectl failed on its own bound: a call the preflight's own cap killed sat in the broker,
which the list cannot fix. A profile scaffolded before this change
carries no bullets and preflight prints what it prints today.

In a sandboxed install the credential proxy's managed kubeconfig for a cluster is the one every
brokered `kubectl` uses, and two things write it: the proxy's own fetch on a cache miss, which
runs this rule, and any caller's `get-credentials`, whose output the proxy files under the
context it selects. A caller that names no endpoint flag (the Platform Agent running the
command by hand, the fleet-upgrade-verification skill) would otherwise move a cluster back to
its public IP for everyone, so the proxy splices the rule's flags into such a fetch when it can
read the target from the argv (the cluster positional and `--location`, `--region`, `--zone`
or `-z`, in either flag form and on either side of the verb, skipping the values of gcloud's
value-taking global flags; `--project` likewise, falling back to the proxy's own
`GKE_PROJECT_ID`, which its bootstrap made gcloud's default project); a caller that names
`--dns-endpoint` or `--internal-ip` is run as given. Every writer therefore applies the same rule to the same
describe. `USER.md` records the scaffold's own decision, except a provisional one: when the scaffold's
describe of the agent's own cluster failed, the proxy decides again for the unflagged fetch and
may splice `--internal-ip`, so the scaffold records nothing rather than an endpoint the
kubeconfig does not end up naming, and the probe reports the connection alone.

## What stays as it is, and why

Other copies of the endpoint rule exist. None changes here.

- **`scripts/installer/gke_dns_endpoint.sh`** runs on a workstation or in CI, outside any GKE
  VPC, to reach the management cluster. Its header comment names this design as the reason it
  carries no `--internal-ip` branch. The bring-your-own-CI case belongs to the Terraform split
  tracked in the enterprise-pilot epic, #2591.
- **The awk program in `platformagent_manifests.go`** fetches the agent's own cluster's
  credentials at credential-proxy bootstrap. It has the exposure this design describes when the
  agent's own cluster restricts Master Authorized Networks, and every VPC-native cluster reports
  a private endpoint, so switching it changes every install at once. That is a separate change,
  decided on evidence the live validation below gathered.
- **`ClientConfigForIdentity` in `k8s-operator/internal/clusterprofiles/endpoint.go`** serves
  the event watcher and the drift detector, which run in the agent image. Rule 3 would apply
  to it unchanged, with its own describe of the host cluster; it does not yet, and its comment
  names the gap until a follow-up closes it.
- **`dns_endpoint_args()` in the fleet-upgrade-verification skill** decides from the record
  `clusters list` returned, without a describe. In a sandboxed install the broker splices this
  rule into the skill's unflagged fetch, so it keeps today's DNS-only rule.

`test_gke_endpoint_parity.py` keeps holding the Python, shell and awk copies to one DNS truth
table. One case is added: with the agent's own identity absent, a same-shape private cluster
yields no flag from all three.

## Tests

- `test_gke_endpoint.py`: the decision matrix. The enterprise shape (list enforced on the
  private endpoint, a different subnet, the agent's Pod range inside a listed block); the
  NAT-only list, which keeps the public IP and names the Pod range; the shared subnet; the
  list not enforced; another region with and without global access; a zonal location in the
  same region; another network; no private endpoint; IP endpoints disabled; DNS open on a
  same-network cluster; the identity unset or partial; the own-cluster describe failing, empty,
  or short, none of which is cached.
- `test_cluster_agent_profile.py`: the bullets land in `USER.md`, with the conditional note
  beside a remedy; the probe runs as the `agent` login; a connection failure logs the
  diagnostic with the remedy, while a 403 or a shim that could not reach the proxy logs it
  without; the scaffold still returns the profile name; a passing probe logs nothing; the
  connection-failure and exclusion patterns equal the shell copy's.
- `test_gke_endpoint.py` also pins the `provisional` mark on a failed own describe, that an
  absent identity is cached, and that a private-only cluster in another region names global
  access.
- `test_credential_proxy.py`: a caller's unflagged `get-credentials` gets the rule's flags
  spliced in, with the target read through gcloud's flag shapes and the project defaulted to
  the broker's own; one that names an endpoint is run as given; a kubeconfig written from a
  provisional decision, or from none, by either writer, is served inside its window and
  refetched after it; a settled fetch or a caller's own flag clears the mark; a failed refetch
  serves the file on disk; a caller's fetch files its result without the kubeconfig lock.
- `test_cluster_preflight.py`: check 5's JSON carries the bullets and the remedy on a connection
  failure, the bullets without the remedy on a 403, and today's text when there are no bullets.
- `test_gke_endpoint_parity.py`: the added case above.

## What was observed on a real install

The rule was validated against a throwaway zonal cluster on the same VPC and subnet as a
running install: private nodes, an authorized-network list holding one documentation range
and enforced on the private endpoint, DNS endpoint closed. The image before this rule wrote the
public IP into the onboarded profile's kubeconfig; an early build of the rule (same network
only, before the admission test) wrote the private IP, the endpoint bullets and a remedy; from
the agent pod and the sandbox the private endpoint answered in under a second and the public
IP timed out; preflight named the endpoint, the list and that build's remedy once the cluster
was gone. The private endpoint admitted the agent although the list excluded every agent
range: that observation is what the shared-subnet clause of rule 3 rests on, and under the
final rule the same cluster takes the same endpoint through that clause. The agent pod also
reached its own cluster's private endpoint, the evidence the bootstrap follow-up needs.
