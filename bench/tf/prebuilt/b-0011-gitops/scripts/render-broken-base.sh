#!/usr/bin/env bash
#
# Render the b-0011 "broken base" directory for the leaderboard GitOps repo.
#
# The stack's manifests/ are the HEALTHY baseline. The original b-0011 setup.sh
# applies them and then makes three live mutations to payments/checkout (memory
# request 64Mi -> 256Mi, image :1.0 -> :1.0.0, replicas 2 -> 4). In the GitOps
# cycle the repo has to describe that post-mutation state, because Argo CD syncs
# whatever the repo says and the agent's PR is what moves it back. This script
# is the single place those mutations are encoded, so the repo content is
# derived from manifests/ rather than a hand-edited second copy.
#
# Ordering. setup.sh deploys everything healthy first and only then inflates
# checkout, so pricer already holds its 128Mi of the 832Mi payments quota when
# checkout's 256Mi pods arrive. A flat apply of the broken state races the two
# and pricer can end up at 1/2 ready, which trips the task's own ready-floor
# safeguard before the agent has done anything (measured 2026-09-09 on a
# scratch GKE cluster). Argo CD sync waves reproduce the original order:
# gating objects (namespaces, quota, netpols) in wave -2, pricer in wave -1,
# everything else in the default wave 0. Result: pricer 2/2, checkout 2/4
# ready with the rest quota-blocked, quota 640Mi/832Mi.
#
# setup.sh also runs envsubst '${CLUSTER_NAME}' over the manifests; neither
# file references CLUSTER_NAME today, so no substitution happens here. If that
# changes, add it.
#
# Usage: render-broken-base.sh <stack manifests dir> <output dir>
set -euo pipefail

SRC="${1:?usage: $0 <manifests dir> <output dir>}"
OUT="${2:?usage: $0 <manifests dir> <output dir>}"
mkdir -p "${OUT}"

python3 - "${SRC}" "${OUT}" <<'PY'
import sys
import yaml

src, out = sys.argv[1:3]
WAVE = "argocd.argoproj.io/sync-wave"


def load(name):
    return [d for d in yaml.safe_load_all(open(f"{src}/{name}")) if d]


def dump(name, docs):
    with open(f"{out}/{name}", "w") as f:
        yaml.safe_dump_all(docs, f, sort_keys=False, default_flow_style=False)


# 00-gating: namespaces, network policies, quota. Wave -2 so they exist before
# any workload is admitted.
gating = load("00-gating.yaml")
for doc in gating:
    doc.setdefault("metadata", {}).setdefault("annotations", {})[WAVE] = "-2"
dump("00-gating.yaml", gating)

# 10-workloads: pricer first (wave -1), then the checkout mutations.
workloads = load("10-workloads.yaml")
for doc in workloads:
    meta = doc.get("metadata", {})
    if meta.get("namespace") == "payments" and meta.get("name") == "pricer":
        meta.setdefault("annotations", {})[WAVE] = "-1"
    # edge/gateway Ingress has no ingress class and a ClusterIP backend, so on
    # GKE nothing programs it and it never gets a load-balancer address. Argo's
    # built-in Ingress health stays Progressing until it does, which would pin
    # the whole Application at Progressing and starve the harness's "Synced and
    # Healthy" completion signal. It is scenery, not graded state: exclude it
    # from health (measured 2026-09-09: app Progressing with payments at 4/4).
    if doc.get("kind") == "Ingress":
        meta.setdefault("annotations", {})["argocd.argoproj.io/ignore-healthcheck"] = "true"

for doc in workloads:
    meta = doc.get("metadata", {})
    if doc.get("kind") == "Deployment" and meta.get("name") == "checkout" and meta.get("namespace") == "payments":
        doc["spec"]["replicas"] = 4
        (web,) = [c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "web"]
        web["resources"]["requests"]["memory"] = "256Mi"
        web["image"] = "hashicorp/http-echo:1.0.0"
        break
else:
    sys.exit("render-broken-base: payments/checkout Deployment not found")
dump("10-workloads.yaml", workloads)
PY

cat > "${OUT}/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - 00-gating.yaml
  - 10-workloads.yaml
YAML

echo "rendered b-0011 broken base into ${OUT}"
