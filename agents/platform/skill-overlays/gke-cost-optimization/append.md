<!-- kube-agents: local addition -->

## On a kube-agents install

- **No dollar figure without billing data.** Quote a cost, a dollar saving or a monthly total only
  from the billing export (see the `gke-cost-analysis` skill). Without it, report waste in resource
  units (vCPU, GiB, nodes, disks) and say that a price needs billing data. Never multiply list
  prices into a fleet total. A published discount range (Spot, committed use) may be quoted as
  Google's range, never as this fleet's saving.
- **A cluster running kube-agents is not empty.** "Stop idle dev clusters" above applies only to a
  cluster with no workload. System pods (`kube-system`, `gke-*`, `gmp-*`) are not workload. The
  kube-agents operator, the agents and their sidecars are workload. Never recommend stopping or
  deleting a cluster without naming the workloads you counted on it.
- **Stay inside the install's scope.** Read `fleet_scope()` first, and leave out every cluster it
  lists in `excluded_clusters` (#3081).
