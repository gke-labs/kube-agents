#!/usr/bin/env python3

"""What provisioning seeds into a pool project's GitOps repository, byte for byte.

Two files: the declared-intent note the declared-intent cases grade against,
and checkout-gateway's manifest the remediation cases put a budget beside.
scripts/provision_ci_pool_project.sh writes them (its GITOPS_INTENT_NOTE_* and
GITOPS_WORKLOAD_MANIFEST_* variables; scripts/test_verify_ci_pool_project.py
pins the two copies to each other), scripts/verify_ci_pool_project.py reads
them back and prints the replace command, and hack/fleet-fixture-state.py
holds a pool repository's file to the git blob sha of what is seeded here
(its `repository/<path>` subject, op `as_seeded`). This module is the one
place that content lives, so a pull request that changes it is the one
bench/kube_agents_bench/fixture_drift.py grades as usual throughout
(DRIFT_DECIDING_PATHS): a changed note makes every pool repository read as
drifted until an operator replaces the file, and that pull request must not
excuse the cases on it. Standard library only, so the scan can load it on
the presubmit's image.
"""

import hashlib

# The declared-intent note provisioning seeds after the first commit
# (GITOPS_INTENT_NOTE_* in scripts/provision_ci_pool_project.sh); the
# declared-intent cases (GITOPS_INTENT_NOTE_CASES) fail on a project whose
# repository lacks it.
GITOPS_INTENT_NOTE_PATH = "knowledge/notification-relay-no-pdb.md"
GITOPS_INTENT_NOTE_MESSAGE = "Declare seeded-intent's missing PodDisruptionBudget and NetworkPolicy, token-reader's mounted token, seeded-c's missing upgrade notifications, burst-ingest's headroom, declared-zonal-pool's single zone, and declared-orphan-snap-seeded's retention, as intended"
# The script's GITOPS_INTENT_NOTE_CONTENT, byte for byte, so the repair this
# verifier prints is the note provisioning seeds; a test pins the two copies
# to each other. The body read back is judged by the audit's parser, not
# compared to this text.
GITOPS_INTENT_NOTE_CONTENT = """---
type: decision
title: seeded-intent, seeded-token, seeded-c, seeded-headroom, the declared-zonal-pool of seeded-a and the declared-orphan-snap-seeded snapshot carry seven postures on purpose
declares:
  - check: no-pdb
    namespace: seeded-intent
    object: Deployment/notification-relay
  - check: netpol-missing
    namespace: seeded-intent
    object: Namespace/seeded-intent
  - check: default-sa-automount
    namespace: seeded-token
    object: Deployment/token-reader
  - check: no-notifications
    namespace: ""
    object: Cluster/seeded-c
  - check: overrequest
    namespace: seeded-headroom
    object: Deployment/burst-ingest
  - check: single-zone-nodepool
    namespace: ""
    object: NodePool/declared-zonal-pool
  - check: orphaned-snapshots
    namespace: ""
    object: Snapshot/declared-orphan-snap-seeded
---

`notification-relay` in `seeded-intent` runs two replicas with no PodDisruptionBudget by design:
it is a stateless relay whose clients retry, and a budget would only slow node drains. The
namespace carries no NetworkPolicy by design either: nothing in it accepts traffic. `token-reader`
in `seeded-token` runs on the default ServiceAccount of its namespace with the token mounted by
design: it reads the API server with that identity. Its neighbour `token-sidecar` is not declared.
`seeded-c` publishes no GKE upgrade notifications by design: this fleet learns about upgrades from
the weekly audit. `burst-ingest` in `seeded-headroom` requests far more memory than it uses by
design: it is sized for an ingest burst the measured week does not show. `declared-zonal-pool` on
`seeded-a` is an autoscaling pool kept in one zone by design: the cluster is single-zone, and the
pool stays empty until a zonal batch needs it. The `declared-orphan-snap-seeded` snapshot is kept
after its source disk was deleted by design: it is the retained copy of a retired disk, held until
an operator reviews it. The obtainability, compliance, upgrade readiness, waste, capacity and GCE
compute audits list the seven postures under Declared intent rather than as findings."""
# checkout-gateway's declaration, which provisioning seeds beside the note
# (GITOPS_WORKLOAD_MANIFEST_* in scripts/provision_ci_pool_project.sh). The
# remediation cases propose a PodDisruptionBudget beside it; in a repository
# without it the agent finds nothing to fix and refuses the pull request. The
# path is the one the fleet audit's workload_declarations reads a cluster off
# (clusters/<cluster>/...), the tie #2228's cases wait for. The script's
# content, byte for byte, pinned by a test as the note is.
GITOPS_WORKLOAD_MANIFEST_PATH = "clusters/seeded-a/seeded-reliability/checkout-gateway.yaml"
GITOPS_WORKLOAD_MANIFEST_MESSAGE = "Declare seeded-a's checkout-gateway Deployment in seeded-reliability"
GITOPS_WORKLOAD_MANIFEST_CONTENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-gateway
  namespace: seeded-reliability
spec:
  replicas: 2
  selector:
    matchLabels:
      app: checkout-gateway
  template:
    metadata:
      labels:
        app: checkout-gateway
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        seccompProfile:
          type: RuntimeDefault
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: checkout-gateway
      containers:
        - name: gateway
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
            limits:
              memory: 32Mi"""
# The two files provisioning seeds into a pool repository, by path. The seeded-fleet scan
# (hack/fleet-fixture-state.py, its `repository/<path>` subject) holds the
# file a pool repository carries to the one seeded here, by git blob sha: the
# seed commands (scripts/provision_ci_pool_project.sh, and the verifier's
# gitops_*_seed_command) write the content through `printf '%s\n'`, so the blob
# is git's hash of the content plus that newline (gitops_seeded_blob_sha).
# Forty registered projects carried the five-item note after the sixth
# declaration landed, and the cases on it graded `fail` because the scan read
# the node pools only (#5827); the blob is what tells the two notes apart.
GITOPS_SEEDED_FILES = {
    GITOPS_INTENT_NOTE_PATH: GITOPS_INTENT_NOTE_CONTENT,
    GITOPS_WORKLOAD_MANIFEST_PATH: GITOPS_WORKLOAD_MANIFEST_CONTENT,
}
SEED_COMMAND_TRAILER = "\n"
GIT_BLOB_HEADER = "blob {size}\0"


def gitops_seeded_bytes(path: str) -> bytes:
    """What the seed command writes at `path`, byte for byte: the content
    provisioning seeds plus the newline its `printf '%s\\n'` adds. KeyError
    for a path provisioning does not seed."""
    return (GITOPS_SEEDED_FILES[path] + SEED_COMMAND_TRAILER).encode("utf-8")


def gitops_seeded_blob_sha(path: str) -> str:
    """The git blob sha the repository carries once the seed command has run:
    SHA-1 over git's blob header and the file's bytes, which is what the
    contents API returns as `sha` and what the scan compares."""
    data = gitops_seeded_bytes(path)
    return hashlib.sha1(GIT_BLOB_HEADER.format(size=len(data)).encode("ascii") + data).hexdigest()
