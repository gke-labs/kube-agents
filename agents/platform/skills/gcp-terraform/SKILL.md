---
name: gcp-terraform
description: >-
  Writes Terraform (HCL) for Google Cloud resource changes and hands them to
  submit-suggestion as one pull request, on an install whose GitOps
  repository keeps clusters/<cluster>/provisioning/ (or its Terraform root) as
  HCL. Use when a request creates or changes a GKE cluster or node pool (new
  cluster, grow, autoscale, adopt), a Cloud SQL instance (read replica,
  resize), or a VPC firewall rule, and the repository holds .tf files for it.
  Edits the existing resources and modules in the repository's own style,
  decides edit, import or create read-only, flags replacements and
  deletion-protection changes, checks syntax with tofu fmt, and never runs
  init, plan or apply. Don't use for Config Connector YAML (use
  gcp-config-connector), Kubernetes workload manifests (use
  gke-manifest-generation), or Helm charts (use gitops-helm-chart).
---

# Terraform Authoring

On a repository that keeps its Google Cloud resources as Terraform, a
change leaves this agent as an edit to that HCL, reviewed by a human and
applied by the customer's CI, which holds the state and the credentials.
This skill writes the edit. It reads the repository and the cloud project
to decide what to write, keeps to the repository's own style, checks the
syntax with `tofu fmt`, and opens the pull request through
**submit-suggestion**. Nothing here plans or applies: `terraform plan` is
the reviewer's or CI's step, because only CI has the state to plan against.

## When to Use

- **A GKE node pool or cluster change** on a Terraform repository: grow or
  shrink a pool, turn on autoscaling, change a release channel or
  maintenance window, add a new cluster, adopt a cluster the repository
  does not describe. A new cluster comes here when `SOUL.md` §3 item 7
  routes it here, which it does for a Terraform repository.
- **A Cloud SQL change**: a read replica, a tier, backups, high
  availability.
- **A VPC firewall rule**: open or close a port, narrow a source range,
  disable a rule, turn on logging.
- **Adopting a resource** that exists in Google Cloud but not in the
  repository (an `import` block).

## When NOT to Use

- **A repository whose `provisioning/` holds Config Connector YAML**:
  `gcp-config-connector`. A repository can hold both in different cluster
  directories; use the format the target's own directory uses.
- **Kubernetes workloads**: `gke-manifest-generation`. **Helm charts**:
  `gitops-helm-chart`.
- **Resource types outside the families in `references/`.** The workflow
  holds for them, but their replace-forcing fields are not catalogued here:
  read the provider's documentation page for the resource at the
  repository's pinned provider version, and say in the PR body that the
  type is outside the catalogued set.
- **Fixing a fleet-audit finding**: the `fleet-audit` skill opens that PR.

## Workflow

### Step 0: A stop ends the request

Every "stop" in this skill is the answer, not a reason to look for another
way in. When one applies, report it in the words given, change nothing, and
end: on a kanban card with `kanban_block(kind="needs_input")` and the stop as
the reason. Do not go on to check what you are allowed to change, try another
identity, or search for credentials or another repository.

The first stop comes before anything else: the install must have a GitOps
repository registered, and it must be reachable. One command answers both,
and its one line is final:

```bash
python3 "$HERMES_HOME"/scripts/gitops_preflight.py
python3 "$HERMES_HOME"/scripts/gitops_preflight.py --repo "<owner>/<repo>"
```

Run the first form before anything else, and the second once you know which
repository the change goes to. `REGISTERED: ...` (exit 0) means go on. Any
other line (`NO_REGISTERED_REPO`, `REPO_UNREACHABLE`, `STATE_UNREADABLE`,
`BROKER_UNAVAILABLE`) is the stop: block with that line as the reason and
change nothing.

### Step 1: Open the repository and learn its Terraform

Open it with **submit-suggestion** `prepare` (its Step 1) on branch
`platform-agent/tf-<type>-<name>`, then find the Terraform that owns the
target:

```bash
python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \
  --repo "<owner>/<repo>" --branch "platform-agent/tf-<type>-<name>"
ls <workspace>/clusters/<cluster>/provisioning
grep -rlE 'resource "google_container_(cluster|node_pool)"|resource "google_sql_database_instance"|resource "google_compute_firewall"' <workspace> --include=*.tf
grep -rnE '^\s*source\s*=|required_version|required_providers' <workspace> --include=*.tf
```

