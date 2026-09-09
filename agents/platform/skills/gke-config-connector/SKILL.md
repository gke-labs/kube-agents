---
name: gke-config-connector
description: >-
  Authors Config Connector (KCC) manifests for Google Cloud resource changes
  and hands them to submit-suggestion as a pull request under the GitOps
  repository's clusters/<cluster>/provisioning/ path. Use when a request
  changes a GKE cluster or node pool (grow, autoscale, adopt), a Cloud SQL
  instance (add a read replica, resize), or a VPC firewall rule on an install
  whose GitOps repository holds Config Connector YAML. Decides create versus
  acquire read-only, validates every field with kubectl explain against the
  customer's CRDs, and never applies. Don't use for Kubernetes workload
  manifests (use gke-manifest-generation), imperative cluster creation (use
  gke-cluster-creation), or Terraform HCL.
---

# GKE Config Connector (KCC) Authoring

A Google Cloud change leaves this agent only one way: as a Config Connector
manifest in the GitOps repository, reviewed by a human and applied by the
customer's reconciler. This skill writes that manifest. It reads the cluster
and the cloud project to decide what to write, validates the result against
the CRDs the customer actually runs, and opens the pull request through
**submit-suggestion**. Nothing here mutates a cluster or a project, and the
command policy refuses the verbs that would.

## When to Use

- **A GKE node pool or cluster change** — grow or shrink a pool, turn on
  autoscaling, change a release channel or maintenance window, adopt a
  cluster the repo does not yet describe.
- **A Cloud SQL change** — add a read replica, change a tier, enable backups
  or high availability.
- **A VPC firewall rule** — open or close a port, narrow a source range,
  disable a rule, turn on logging.
- **Adopting a resource** that exists in Google Cloud but has no manifest in
  the repository (acquisition).

## When NOT to Use

- **Kubernetes workloads** (Deployments, Services, NetworkPolicies): the
  `gke-manifest-generation` skill.
- **Creating a cluster imperatively** with the `gke` MCP tools or `gcloud`:
  the `gke-cluster-creation` skill.
- **A repository whose `provisioning/` holds Terraform HCL** rather than KCC
  YAML: say so and stop. This skill authors KRM only.
- **Kinds outside the families catalogued in `references/`.** Other Config
  Connector kinds follow the same annotations and workflow, but their
  immutable fields are not catalogued here; validate every field with
  `kubectl explain` and say in the PR body that the kind is outside the
  catalogued set.
- **Fixing a fleet-audit finding**: the `fleet-audit` skill opens that PR.

## Prerequisites (the customer's, not yours)

Config Connector is a customer platform decision. Never propose installing
or enabling it; state the missing piece and stop.

1. **The CRDs are installed on a cluster your kubeconfig reaches.** The
   check is `kubectl get crd <kind-plural>.<group>.cnrm.cloud.google.com`
   (Step 2). No CRD means no schema to validate against and no controller to
   reconcile the manifest. Stop with: "Config Connector is not installed on
   `<context>`; this change needs a KCC-enabled cluster before I can author
   it."
2. **The repository has a KCC path.** `clusters/<cluster>/provisioning/`
   in the reference layout, holding YAML with `cnrm.cloud.google.com`
   apiVersions. If the repo has no such directory and the user has not named
   one, ask rather than invent a layout.
3. **A namespace bound to a project.** The sibling manifests carry either a
   `cnrm.cloud.google.com/project-id` annotation on each resource or on the
   namespace (`ConfigConnectorContext`). Copy what the siblings do.
4. **Read access to the `cnrm.cloud.google.com` API groups** for the
   create-versus-acquire check. Which identity answers depends on where the
   KCC objects live: on the management cluster reached in-cluster, the
   read-only ClusterRole the operator ships grants none of those groups, so
   `kubectl auth can-i` says `no`; on a separate hub reached through
   `get-credentials`, the Google service account's IAM permission set
   decides, and the default `read-only` set's cluster-viewer role does read
   custom resources. Either way a `no` is a fact to report, not an absence
   to infer from.

