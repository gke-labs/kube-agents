# Acquisition, annotations, and immutable fields

Shared semantics for every kind this skill authors. Written against the
`v1beta1` reference pages of Config Connector as published in September
2026; `kubectl explain` against the customer's CRDs is authoritative where
they differ.

## How Config Connector matches a manifest to a resource

- On the first reconcile of a new object, Config Connector looks in the
  bound project for a Google Cloud resource named `spec.resourceID`, or
  `metadata.name` when `resourceID` is unset. Found: it **acquires** the
  resource and starts managing it. Not found: it **creates** one.
- The manifest alone therefore does not say which will happen; the live
  state does. That is why SKILL.md Step 2 decides beforehand and the PR body
  states the decision: a reviewer who reads "create" should not be adopting
  a production resource by surprise.
- `spec.resourceID` is immutable and defaults to `metadata.name`. Spell it
  out on every acquisition, even when the two are equal, so the manifest
  says what it adopts.

## What goes wrong

| Situation                                                                  | What Config Connector does                                                                                                                                          | What to do instead                                                                                                            |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| A "create" manifest names a resource that exists                           | Acquires it. Fields the manifest states are enforced on the live resource; a stated immutable field that differs fails the object.                                  | Run Step 2; write an acquisition with the live immutable values.                                                              |
| An acquisition states an immutable field that differs from the live value  | `Update call failed: cannot make changes to immutable field(s)` or `infeasible update ... would require recreation` in `status.conditions`; nothing changes in GCP. | Copy the live value. The only way to change the field is delete-and-recreate, which is a human's decision, not this skill's. |
| A manifest is edited to change an immutable field                          | The admission webhook rejects the apply; a reconciler that does not handle admission errors sits stuck on the sync.                                                 | Stop and describe the replacement (new name, cutover, old resource abandoned); never author it unasked.                       |
| A manifest is deleted, or pruned by the reconciler                         | Deletes the Google Cloud resource, unless `deletion-policy: abandon` is set.                                                                                        | The annotation below, on every manifest.                                                                                      |

## Annotations

| Annotation                                     | Value        | Why                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cnrm.cloud.google.com/deletion-policy`        | `abandon`    | Default `none` deletes the cloud resource when the KCC object goes, including through a reconciler prune. `abandon` only detaches Config Connector. A human who wants the resource gone flips it in a reviewed PR first.                                                                                                                                                                                                                                                                                                                                                             |
| `cnrm.cloud.google.com/state-into-spec`        | `absent`     | Under `merge`, Config Connector writes the live values of every field the manifest omits back into the object's `spec`. The object in the cluster then no longer matches the file in git: a pull reconciler reports it out of sync for as long as the annotation stands, and with self-heal on it re-applies against Config Connector's writes. Under `absent`, omitted fields (list fields included) stay externally managed and the file stays the source of truth. The annotation is immutable once set, so it goes on the first commit. Upstream notes it is not supported on every kind; the kinds catalogued here are Terraform-based resources and are. |
| `cnrm.cloud.google.com/project-id`             | the project  | Which project the resource lives in. A resource-level annotation overrides one on the namespace. Copy the sibling manifests: per-resource if they carry it, omitted if they rely on the namespace's `ConfigConnectorContext`.                                                                                                                                                                                                                                                                                                                                                       |
| `cnrm.cloud.google.com/reconcile-interval-in-seconds` | leave unset | Default 600 s is right for a resource that changes by PR.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

## Which fields to state on an acquisition

1. The kind's **required** fields.
2. The **immutable fields that identify the resource** — location and
   `clusterRef` for a node pool, region for a SQL instance, network and
   direction for a firewall rule — copied from the live resource. Stating
   them is the guard: if the live resource differs, Config Connector fails
   the object instead of adopting the wrong one. A stated _mutable_ field is
   no guard at all: a wrong value is enforced on the acquired resource.
3. The fields the **request changes**, with their new values.
4. Nothing else. Under `state-into-spec: absent` an omitted mutable field
   stays externally managed; a copied default becomes something the
   reconciler now enforces and the next reviewer has to reason about.

Each family reference maps the `describe` output to the spec paths for
items 2 and 3.

## Namespaces and projects

Config Connector in namespaced mode reconciles each namespace with the
identity its `ConfigConnectorContext` names. A manifest in the wrong
namespace reconciles as the wrong service account or not at all, and neither
shows up until after merge. The namespace is always the one the sibling
manifests use.

## Replacement is a decision, not an edit

When a request needs an immutable field changed, the honest change is a new
resource under a new name (or `resourceID`), a cutover, and the old resource
abandoned by a later PR. That is two resources billed during cutover and a
data-migration or traffic-move step in between. Describe it in one paragraph
with the field named, and stop for the user's decision.
