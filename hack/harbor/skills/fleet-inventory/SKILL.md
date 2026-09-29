---
name: fleet-inventory
description: Which clusters this agent manages. Use for any question about the fleet's membership.
---

The clusters this agent manages are recorded in
`/var/lib/kube-agents/clusters.yaml`, one entry per cluster with `name`,
`platform` and `location`. A background sync keeps it current; answer
membership questions from it and do not query clusters or cloud APIs to answer
them.