## Workflow

### Step 1: Find the target path and its conventions

Open the repository with **submit-suggestion** `prepare` (its Step 1). It
needs the branch name up front, and the convention for this skill is
`platform-agent/kcc-<kind>-<name>`, the same branch Step 5 submits on. Then
list what is already under `provisioning/`:

```bash
S="$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py
SCRATCH=$(mktemp -d)
"$S" prepare --repo "<owner>/<repo>" --branch "platform-agent/kcc-<kind>-<name>"
"$S" list --handle "<handle>" --prefix clusters/<cluster>/provisioning
"$S" fetch --handle "<handle>" --path clusters/<cluster>/provisioning/<sibling>.yaml --to "$SCRATCH"
```

(Directory mode: `ls` and `cat` inside the returned `workspace`.) From a
sibling manifest take the `metadata.namespace`, how the project is bound,
the label conventions, and one file per resource or one per kind. A manifest
for the resource you were asked to change may already be there; then the
change is an edit to that file, and Step 2's answer is "edit".

### Step 2: Decide create, acquire, or edit — read-only

Config Connector acquires an existing Google Cloud resource when a new
manifest's name (or `spec.resourceID`) matches it, and creates one when
nothing matches. A create manifest for a resource that exists silently adopts
it after merge; an acquisition manifest whose stated immutable fields differ
from the live resource fails in `status.conditions` with `cannot make changes
to immutable field(s)`. Decide before you write, and say which in the PR body.

Check permission before the object read, so a `Forbidden` is reported as
such rather than mistaken for "not there":

```bash
kubectl get crd containernodepools.container.cnrm.cloud.google.com
kubectl auth can-i get containernodepools.container.cnrm.cloud.google.com -n <namespace>
kubectl get containernodepool <name> -n <namespace> -o yaml
```

Then the live resource, with the reads the command policy allows:

```bash
gcloud container clusters describe <cluster> --location <location> --project <project> --format=json
gcloud container node-pools describe <pool> --cluster <cluster> --location <location> --project <project> --format=json
gcloud compute firewall-rules describe <rule> --project <project> --format=json
```

| Manifest in repo | KCC object in cluster | Resource in Google Cloud | Decision                                                                 |
| ---------------- | --------------------- | ------------------------ | ------------------------------------------------------------------------ |
| yes              | any                   | any                      | **Edit** that file. Touch only the fields the request needs.             |
| no               | yes                   | yes                      | **Stop.** Something outside the repo manages it; report the object.      |
| no               | yes                   | no                       | **Stop.** A failed or in-flight create; report its `status.conditions`.  |
| no               | no                    | yes                      | **Acquire.** `references/acquisition.md`.                                |
| no               | no                    | no                       | **Create.**                                                              |
| no               | forbidden / no KCC    | unknown                  | Report what you could not read. Ask the user which it is; do not assume. |

`gcloud sql` has no entry on the read allowlist. For a `SQLInstance` the
in-cluster object is the only read you have; when that is forbidden or
absent, ask the user to confirm whether the instance exists rather than
guessing (`references/sql.md`).

### Step 3: Author the manifest

- **apiVersion `v1beta1`** of the kind's group; per-family details in
  `references/`.
- **`metadata.namespace`**: the siblings' namespace. Never `default`, never a
  namespace you did not see in the repo.
- **Annotations on every manifest** (`references/acquisition.md` explains
  each):
  - `cnrm.cloud.google.com/deletion-policy: abandon` — a pruned or deleted
    manifest detaches the resource instead of destroying it.
  - `cnrm.cloud.google.com/state-into-spec: absent` — Config Connector
    leaves fields the manifest omits under external management instead of
    writing live state back into `spec`, which a pull reconciler would read
    as drift.
  - `cnrm.cloud.google.com/project-id: <project>` when the siblings set it
    per resource; omit it when they rely on the namespace binding.
- **Acquisition adds** an explicit `spec.resourceID` equal to the Google
  Cloud resource name, and every immutable field it does state is copied
  from the live resource so the spec matches what exists.
