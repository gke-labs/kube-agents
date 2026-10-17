<!-- kube-agents: local addition -->

## On a kube-agents install

- **No dollar figure without billing data.** Quote a cost, a dollar saving or a monthly total only
  from the billing export queries above, and say which query it came from. When the export is not
  configured or cannot be read, report waste in resource units (vCPU, GiB, nodes, disks) and say
  that a price needs billing data. Never multiply list prices, or the $0.10/hour cluster fee above,
  into a fleet total. A published discount range (Spot, committed use) may be quoted as Google's
  range, never as this fleet's saving. A fleet-wide waste question belongs to the `fleet-audit`
  skill's cost SOP (`fleet_wide_cost_analysis_sop.md`), which reports in resource units by design.
- **A cluster running kube-agents is not empty.** System pods (`kube-system`, `gke-*`, `gmp-*`) are
  not workload. The kube-agents operator, the agents and their sidecars are workload, wherever they
  run. Never call a cluster idle or empty, or recommend stopping or deleting it, without naming the
  workloads you counted on it.
- **Stay inside the install's scope.** Read `fleet_scope()` first, and leave out every cluster it
  lists in `excluded_clusters` (#3081).
