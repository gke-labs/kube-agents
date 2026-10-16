output "members" {
  description = <<-EOT
    What each selector resolved to at plan time, by project ID, under the
    name the reconcile's snapshot gives the selector (sharedVpcHosts/<host>,
    metricsScopes/<scope>), so this output and the `containers` array of
    fleet_scope.json can be read side by side. A monitored project an
    exclude_projects entry names by number is neither named nor listed here;
    a member an entry names by ID is listed, and the kube-agents-iam module
    withholds its grant when it is a Shared VPC service project and keeps it
    when it is a monitored project, whose naming call the reconcile makes with
    that grant before the entry can match.
  EOT
  value       = local.scope_selector_members
}

output "uncarriable_members" {
  description = <<-EOT
    Projects a selector or a listed container named by an ID the scope
    cannot carry (a legacy domain-scoped ID): a Shared VPC host's service
    projects, a monitored project should the Monitoring API ever name one by
    such an ID rather than by number, and a project under a folder or
    organization whose clusters the Asset Inventory search named; keyed by
    the snapshot name of the selector or container. Left out of `members` and
    `container_members`, so bound nowhere and given no pool account, and
    reported by the module's check block as a warning on every plan they
    appear in.
  EOT
  value       = local.scope_uncarriable
}

output "container_members" {
  description = <<-EOT
    What each declared folder and organization was listed to at plan time
    for the scoped service account pool, by project ID, under the snapshot's
    key for the container (folders/<id>, organizations/<id>): a key per
    declared container while `list_container_members` is set, an empty list
    for one with no GKE cluster, and `{}` when it is not, since no container
    is read then. Less a member an exclude_projects entry names exactly by
    ID, and less one with an ID the scope cannot carry, which
    `uncarriable_members` lists. Feeds kube-agents-iam's
    scope_container_members, the pool alone: a member here gets no binding
    of its own (the container's is inherited) and no place in the
    resolved-set cap, which counts containers at runtime.
  EOT
  value       = local.scope_container_members
}

