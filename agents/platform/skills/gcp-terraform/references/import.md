# Import blocks

Facts below are from the `google` provider's source and docs at 8.x
(September 2026) and OpenTofu 1.13. The provider's page for the
repository's pinned version is the authority.

## When

The target exists in Google Cloud and no block in the repository describes
it (SKILL.md Step 2). A plain new resource block would plan as a create,
which fails against the existing resource or, worse, races it. An `import`
block tells CI's `plan` to adopt the resource into state instead.

Import blocks need Terraform 1.5 or OpenTofu 1.6 and later. If the
repository's `required_version` is older, stop and say so: the adoption is
a `terraform import` command someone with the state runs by hand.

## Shape

An `import` block is only allowed in the root module, the directory CI
runs `plan` in; OpenTofu and Terraform both refuse one inside a module, and
`tofu fmt` does not catch it. Put it in a root-module file beside the
resource, or, when the resource is in a module, in the root module with the
module address in `to`:

```hcl
import {
  to = google_container_node_pool.batch
  id = "my-project/us-central1/prod-east/batch"
}

resource "google_container_node_pool" "batch" {
  # every argument copied from the live resource that the provider
  # would otherwise plan to change, including replace-forcing ones
}
```

`to` is the resource address, with the module path when the resource is in
a module (`module.gke.google_container_node_pool.batch`). Write the
resource's arguments from the `describe` output so the plan shows an import
and no change; any argument that differs is a change the plan applies on
the same run, and a replace-forcing one turns the import into a
replacement. The PR body says the plan must show `import` and no replace.

## ID formats

| Resource                       | `id` forms the provider accepts                                                                                        |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `google_container_node_pool`   | `{project}/{location}/{cluster}/{pool}`, `{location}/{cluster}/{pool}`                                                 |
| `google_container_cluster`     | `projects/{project}/locations/{location}/clusters/{cluster}`, `{project}/{location}/{cluster}`, `{location}/{cluster}` |
| `google_sql_database_instance` | `projects/{project}/instances/{instance}`, `{project}/{instance}`, `{instance}`                                        |
| `google_compute_firewall`      | `projects/{project}/global/firewalls/{name}`, `{project}/{name}`, `{name}`                                             |

Use the form with the project in it: the short forms take the provider's
default project, which you cannot see.