- **Write the fields the request needs plus the required ones, nothing
  else.** An omitted field stays externally managed; a copied default becomes
  a managed field the next reviewer has to reason about.
- **Immutable fields are the line.** If the request can only be met by
  changing one (a machine type, a region, a firewall's network), the answer
  is replacement, not an edit: say so, name the field, and stop for the
  user's decision. Never author a replacement unasked.
- **Names**: Kubernetes names in `metadata.name`; a Google Cloud name that
  is not a valid Kubernetes name goes in `spec.resourceID`.

### Step 4: Validate against the customer's CRDs

Server-side dry-run is a write from the API server's point of view: it needs
write RBAC the platform role lacks, and the command policy refuses `apply`,
`create`, and `diff` regardless. Validate with the schema instead:

```bash
kubectl explain containernodepool --api-version=container.cnrm.cloud.google.com/v1beta1
kubectl explain containernodepool.spec.autoscaling --api-version=container.cnrm.cloud.google.com/v1beta1
kubectl explain containernodepool.spec.autoscaling.totalMaxNodeCount --api-version=container.cnrm.cloud.google.com/v1beta1
```

- Run `explain` on the kind and on **every** `spec.<path>` you wrote. A path
  the CRD does not have is a field the customer's Config Connector version
  does not know; drop or rename it, do not ship it.
- A description that begins `Immutable.` marks an immutable field. That is
  the authority over the lists in `references/`, which were written against
  one version of the CRDs.
- `explain` reads discovery, which every authenticated user has, so it works
  even where `auth can-i get` said `no`.
- A missing CRD (`explain` reports the resource is not found) is prerequisite
  1 unmet: stop with the message there.

### Step 5: Hand off as a pull request

Write the manifest into the scratch directory (content mode) or the workspace
(directory mode) at `clusters/<cluster>/provisioning/<kind>-<name>.yaml`
unless the siblings use another convention, and submit it through
**submit-suggestion** with branch `platform-agent/kcc-<kind>-<name>`. Use
this shape for `--body`, after the skill's standard header:

```
### Config Connector change
- **Decision:** create | acquire | edit — and the reads that decided it
  (object present/absent, the `describe` result, or "user confirmed").
- **Resource:** <kind> `<name>` in project `<project>`, location `<location>`.
- **Immutable fields touched:** none | <field> (this PR proposes replacement; see below).
- **Validated:** `kubectl explain` on <kind> and <n> spec paths against the CRDs on `<context>`.
- **Not run here:** `kubectl apply --dry-run=server`. The agent is read-only; run it on the
  KCC cluster before merging, or let CI do it.
- **After merge:** the reconciler applies; KCC reports readiness in the object's `status.conditions`.
```

Record the PR URL for the user. Addressing review feedback follows
**submit-suggestion** Step 5.

## Red Lines

- No `kubectl apply`, `create`, `patch`, `edit`, `delete`, `diff`, or any
  `--dry-run=server`; no mutating `gcloud`. The command policy refuses them
  and a refusal is not a retry prompt.
- Never set `deletion-policy` to `none` or remove the annotation. Deleting a
  resource is a human's PR that flips it deliberately.
- Never delete a manifest under `provisioning/` to "recreate" a resource.
- Never fill in a project, namespace, network, or location you did not read
  from the repository, the cluster, or the user.
- Never propose enabling Config Connector, granting its IAM, or creating a
  `ConfigConnectorContext`.

## References

- **[Acquisition and annotations](references/acquisition.md)**: how Config
  Connector matches a manifest to an existing resource, what each annotation
  does, and the immutable-field failure modes.
- **[ContainerCluster and ContainerNodePool](references/container.md)**:
  fields, immutables, `describe`-to-spec mapping, create and acquire examples.
- **[SQLInstance](references/sql.md)**: read replicas, tier changes, the
  missing `gcloud sql` read, create and acquire examples.
- **[ComputeFirewall](references/compute-firewall.md)**: rule shape,
  immutables, `describe`-to-spec mapping, create and acquire examples.
