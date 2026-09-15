# drift-detector

Pulls GKE audit records from the Pub/Sub subscription
[`terraform/modules/drift-pubsub`](../../../terraform/modules/drift-pubsub/) provisions, parses
them, and — eventually — turns out-of-band cluster changes into `gitops-drift` injects.
[`docs/designs/drift-detection.md`](../../../docs/designs/drift-detection.md) is the design; this
file is how to work on the code.

Kubernetes audit is not served by the Kubernetes API on GKE. The control plane is managed, so the
audit stream surfaces only in Cloud Logging, and a Log Router sink exports it to a topic. That is
why this is a Pub/Sub consumer and not an informer like its sibling
[`k8s-event-watcher`](../k8s-event-watcher/README.md).

## What ships today

Ingestion and classification: pull, parse, assign a tier, forward the records that represent a real
human change. Nothing builds this binary into an image and nothing launches it, so no installation
runs it yet. The `managedFields` join and the inject are still to come; `recordHandler` in
`subscriber.go` is the seam they plug into, and `driftFilter.next` is where they attach.

## Running it

```bash
go run ./k8s-operator/cmd/drift-detector --project "$PROJECT_ID"
```

Application Default Credentials need `roles/pubsub.subscriber` on the subscription — inside the
agent pod, the Workload Identity the `drift-pubsub` module grants it to.

| Flag                      | Default                          | Notes                                                                                                 |
| ------------------------- | -------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `--project`               | —                                | Required. The project holding the subscription.                                                       |
| `--subscription`          | `platform-agent-drift-audit-sub` | A bare id, or the module's fully qualified `subscription_id` output. Both work.                       |
| `--max-messages`          | `100`                            | Messages per pull, 1 to 1000.                                                                         |
| `--automation-principals` | empty                            | Comma-separated principals to treat as automation. Applies to every cluster the subscription carries. |
| `--human-domains`         | empty                            | Comma-separated domains whose accounts are human. Empty means any principal carrying a domain.        |
| `--log-dropped`           | `false`                          | A log line per filtered record. On a live cluster that is nearly the whole stream.                    |

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
owner references can, which is T3's join. Until then a `kubectl drain` produces a line per pod.

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

## Two things to know before changing it

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
