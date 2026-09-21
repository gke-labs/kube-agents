# drift-detector

Pulls GKE audit records from the Pub/Sub subscription
[`terraform/modules/drift-pubsub`](../../../terraform/modules/drift-pubsub/) provisions, parses
them, and turns out-of-band cluster changes into `gitops-drift` injects.
[`docs/designs/drift-detection.md`](../../../docs/designs/drift-detection.md) is the design; this
file is how to work on the code.

Kubernetes audit is not served by the Kubernetes API on GKE. The control plane is managed, so the
audit stream surfaces only in Cloud Logging, and a Log Router sink exports it to a topic. That is
why this is a Pub/Sub consumer and not an informer like its sibling
[`k8s-event-watcher`](../k8s-event-watcher/README.md).

## What ships today

Ingestion, classification, the `managedFields` join, and the inject: pull, parse, assign a tier,
forward the records that represent a real human change, enrich each with what the live object says
owns the fields, and post the result to the core-agent daemon — `session_kv_server.py`, the Session
KV server, which the sibling event watcher posts to under the same name — as a `gitops-drift` inject. Nothing
builds this binary into an image and nothing launches it, so reaching that pipeline is something an
operator does by hand today and no installation detects drift on its own.

The inject is off unless `--daemon-url` is set, and off is the default. With it off the detector
does everything else and stops at the `DRIFT` log line, which is how it ran before T4 and is what a
local run against a real subscription wants. With it on, each surviving record opens a session and
posts one payload into it; `logDriftEvent` still runs first either way, so the `DRIFT` line is
emitted whether or not a daemon is configured and whether or not the inject lands.

Two things about that are worth knowing before relying on it. A record whose inject fails is
**acked anyway and never redelivered** — the handler signature returns nothing, so there is no way
to tell the subscriber to nack, and the `INJECT FAILED` log line naming the `insert_id` is the only
remaining trace of the change. And because Pub/Sub delivers at least once while the subscriber acks
after the handler returns, redelivery is ordinary rather than exceptional: the detector remembers
the last few thousand `insertId`s it has injected and suppresses a repeat before opening a second
session, so one redelivered batch does not page a human twice for one change. That set is in memory
and per-process, which leaves one case uncovered: a non-graceful exit leaves its batch unacked, and
the next instance starts with an empty set and injects those records again. A climbing `duplicate=`
count in the shutdown tally is a sign the ack deadline is too tight, not that the cluster is busy.
Climbing alongside `failed=` means something else — a record is marked seen before its send, so a
failed inject is not retried on redelivery either.

What the daemon does with the payload is
[`session_kv_server.py`](../../../agents/platform/scripts/session_kv_server.py)'s half: it routes on
`kind`, so a `gitops-drift` payload gets its own chat alert and its own triage card — addressed to
the agent for the cluster the change was made on, which the fan-in means is not necessarily the one
the detector runs in — instead of being rendered through the event watcher's path, where the field
defaults would describe it as a Pod. Drift is recorded as a `Warning` in the daemon's ledger — the
chat line names no severity — but billed to a daily ceiling
of its own rather than to the event watcher's, because one `kubectl apply` over a directory is one
human action and several audit entries: sharing the bucket let routine use of this signal cap-drop
the watcher's for the rest of the day. The two ceilings therefore add up, which is the cost of the
split; what it does not fix is the fan-out itself, since the detector coalesces nothing. A record
the ceiling refuses is lost rather than deferred: the daemon answers
`suppressed`, the detector has already marked the `insertId` as seen, and the intercepted-events
ledger row the daemon writes is the only place that change survives. `GET /v1/alert-quota` is where
a spent budget shows up.

The join has two credential sources and they are additive. `--in-cluster` or `--kubeconfig` gives it
the one directly reachable cluster; `--profiles-dir` gives it every cluster in `--project` that has
a Cluster Agent profile, reached as the pod's own Google identity through
[`internal/clusterprofiles`](../../internal/clusterprofiles/) — one shared token source, not a
credential per cluster. With neither set the join reaches nothing and every record naming a live
object comes out `unreachable`. The subscription is project-wide, so any cluster in the project
that neither source covers still comes out `unreachable`, and the shutdown line names those
clusters.

## Running it

```bash
go run ./k8s-operator/cmd/drift-detector --project "$PROJECT_ID"
```

With escalation on, which is off unless you ask for it:

```bash
# The token is passed by the *name* of the variable holding it, never by value:
# a flag is visible in the process table and in this binary's own startup line.
export SESSION_KV_API_KEY=$(kubectl get secret platform-agent-secrets \
  -n kubeagents-system -o jsonpath='{.data.SESSION_KV_API_KEY}' | base64 --decode)

go run ./k8s-operator/cmd/drift-detector \
  --project "$PROJECT_ID" \
  --kubeconfig "$HOME/.kube/config" \
  --cluster-name "$CLUSTER_NAME" \
  --cluster-location "$CLUSTER_LOCATION" \
  --daemon-url http://127.0.0.1:8699 \
  --token-env SESSION_KV_API_KEY \
  --owner drift-detector
```