From the files that own the target, take:

- **Resources or modules.** Whether the repository writes
  `resource "google_container_node_pool"` blocks or calls a module (its own
  under `modules/`, the public `terraform-google-modules`, or
  kube-agents' own `gke-cluster`). A change to something a module creates
  is a change to that module call's inputs, never a new resource beside it.
- **Versions.** The `required_providers` pin for `google` and the
  `required_version`. Write only syntax that version accepts: `import`
  blocks need Terraform 1.5 or OpenTofu 1.6 and later.
- **Style.** How names, variables, `locals`, labels and `for_each` are
  written, and whether values are literals or `var.`/`local.` references.
  Copy it; a reviewer should not be able to tell your block from theirs.
- **The resource address** of the target (`google_container_node_pool.<name>`
  or `module.<name>.google_container_node_pool.<name>`), for the PR body.

**Stopping before Step 5 leaves nothing to clean up.** The copy is on this
filesystem. Stop where you need to (a question from Step 2's table, an
unasked replacement in Step 3, a syntax error you cannot resolve) and run
`prepare` again when the work resumes.

### Step 2: Decide edit, import or create, read-only

Read the live resource with the reads the command policy allows:

```bash
gcloud container clusters describe <cluster> --location <location> --project <project> --format=json
gcloud container node-pools describe <pool> --cluster <cluster> --location <location> --project <project> --format=json
gcloud compute firewall-rules describe <rule> --project <project> --format=json
```

| In the repository | In Google Cloud | Decision                                                                                                        |
| ----------------- | --------------- | --------------------------------------------------------------------------------------------------------------- |
| yes               | any             | **Edit** that block or module call. Touch only what the request needs.                                          |
| no                | yes             | **Import.** An `import` block plus a resource whose arguments match the live resource (`references/import.md`). |
| no                | no              | **Create**, in the repository's style.                                                                          |
| no                | unknown         | Report what you could not read. Ask the user whether it exists; do not assume.                                  |

`gcloud sql` has no entry on the read allowlist. For a Cloud SQL instance
the repository is your only read; when the instance is not in it, ask the
user whether it exists rather than guessing (`references/sql.md`).

A resource in the repository is not proof that the live one matches it:
the CI's state is the record, and you cannot read it. Say in the PR body
which reads decided the edit.

### Step 3: Write the change

- **Edit in place.** Change the arguments the request needs, add the ones
  it requires, and nothing else. An argument you copy from the live resource
  becomes one someone has to own.
- **A replacement is written only when the request names it.** An
  argument that forces replacement (`references/` lists them per family;
  the provider's page for the pinned version decides) destroys and recreates
  the resource when the pull request is applied. Write one only when the
  request names that argument or its direct effect ("switch the pool to
  Spot VMs" names `spot`), and lead with it: the PR body's **Replacement**
  line names the argument and what is destroyed and recreated (the pool's
  nodes and their workloads, or the whole cluster), and so does your reply.
  Name a route that avoids the replacement if one exists (a new pool beside
  the old one) as an alternative in the body, without writing it. When the
  request states a goal and only a replace-forcing argument would meet it
  ("more pod IPs" is `cluster_ipv4_cidr`), or only your way of writing it
  would cause one, never write it: say so, name the argument, and stop for
  the user's decision.
- **Deletion protection.** Never set `deletion_protection = false`, remove
  it, remove a `lifecycle { prevent_destroy = true }`, or change a
  `deletion_policy` (`ABANDON` gets past `deletion_protection`). A
  requested cluster replacement is still written, with the **Deletion
  protection** line saying that applying it fails until a human sets
  `deletion_protection = false` in a separate change. Anything else that
  needs one of them is a human's pull request: stop and say so.
- **A new cluster's deletion protection** is on, written the way the
  repository's existing clusters write it (the resource argument or the
  module input). With no cluster to copy, it is `deletion_protection = true`,
  the provider's default since 5.0.0. A sibling that has it `false` is not
  copied: write `true` and name that sibling in the **Deletion protection**
  line. Never `false`, for a test, an evaluation or an easier teardown: a
  cluster that needs it off is a human's pull request.
- **New resources** go where the repository keeps their siblings, in a file
  named the way the siblings are, using the same module or resource shape.
  Shape, not contents: a new cluster gets only what exists once per cluster
  (the cluster, its node pools, its fleet membership). A project- or
  fleet-wide resource the repository already declares (a
  `google_gke_hub_feature`, a `google_project_service`, a shared service
  account) is used by name or left alone, never declared a second time:
  copying a sibling's file copies those too, and a second declaration fails
  or fights the first at apply. Name the shared ones the cluster relies on
  in the PR body. `references/container.md` lists the common ones. Where
  each cluster is its own root module, its `backend` and provider blocks
  are copied with this cluster's own state prefix: a copied prefix points
  the new root at the sibling's state, and CI's plan would then propose
  destroying the sibling.
- **No secrets in HCL.** A password, key or token never goes into a `.tf`
  or `.tfvars` file; use the repository's existing reference (a
  `google_secret_manager_secret_version` data source, a variable CI fills)
  or stop and ask.

### Step 4: Check the syntax

`tofu fmt` parses every file it formats, so it is the syntax check that
needs no providers, state or credentials. Run it on the files you changed
or added, never the directory: a directory target rewrites every sibling
that is not already formatted, and `submit` commits every modified tracked
file, so the pull request would carry formatting the request never asked
for. Then apply the formatting to those files:

```bash
tofu fmt -check -diff <workspace>/clusters/<cluster>/provisioning/<file>.tf
tofu fmt <workspace>/clusters/<cluster>/provisioning/<file>.tf
```

- Exit 2 is a syntax error, naming the file and line: fix it and run
  again. It hides the formatting report until the file parses.
- Exit 3 with a diff is formatting only: the second command writes it, so
  the pull request matches `terraform fmt` and the reviewer's CI.
- Exit 0 means the files parse. It does not mean the arguments exist: `fmt`
  loads no provider schema, so a misspelled argument passes here and fails
  CI's `plan`. Check every argument you wrote against the provider's page
  for the pinned version.
- Nothing else. `tofu init`, `validate`, `plan`, `apply` and `import`, and
  any `terraform` command, need provider plugins from a registry and a cloud
  this shell cannot reach, and they are CI's to run.

### Step 5: Hand off as a pull request

Submit through **submit-suggestion** `submit` with the branch from Step 1.
Use this shape for the `--body-file` document, after the skill's standard
header:

```
### Terraform change
- **Decision:** edit | import | create, and the reads that decided it.
- **Resource:** `<address>` (<type> `<name>` in project `<project>`, location `<location>`).
- **Replacement:** none | REPLACES <address>: `<argument>` forces it; applying this PR destroys and recreates <what> (or deletes <what>).
- **Deletion protection:** unchanged | unchanged; applying this PR fails until a human sets `deletion_protection = false` in a separate change. | new cluster: `true`, as `<sibling address>` has | new cluster: `true`, the provider default (no existing cluster to copy) | new cluster: `true`; `<sibling address>` has `false` and was not copied.
- **Import:** none | `import { to = <address>, id = "<id>" }`; plan must show an import, not a create.
- **Checked:** `tofu fmt -check` on <path>. Not run here: `terraform init`, `validate` and
  `plan`. Run `plan` in CI before merging; it must show only the changes above.
```

Record the PR URL for the user. Addressing review feedback follows
**submit-suggestion** Step 5. On a kanban card, a stop from Steps 1 to 4
ends in `kanban_block(kind="needs_input")` with the reason, never in
`kanban_complete` with advice.

## Red Lines

- No `tofu` or `terraform` command other than `tofu fmt`. No mutating
  `gcloud`; the command policy refuses it and a refusal is not a retry
  prompt.
- Never set `deletion_protection = false`, remove it, remove
  `prevent_destroy`, or change a `deletion_policy`.
- Never write a replacement the request did not name, a removed resource
  or a `removed` block unasked.
- Never fill in a project, region, network or name you did not read from
  the repository, the cloud or the user.
- Never write a secret into HCL or a `.tfvars` file.

## References

- **[Import blocks](references/import.md)**: when to adopt instead of
  create, the block, and the ID formats.
- **[Clusters and node pools](references/container.md)**: arguments,
  replace-forcing fields, `describe`-to-argument mapping.
- **[Cloud SQL](references/sql.md)**: instances, replicas, the missing
  `gcloud sql` read.
- **[Firewall rules](references/compute-firewall.md)**: rule shape,
  replace-forcing fields.
