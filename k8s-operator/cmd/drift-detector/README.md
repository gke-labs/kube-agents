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

Ingestion only: pull, parse, log. Nothing builds this binary into an image and nothing launches it,
so no installation runs it yet. Classification, the `managedFields` join, and the inject are still
to come; `recordHandler` in `subscriber.go` is the seam they plug into.

## Running it

```bash
go run ./k8s-operator/cmd/drift-detector --project "$PROJECT_ID"
```

Application Default Credentials need `roles/pubsub.subscriber` on the subscription — inside the
agent pod, the Workload Identity the `drift-pubsub` module grants it to.

| Flag             | Default                          | Notes                                                                           |
| ---------------- | -------------------------------- | ------------------------------------------------------------------------------- |
| `--project`      | —                                | Required. The project holding the subscription.                                 |
| `--subscription` | `platform-agent-drift-audit-sub` | A bare id, or the module's fully qualified `subscription_id` output. Both work. |
| `--max-messages` | `100`                            | Messages per pull, 1 to 1000.                                                   |

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