`--kubeconfig` rather than `--in-cluster` because this is a `go run` on a workstation:
`--in-cluster` reads the ServiceAccount token a Pod is given and finds nothing outside one. Swap it
back for a run inside the agent Pod.

`--cluster-name` and `--cluster-location` are not optional here: `--in-cluster` and `--kubeconfig`
each give the join one cluster's credentials, and with `--project` these two are what name the
cluster those credentials reach. Omit either and startup refuses the flags rather than joining
against the wrong cluster.

**The daemon is loopback-only**, and that decides where this binary can run. `docker-entrypoint.sh`
starts it with `--host 127.0.0.1 --port 8699`, so `--daemon-url` has nothing to point at from
outside the agent Pod's network namespace. For a local run against a real install, forward the port
first — `kubectl port-forward -n kubeagents-system <agent-pod> 8699:8699`, which only works where the
install does not sandbox the pod. For a deployed one, the detector has to be a container in that Pod;
nothing builds or launches it there yet, which is why the flag defaults to empty and the inject is
off.

Application Default Credentials need `roles/pubsub.subscriber` on the subscription — inside the
agent pod, the Workload Identity the `drift-pubsub` module grants it to. Add `roles/pubsub.viewer`
for the startup ack-deadline check described under
[Three things to know before changing it](#three-things-to-know-before-changing-it): subscriber does
not carry `subscriptions.get`. The module grants both, so this is a note for a
hand-made subscription or a local run, where the check is skipped with a log line rather than
failing the process.

The join needs Kubernetes permissions on top of that, and they are not the same grant. With
`--in-cluster` the Pod's ServiceAccount has to be able to `get` every resource a human might change
— a cluster-wide `get` on `*` is the honest shape of it, since the audit stream names arbitrary
groups including CRDs. A resource it cannot read comes out `failed` with the RBAC error on the
line, not silently unenriched.

`--profiles-dir` needs two more grants, and they attach to the pod's **Google** identity rather
than to its ServiceAccount, because that is what a profile cluster is reached as. Addressing the
cluster at all takes `container.clusters.get` — `roles/container.viewer` is the usual shape — in
`--project`, and only there: a profile naming a cluster in another project is dropped on its
identity before anything is spent on reaching it, so no grant outside `--project` is wanted or
used. Without the grant every profile is skipped at startup with
`asking the GKE API where … is: 403` and the detector joins nothing it did not already reach — with
one exception, and it is the cluster that matters most on a single-cluster install: the profile for
the cluster `--in-cluster`/`--kubeconfig` already reaches is declined before it is addressed, so
that cluster is still joined, through a credential that needs no Google grant at all.
Reading the objects then takes the same cluster-wide `get` as above, bound to that Google identity
on every cluster in the fleet. A cluster where only the second is missing still starts: its records
come out `failed` with the RBAC error, which is the difference between a cluster that was not
addressed and one that was and refused.

| Flag                      | Default                          | Notes                                                                                                                                                                                                                                                                                            |
| ------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--project`               | —                                | Required. The project holding the subscription. With the join on it must be the project **ID**, not the project number: a Pub/Sub path accepts either, but the join matches this against each record's `project_id`, so a number matches nothing. Refused at startup.                            |
| `--subscription`          | `platform-agent-drift-audit-sub` | A bare id, or the module's fully qualified `subscription_id` output. Both work.                                                                                                                                                                                                                  |
| `--max-messages`          | `100`                            | Messages per pull, 1 to 1000.                                                                                                                                                                                                                                                                    |
| `--automation-principals` | empty                            | Comma-separated principals to treat as automation. Applies to every cluster the subscription carries.                                                                                                                                                                                            |
| `--human-domains`         | empty                            | Comma-separated domains whose accounts are human. Matched exactly, so subdomains are listed separately. Empty means any principal carrying a domain.                                                                                                                                             |
| `--log-dropped`           | `false`                          | A log line per filtered record. On a live cluster that is nearly the whole stream.                                                                                                                                                                                                               |
| `--in-cluster`            | `false`                          | Read live objects with the Pod's own ServiceAccount. Mutually exclusive with `--kubeconfig`.                                                                                                                                                                                                     |
| `--kubeconfig`            | empty                            | Read live objects through this kubeconfig. Mutually exclusive with `--in-cluster`; with no `--profiles-dir` either, the join is disabled.                                                                                                                                                        |
| `--cluster-name`          | empty                            | The GKE cluster those credentials reach. Required with either of the two above, and an error without them. Checked at startup against the cluster they actually reach; a disagreement stops the process.                                                                                         |
| `--cluster-location`      | empty                            | That cluster's region or zone. Required with `--cluster-name`: a name is unique only within a project and location.                                                                                                                                                                              |
| `--profiles-dir`          | empty                            | Hermes profiles directory, normally `/opt/data/profiles`. Every Cluster Agent profile whose cluster is in `--project` becomes a joinable cluster. Combines with the two above; a profile naming the cluster they already reach is dropped in favour of them.                                     |
| `--gitops-managers`       | empty                            | Comma-separated `managedFields` managers that are the GitOps controller. Matched exactly, and only on writes to the object rather than through a subresource, in a second later than the audited change. Empty means no reconciliation claim is made.                                            |
| `--batch-join-budget`     | `30s`                            | Longest one batch may spend on lookups; 1ns to 5m. Startup warns if it exceeds half the subscription's real ack deadline.                                                                                                                                                                        |
| `--daemon-url`            | empty                            | Core-agent daemon to post the `gitops-drift` inject to, without a trailing slash, query or fragment. Probed at startup and refused if it does not understand the kind. Empty disables the inject: records are still classified, joined and logged, and nothing is escalated.                     |
| `--token-env`             | empty                            | **Name** of the environment variable holding the daemon's bearer token, not the token. Required with `--daemon-url`, and an error without it — a flag value is visible in the process table.                                                                                                     |
| `--owner`                 | empty                            | `X-Asserted-Caller` for the session the inject opens. Sent, but not read: nothing in the daemon looks at the header today, and `POST /sessions` is guarded by the bearer token alone and stamps its own metadata. Set it anyway, so the value is on the wire before anything starts checking it. |

## Classification

Four tiers. `system` is a `system:` prefix; `automation` is any `*.gserviceaccount.com` account or
a principal named in `--automation-principals`; `human` is a positive test — the principal carries a
domain, and one of `--human-domains` when that is set; `unattributed` is everything else.

The CUJ 3 task breakdown ([#467](https://github.com/gke-labs/kube-agents/pull/467)) specifies three
of those. It drops `system:`, drops an allowlist, and calls the residual human.
Over 24 hours of live audit logs across three projects, that residual was 1230 calls and every one
was a machine: 1110 from `kubelet-nodepool-bootstrap`, which carries neither the prefix nor the
service-account suffix, and 120 unauthenticated requests with an empty principal. Public GKE
endpoints get crawled, and a rejected probe from Googlebot, Baiduspider, Amazonbot or any of the
others is an audit entry with a mutating `methodName`. An allowlist cannot close that, because it has to
anticipate every identity GKE invents. A positive human test plus a tier for the leftovers makes an
unknown principal loud rather than wrong, and the unattributed principals are logged by name so
there is something to write the next rule from.

The service-account match is the whole `.gserviceaccount.com` domain. The Google-managed accounts —
`<number>-compute@developer`, `@cloudbuild`, `@appspot`, `@cloudservices` — carry no `iam` label, so
matching `.iam.gserviceaccount.com` alone would send a Cloud Build pipeline to the human tier.

**Domains are folded, usernames are not.** Both domain tests — the service-account suffix and
`--human-domains` — are case-insensitive, because DNS is, and an unfolded suffix sends
`deployer@proj.iam.GSERVICEACCOUNT.COM` to the human tier on the strength of its `@`.
`--automation-principals` matches the whole principal and is deliberately exact: a Kubernetes
username is case-sensitive by specification.

`--human-domains` matches the exact domain rather than its subtree, so `example.com` does not cover
`ada@corp.example.com` and an organisation using subdomains lists them. Widening it would widen what
the detector reports as somebody's drift, and no fleet measured here spreads its accounts that way;
the misses are not silent either, since an unmatched principal lands in `unattributed` and is logged
by name. A leading `@` or `.` on a configured value is stripped before matching, so `.example.com` —
the conventional way to write a domain elsewhere, and therefore what an operator reaches for — is
the same configuration as `example.com` rather than one that quietly matches nothing at all.

**Two identities this cannot see through**, both of which fail closed — a real change classified as
a machine and dropped, rather than a false report. A person acting through a ServiceAccount token
arrives as `system:serviceaccount:<ns>:<name>`, indistinguishable in the audit record from the
controller that normally holds it. A person acting through an impersonated GCP service account
arrives as that account. Neither leaves an `unattributed` entry, so neither is visible in the
shutdown report the way a missing rule is. Separating them would need the user agent or
`serviceAccountDelegationInfo`, and the classifier consults neither — the user agent is parsed and
printed on the drift line, just never classified on. `serviceAccountDelegationInfo` is not the way
in it looks either: it is absent from every `k8s_cluster` audit record across seven days on three
projects, which leaves the user agent as the only lead. Worth knowing before reading a quiet human count
as an empty cluster.

**A mutating verb is not a mutating call.** `kubectl exec` is audited as
`io.k8s.core.v1.pods.exec.create` — which contains `create`, so it matches the sink's
`create|patch|update|delete` filter — and it names a real object (`.../pods/<name>/exec`), so the
parser's "named no object" drop does not catch it either. Left alone, a person exec-ing into a pod
is classified `human`, succeeds, and is reported as a change they never made. This is measured, not
theoretical: `pods.exec.create` is present in the Admin Activity log on a live project.
`nonDeclarativeSubresources` drops the six that arrive this way — `exec`, `attach`, `portforward`
and `proxy` (pod session subresources), `ephemeralcontainers` (`kubectl debug`) and `token`
(`kubectl create token`) — after classification, counted as `non_declarative` so the drop is
visible rather than silent. `ephemeralcontainers` is the one that really does mutate the stored
object, and it is still not drift: the Git-side object is the Deployment that owns the pod, which
is unchanged, so there is nothing to revert or codify. Subject access reviews get to the same place
by another route — they name no object, so the parser already discards them. `status`, `scale` and
`eviction` are deliberately not in the set: the first two are real declarative writes, and
`eviction` can destroy a Git-side object where the six above cannot. Evicting a Deployment-owned
pod changes nothing in Git, but evicting a pod applied from a manifest of its own removes the
object Git declares, and a subresource name cannot tell the two apart — only the live object's
owner references can. The join below reads `managedFields` and not `ownerReferences`, so it does not
close this: a `kubectl drain` still produces a line per pod.

A server-side dry run is the same category and is **not** handled. No `dryRun` marker appeared in
seven days of Admin Activity logs across three projects, which leaves it open whether GKE surfaces
one at all — so a dry-run write by a person would currently be reported as drift, and the first
step on it is establishing what the payload looks like rather than writing a rule for a shape
nobody has seen.

**A failed call is not drift.** The audit log records attempts: writes rejected by RBAC, refused by
admission, or aborted after losing an optimistic-concurrency race. A 24-hour query for failed
mutating calls on one project returned its full 5000-row limit — a floor, not a total — with 4793 of
those status 10. (That 5000 and the 10,000 below are two different ceilings because the two queries
passed different `--limit` values, not because either number is a typo; both are floors.) Those
particular records are `system:` tier and the tier filter would drop them
anyway; the outcome filter earns its place on the human side, where 46 of the 879 human calls
measured over 30 days failed (5.2%), `PERMISSION_DENIED` among them. A change someone was stopped
from making is the clearest case of something that is not drift. `AuditRecord.Succeeded` gates the
forward, and classification still happens first: a cluster whose human changes are all being denied
has to look different from one with no human changes.

**On the tier ratios the breakdown predicts.** It expects roughly 78% system, 20% automation and 1%
human (its own rounding; the three do not sum to 100). Measured post-exclusion over a 15-minute
window on each of three projects — short enough that no query hit the 10,000-row cap, so these are
complete counts and not floors — the split is 97.4–98.5% system and 1.5–2.4% automation, with
`system:cluster-autoscaler` alone accounting for 48–62% of the whole post-exclusion stream. Humans
do not appear in those windows at all; a 30-day query found 879 human calls across the same three
projects, about 29 a day, from six principals. Six of one project's 3046 records fell to
`unattributed` — principals carrying neither a `system:` prefix, a service-account domain, nor an
`@` at all.

**Volume is per-project, and the row cap makes it easy to get wrong.** Every 24-hour volume query
here came back with exactly 10,000 rows, which is that query's cap rather than an answer. Counted
over windows short enough to avoid it, the post-exclusion stream ran 1 to 10 calls a second —
roughly 100k to 840k a day — and two windows twelve minutes apart on one project differed by 30%.
`drift-pubsub` measured 0.7 a second on a quieter two-cluster project. Take the order of magnitude
rather than the figure. The unfiltered stream hit the cap inside 15 minutes on all three projects,
so it is at least 11 a second, and the sink filter is what stands between the two. That range is
also why the progress line has a time bound as well as a record count: 10,000 records is seventeen
minutes at the top of it and close to four hours at the bottom.

The progress line covers a cluster nobody is changing; it cannot cover a subscription delivering
nothing, because it is emitted from the filter and a record that never arrives never reaches it. An
empty pull is not an error either, so a sink whose filter stopped matching produces no output of any
kind. `subscriber.Run` therefore reports an idle line on the same fifteen-minute bound, carrying the
running totals — zeroes since start-up mean the pipeline was never wired up, non-zero ones mean it
worked and has gone quiet. Between the two, the pod logs something every fifteen minutes in every
state it can be in.

The absolute human number is close to the spike's estimate of seven a day per cluster; the
denominator differs by orders of magnitude, which is what makes the filter worth building. Human
traffic is bursty, so a day with none is ordinary and says nothing about whether the human rule
still works — which is why `TierCounts.String` always prints every tier, including the zeroes.

Eight of the nine fixtures in `testdata/` are captured from live Cloud Audit Logs. Identifiers are
substituted, and a `request` or `response` body that ran long is replaced with a stub marked
`_trimmed`; nothing else is edited. `"authenticationInfo": {}` is the shape that justifies the
fourth tier. The ninth, `human_exec.json`, is **derived** rather than captured: the live query
established that `pods.exec.create` reaches the Admin Activity log but did not yield an entry that
could be shipped, so the method, permission and `resourceName` of a captured envelope were replaced
with the exec form. It is the only fixture that exercises a `resourceName` carrying a subresource,
which is what the subresource rule turns on, so replacing it with a real capture is worth doing.

## The join

Classification says a person changed something. The join says what, by fetching the live object and
decoding `metadata.managedFields` — the API server's record of which field manager owns which field.
One `GET` per forwarded record, which the tier filter has already cut to roughly ten a day per
project: the 879 human calls above are 29 a day across all three, and the outcome and
non-declarative filters take a further slice off that before the join sees anything.

`ResourceRef` already carries the group, version and plural resource, so the lookup goes straight to
a dynamic client with no RESTMapper: `resourcename.go` keeps the audit log's plural rather than
converting to a Kind precisely so that this stays a struct copy. The subresource is deliberately not
requested. A write to `status` changes the parent object, whose `managedFields` carries the status
claim as an entry of its own, so fetching the parent gets both; asking for the subresource returns a
body with no `managedFields` at all.

**Which cluster serves a record is decided on the full `project/location/cluster` triple**, not on
the cluster name. The join holds a map keyed on that triple, and a record is routed by the triple it
carries in `resource.labels` — the control plane's own account of which cluster served the call.
Keying on the name alone would read `prod/deployments/api` from `europe-west1`'s `prod` when the
audited change happened on `us-central1`'s, and that lookup does not fail: it returns a real object
and reports its ownership as though it were the audited one. The same reasoning drives
`targetCluster.identity` in `k8s-event-watcher`. A record whose triple is incomplete is refused
before the lookup rather than left to miss on its own, so a partially labelled record can never be
served by a cluster that happens to share the parts it does carry.

**Two sources fill that map, and the direct one wins an overlap.** `--profiles-dir` contributes one
entry per Cluster Agent profile; `--in-cluster`/`--kubeconfig` contributes the cluster the process
itself can reach. They overlap on every install, because reconcile gives the management cluster a
profile like any other cluster, and the direct credential is kept: it reaches
`kubernetes.default.svc` as the pod's Kubernetes service account and never leaves the cluster, where
a profile authenticates as the pod's Google identity against the control-plane endpoint, which IAM
without `roles/container.viewer` or a master authorized network can refuse. That profile is declined
during the scan rather than dropped after it, on the same grounds as the out-of-project drop below:
addressing it costs a GKE describe for a getter that is discarded, and on an install without
`container.clusters.get` the describe fails and the profile is reported as skipped — telling an
operator that a cluster this detector enriches normally will not be joined. The absorbed cluster is
named at startup, and separately from the skip count, so a profile count that does not match the
cluster count has its explanation in the same place as the counts without inflating the number that
reads as lost coverage.

**A profile for a cluster outside `--project` is dropped, and that is correctness rather than
economy.** The subscription is a project-level sink, so a record can only ever name a cluster in
that project; a profile for a cluster elsewhere — which the Platform Agent legitimately writes,
since a fleet can span projects — produces a client no record can match. Dropping it also keeps the
real misconfiguration visible: an operator who pointed `--project` at the wrong project sees eight
profiles skipped as outside it, rather than a detector that starts cleanly and enriches nothing.

Profile discovery follows `internal/clusterprofiles`' failure policy, which is the watcher's: a
`--profiles-dir` that was given and is not there is fatal, because discovery runs once and a restart
fixes it; an unreadable one degrades to a log line; and a single bad profile is skipped so one
unparseable `config.yaml` cannot cost the whole fleet. A profile whose dynamic client will not build
is skipped on the same grounds. Every skip is logged by profile name and counted, and the count is
reported at startup — a fan-in that silently reached six of seven clusters otherwise looks exactly
like a fleet of six.

The unreadable directory is the exception to that counting, and it is carried separately rather than
added to the total: the number of clusters behind a directory nobody can open is unknown, so calling
it one skipped profile would understate it. It is tracked at all so that the line naming why the
join has no clusters can tell it from an empty directory: both end the scan with no clusters and
nothing skipped, and reporting the first as the second sends an operator whose mount is broken, or
whose `fsGroup` is wrong, to wait on `cluster-agent-reconcile` for profiles it has already written.
Discovery runs once, so that line is the whole account of why the fan-in is off for the life of the
pod.

**Startup checks the direct cluster's triple against the cluster its credentials actually reach, and
refuses to run if they disagree.** The triple decides which records that cluster serves; the
credentials decide where it reads them from, and nothing else connects the two. A Pod in `us-east4`
started from the `us-central1` manifest, or a local run against the wrong `kubectl` context, would
otherwise pass every flag check and enrich one cluster's records from the other's objects — the
failure the triple exists to prevent, reached from the other end. It is the one startup check that
stops the process rather than logging, because it is the only one whose failure produces confident
wrong output: every lookup succeeds, so no outcome is counted `failed` or `unreachable` and the
shutdown counts read healthy. Where the ack-deadline check costs redelivery, this one costs
correctness.

The two credential modes are checked from different evidence. `--in-cluster` reads the node's
`cluster-name` and `cluster-location` metadata attributes, which is exact — the Pod reads its own
cluster, and that needs no IAM grant. `--kubeconfig` has no such channel, so the check parses the
`gke_<project>_<location>_<cluster>` context name `gcloud container clusters get-credentials`
writes. Either source failing is "not established", never "mismatch": a kind cluster publishes no
GKE attributes and a renamed or hand-written context parses as nothing, and both log a line saying
the identity went unverified and carry on, because refusing there would break the local runs
`--kubeconfig` exists for.

The profile clusters are not checked this way and need not be: a profile's identity is read from the
same `cluster_identity` that addressed its endpoint, so there are not two claims to disagree. The
flags are the only place a human states which cluster a credential reaches. The check runs before
the profile scan, so a run with both sources configured still refuses on a mismatch without first
minting a token per profile.

Five outcomes, all of them counted in the shutdown line. The table lists them by how a reader meets
them, not in the order `join` decides them — and one of those decisions is worth knowing on its own:
the `no_object` test runs before the cluster lookup, so a delete is `no_object` whether or not the
join has a cluster to read. "The join is off" therefore does not mean "everything is `unreachable`".

`unreachable` is the one whose count stopped being self-explanatory when the fan-in landed. With one
cluster the missing set was inferable — everything else in the project — and with a fan-in it is
not, so the shutdown report names each unreachable cluster with a count, capped at
`maxUnreachableClusters` with the rest collected under a label. Not every entry is a
misconfiguration: a project holding a cluster nobody intends to onboard reports it every run, and
the detector cannot tell that from one whose profile failed to write.

| Outcome       | Means                                                                                                                                                                                                                          |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `enriched`    | The object was read and `owners=` carries its field ownership.                                                                                                                                                                 |
| `no_object`   | Nothing to fetch: a delete, or a create whose name the API server had not assigned when audited.                                                                                                                               |
| `gone`        | The cluster served the path and answered `NotFound`. The object existed when the call was audited and does not now.                                                                                                            |
| `unreachable` | The record names a live object this process cannot read: a cluster in the project with neither credentials nor a Cluster Agent profile, or any cluster at all when neither source is configured. The shutdown line names them. |
| `failed`      | Any other lookup error: RBAC, a network fault, a timeout, an API group or version the cluster does not serve.                                                                                                                  |

A 404 answers both of the last two, so they are told apart by what the error names rather than by
its status code: a genuine absence names the group, resource and object that were looked up, while
a refusal of the path names nothing. Without the distinction a CRD uninstalled — or a served
version retired — between the audited write and the lookup would be reported `gone`, which says the
object was deleted about an object still standing under another version.

**The join fails open, and that is the opposite of what classification does.** The tier filter drops
anything it cannot prove is a human change, because a false report costs an operator's attention.
The join forwards every outcome, because what it adds is detail: dropping a confirmed human change
on a lookup error would discard the finding to protect the annotation on it. An outcome other than
`enriched` is a `DRIFT` line without an `owners=` field, never a missing line.

**`reconciled_by=` is a positive claim only.** With `--gitops-managers` set, a named manager whose
`managedFields` timestamp falls in a later second than the audited change marks the event
`Reconciled` — the GitOps controller has written since, so there may be nothing left to revert. A
missing time on _either_ side declines the claim rather than guessing at it: guessing "after" hides
real drift, guessing "before" invents a reconcile that never happened. Both sides matter because the
zero time sorts before every real one, so a record that arrived without a `timestamp` — an absent or
null key decodes to the zero time without error — would otherwise read as earlier than any
configured manager that had ever touched the object, and report that manager's last write as the
reconcile for a change the detector cannot place in time. With the flag unset the detector cannot
tell a GitOps controller from any other client, so `Reconciled` is false everywhere and means "not
shown to be reconciled", never "shown not to be".

**Both sides are floored to the second first, and the comparison is strict.** The two timestamps
come from different components and are not recorded at the same precision: `metav1.Time` marshals as
RFC 3339 with no fractional part, so every `managedFields` timestamp arrives floored to the whole
second, while the audit timestamp keeps its nanoseconds from Cloud Logging. Flooring the audit side
to match is what stops which of them sorts first turning on how much of the second had elapsed.

On that shared grid, a write in the change's own second does not claim — because it may _be_ the
change. A manager name is self-declared: the API server copies whatever the client passed in
`--field-manager` and verifies nothing, so `kubectl apply --server-side
--field-manager=kustomize-controller` run by a person produces a single entry carrying the
configured name, no subresource, and a time floored into the audited change's own second. Under an
at-or-after rule that entry satisfies the claim by construction, and the change goes out
`Reconciled` on the strength of being itself. It needs no intent: configure a manager name that
ordinary `kubectl` also emits and every human apply self-marks. The price of the strict comparison
is the reconcile that genuinely lands inside the same second, which is now missed — a false claim
suppresses the report, a missed one only leaves it noisy, and this is the direction every other
judgment here fails in. A reconcile that crosses the second boundary, which is the ordinary one,
still claims.

**A claim made through a subresource does not count.** `managedFields` records a write to `status`
as its own entry, and every GitOps controller writes `.status` on its own custom resources — a
`Kustomization`, a `HelmRelease`, an `Application` — under the same manager name, on every
reconcile loop. Counting those would make the wrong answer the systematic one rather than the rare
one: `flux suspend kustomization apps` patches `spec.suspend`, the controller writes its conditions
four seconds later, and the change goes out `Reconciled` while the suspension still stands. Entries
with a subresource are therefore skipped, and the manager's own write to the object — a separate
entry — is what can still make the claim.

The reverse case is the price: a person patching `/status` directly, answered by the controller's
own status write, is a reconcile this declines. Matching the entry's subresource against the
audited change's own would catch it, and is wrong for `kubectl scale` — the audit record carries
subresource `scale`, while the controller answers it by re-applying the object under no subresource
at all, so the two would never line up. Declining reports the drift, which is the direction every
other judgment here fails in.

It is also the weakest claim the data supports. `managedFields` keeps no previous value, so it
cannot say the person's change was reverted — the controller may have written an unrelated field.
The flag exists so the agent is told when there is reason to look.

Manager names are matched exactly and case-sensitively. A field manager is a free string the client
chooses rather than a DNS name, so there is no case-folding rule to appeal to here the way there is
for a service-account domain, and `argocd-controller` and `ArgoCD-Controller` really can be two
clients. The names your fleet uses are worth reading off a live object (`kubectl get <obj> -o
yaml --show-managed-fields`) rather than assumed.

**Attribution quality depends on Server-Side Apply.** A client that does `Update` rather than
`Apply` still gets an entry, but a coarser one. An entry whose `FieldsV1` blob is missing or does
not decode is kept with no paths rather than dropped: "nobody owns this field" is exactly the false
positive the join exists to avoid, and a manager rendered with an empty path list is visibly a gap
in the data instead.

Field paths are rendered from `FieldsV1` with the list selector intact —
`spec.containers[name=app].image`, not `spec.containers.image`, which does not say which container
drifted. A manager that owns a whole object owns hundreds of leaves, so `maxReportedPaths` truncates
the rendered list at twelve and appends `...` as a thirteenth entry — behind a comma, because a path
can itself end in a selector and `containers[name=app]...` would read as a path rather than as the
marker. The cap is on the output only, and the ownership decision reads every path.

**Those selectors put object field values in the log.** Two of the three `FieldsV1` list forms carry
data rather than structure: `k:` holds the merge key's value and `v:` holds a whole scalar entry, so
a rendered path can read `spec.ports[name=admin-postgres].port` or `spec.rules["10.1.2.3/32"]` —
the `v:` form keeps the entry's JSON quoting and carries no `v` marker into the output.
The drift line goes to this process's stdout and from there to Cloud Logging, which is a different
place from the cluster whose object it came out of. Merge keys are names, ports and protocols on
the resources this join reads, so nothing high-value has turned up in practice — but the join reads
whatever the audit stream names, CRDs included, and a CRD is free to pick a merge key that carries
something an operator would not put in a log. Worth knowing before pointing this at a fleet whose
CRDs you did not write; dropping the value half of a selector would cost the thing the selector is
for, which is saying which entry drifted.

## The startup handshake

With `--daemon-url` set, the binary probes `GET /healthz` before it touches the subscription and
refuses to start unless `gitops-drift` is on the `inject_kinds` the daemon advertises.

The daemon dispatches on the payload's `kind` with an equality test, and a daemon predating that
dispatch has no way to say so. The drift payload falls into its event path, where the defaults
render it as a `Warning` Pod alert named `default/` for reason `Unknown`: it bills the event
watcher's daily ceiling rather than drift's own, writes a ledger row the watcher's recap counts as
one of its events, and answers `200` — so `Inject` here reports success, the `insertId` is marked
seen, and the record is never re-offered. Silent at both ends, and one `kubectl apply` over six
objects is six such alerts.

The skew is an ordinary deployment window rather than a hypothetical. The daemon script is copied to
the shared PVC from the agent image while this binary ships in its own, so the two roll
independently — which is why the event watcher negotiates its own capabilities with
`X-Watcher-Features`. This is the same trade in the other direction: there the daemon has to know
what the producer can do, here the producer has to know what the daemon can.

It fails closed. An unreachable daemon, a non-200, a body that will not parse, and a reply carrying
no `inject_kinds` at all are all refusals, because treating any of them as permission is the
behaviour the check exists to prevent. A startup check rather than a per-record one because the
answer cannot change under a running process, and a refusal rather than a fall back to log-only
because an operator who set `--daemon-url` asked for escalation and the two modes are
indistinguishable in every later line. The error names dropping the flag as the way to get the
degraded mode deliberately.

## Three things to know before changing it

**The join happens before the ack, inside the batch's deadline.** Synchronous pull does not extend
the ack deadline while a handler runs, so every `GET` a batch makes has to finish inside the
subscription's deadline or the whole batch is redelivered — and a redelivered batch is re-enriched
and re-logged, not resumed. `--batch-join-budget` caps a batch's handling, defaulting to thirty
seconds against the sixty the `drift-pubsub` module sets on the subscription. It is a flag rather
than a constant because the deadline it is sized against is a Terraform variable: a hand-created
subscription carries Pub/Sub's own ten-second default, and `--batch-join-budget 4s` is how that
install stays inside it without rebuilding the image. The budget wraps the whole per-record
handler, classification included, not the lookup alone — classification is CPU-bound and the
lookup is the part that can hang, but the deadline covers both. Exceeding it fails open like any
other lookup error: the records that would have reached a `GET` come out `failed` and are still
acked, while a delete or a foreign-cluster record is unaffected, since both are settled before the
context is consulted. Adding retries, a second `GET`, or a per-record backoff means re-checking
that arithmetic.

**The inject spends that same budget, and it is the slowest thing in it.** With `--daemon-url` set,
a surviving record makes two more calls after the join — `POST /sessions` then
`POST /sessions/<id>/inject` — each with its own ten-second client timeout and one retry behind a
250ms delay. How much of the batch one hung daemon spends depends on where it hangs, and the second
call only adds to the bill if the first one leaves time for it. A daemon that never answers
`POST /sessions` costs two client timeouts plus the retry delay, about 20.25s, because the inject is
never reached; a daemon that answers the session instantly and then hangs the inject costs the same
20.25s, for the mirror-image reason. Around forty seconds needs both ceilings paid on both attempts,
which means a session answered just inside its own timeout and an inject that then hangs — the worst
case, not the ordinary one. So one hung daemon costs most of a thirty-second batch on record one and
can cost all of it, and every record behind it then fails its lookup on a context at or near expiry
and is acked anyway. So the escalation gets a sub-budget of its own,
`perRecordInjectBudget`, five seconds derived from the handler's context — small enough that a
batch survives several slow records, and derived rather than independent so a SIGTERM or an
exhausted batch still cuts it short.

That bounds the blast radius rather than removing it. Six consecutive unresponsive records still
exhaust a thirty-second budget between them, and what the operator sees when they do is a batch of
`failed` **lookups** — every later record's `GET` returning a deadline error — which reads
identically to a slow control plane and is not. Check the inject tally on the shutdown line before
blaming the API server, and turn the inject off with an empty `--daemon-url` to tell the two apart.
Sizing the budget for an install with the inject on still means budgeting for the daemon's latency
as well as the control plane's; a per-record budget covering the whole handler, rather than the
escalation alone, is the fix that would remove the coupling and is tracked separately.

For the budget to be the bound, it has to be the only one, and client-go supplies a second by
default: a `rest.Config` that leaves `QPS` unset gets 5 requests a second with a burst of 10, and
every lookup waits on that token bucket before it touches the network. Against a batch of 100
human writes that is eighteen seconds of the thirty spent queueing on the client, and the
`--batch-join-budget 4s` above would stop enriching after about thirty records — while reporting
them as lookup failures, because a budget that runs out mid-throttle surfaces as a plain context
deadline, indistinguishable from a slow control plane. `cluster.go` therefore sets both to
`--max-messages`' own ceiling, so a full batch is issued without the client ever waiting. Raising
that ceiling raises the throttle with it; leaving them out of step puts part of the budget back out
of reach.

The flag's own validation cannot do that re-checking for you: it is bounded by a fixed ceiling of
5m — half Pub/Sub's 600-second maximum deadline — not by whatever this subscription is set to, so
`--batch-join-budget 120s` is accepted against a stock 60-second install. What closes the gap is one
`subscriptions.get` at startup, which reads the configured deadline and warns when the budget takes
more than half of it — half because the deadline starts at delivery and the batch is settled only
once its `Ack` returns, so the remainder is what that round trip runs in. Half is the margin rather
than the cliff: a budget past it has spent the room the `Ack` was meant to run in, which is why the
warning says Pub/Sub _may_ redeliver rather than that it will. The warning is advisory in both
directions. A budget that overruns costs redelivery rather than correctness, so it does not refuse
to start; and the probe needs `pubsub.subscriptions.get`, which `roles/pubsub.subscriber` does not
carry but `roles/pubsub.viewer` does, so a probe that fails says the budget went unchecked and the
detector pulls anyway. The `drift-pubsub` module grants viewer alongside subscriber, so the check is
live in an install built from it.

**Settling is three-way.** A record that parses is handled and acked. One that is understood and
not actionable — another service's audit entry, or a call that named no object, such as a subject
access review — is acked and dropped, with a per-batch count in the log so a sink whose filter
stopped matching does not read as a quiet cluster. Anything else is nacked, so a payload-shape
change on Google's side redelivers rather than silently acking drift away. A payload the parser
cannot recognise at all is a nack, not a drop: `json.Unmarshal` zeroes what it cannot match, so a
restructured payload arrives looking exactly like an empty one.

**The `resourceName` grammar has two ambiguous shapes**, both handled explicitly in
`resourcename.go`: the namespace object itself (`core/v1/namespaces/foo`, where `namespaces/<ns>`
is the object rather than the scope), and a create, whose name the API server has not assigned when
the call is audited. The closed set of namespace subresources is the one rule in that file not read
off a specification — check it against live fixtures before trusting it.
