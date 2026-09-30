#!/usr/bin/env bash
#
# Render a task's "broken base" directory for the leaderboard GitOps repo.
#
# manifests/<task>/ is the HEALTHY baseline copied from the task's devops-bench
# stack. In the GitOps cycle the repo has to describe the BROKEN state, because
# Argo CD syncs whatever the repo says and the agent's PR is what moves it back.
# This script is the single place a task's broken state is encoded, so the repo
# content is derived from manifests/ rather than a hand-edited second copy.
#
# Per task:
#
# b-0011. The original setup.sh applies the manifests and then makes three live
#   mutations to payments/checkout (memory request 64Mi -> 256Mi, image :1.0 ->
#   :1.0.0, replicas 2 -> 4); those are applied here. The task's clue is that
#   history: the request was inflated in an earlier revision and this morning's
#   build update (image, scale) came on top of it. A single sync of the broken
#   state has no history, so the branch is built in three stages, each a commit
#   (run-branch.sh): `healthy` (the manifests as shipped), `inflated` (memory
#   256Mi), `broken` (all three mutations). Argo syncs healthy first and then
#   the broken head, so the cluster keeps the old 64Mi ReplicaSet exactly as
#   the original's in-place rollout does. Ordering: setup.sh deploys
#   everything healthy first and only then inflates checkout, so pricer already
#   holds its 128Mi of the 832Mi payments quota when checkout's 256Mi pods
#   arrive. A flat apply of the broken state races the two and pricer can end
#   up at 1/2 ready, which trips the task's own ready-floor safeguard before the
#   agent has done anything (measured 2026-09-09 on a scratch GKE cluster). Argo
#   CD sync waves reproduce the original order: gating objects (namespaces,
#   quota, netpols) in wave -2, pricer in wave -1, everything else in the
#   default wave 0. Result: pricer 2/2 and, once the staged history has synced
#   healthy before broken, checkout 3/4 ready with the rest quota-blocked (2/4
#   from a branch cut at the broken base alone), quota 704Mi/832Mi. The edge/gateway Ingress is excluded from
#   Argo health: it has no ingress class and a ClusterIP backend, so GKE never
#   programs it, and Argo's Ingress health would pin the Application at
#   Progressing and starve the harness's completion signal.
#
# b-0022b. The three faults are already declarative in the manifests (shelfview
#   at 0 replicas, the price-refresh CronJob suspended, search-api's readiness
#   probe on port 9099 instead of 8085); the original setup.sh applies them as
#   they are. The broken base is therefore the manifests unchanged, with the
#   gating objects (the storefront Namespace) in wave -2 so the workloads are
#   admitted into an existing namespace.
#
# The original scripts run envsubst '${CLUSTER_NAME}' over the manifests;
# neither task's files reference CLUSTER_NAME today, so nothing is substituted
# here. If that changes, add it.
#
# Usage: render-broken-base.sh <task> <stack manifests dir for the task> <output dir> [stage]
#   stage (b-0011 only): healthy | inflated | broken (default). b-0022b has one
#   stage, broken.
set -euo pipefail

TASK="${1:?usage: $0 <task> <manifests dir> <output dir> [stage]}"
SRC="${2:?usage: $0 <task> <manifests dir> <output dir> [stage]}"
OUT="${3:?usage: $0 <task> <manifests dir> <output dir> [stage]}"
STAGE="${4:-broken}"
mkdir -p "${OUT}"

python3 - "${TASK}" "${SRC}" "${OUT}" "${STAGE}" <<'PY'
import sys
import yaml

task, src, out, stage = sys.argv[1:5]
STAGES = {"b-0011": ("healthy", "inflated", "broken"), "b-0022b": ("broken",)}
if stage not in STAGES.get(task, ()):
    sys.exit(f"render-broken-base: task {task!r} has no stage {stage!r} (stages: {STAGES.get(task)})")
WAVE = "argocd.argoproj.io/sync-wave"
IGNORE_HEALTH = "argocd.argoproj.io/ignore-healthcheck"


def load(name):
    return [d for d in yaml.safe_load_all(open(f"{src}/{name}")) if d]


def dump(name, docs):
    with open(f"{out}/{name}", "w") as f:
        yaml.safe_dump_all(docs, f, sort_keys=False, default_flow_style=False)


def annotate(doc, key, value):
    doc.setdefault("metadata", {}).setdefault("annotations", {})[key] = value


# 00-gating: namespaces (and, for b-0011, network policies and the quota).
# Wave -2 so they exist before any workload is admitted.
gating = load("00-gating.yaml")
for doc in gating:
    annotate(doc, WAVE, "-2")
dump("00-gating.yaml", gating)

workloads = load("10-workloads.yaml")

if task == "b-0011":
    for doc in workloads:
        meta = doc.get("metadata", {})
        if meta.get("namespace") == "payments" and meta.get("name") == "pricer":
            annotate(doc, WAVE, "-1")
        if doc.get("kind") == "Ingress":
            annotate(doc, IGNORE_HEALTH, "true")
    for doc in workloads:
        meta = doc.get("metadata", {})
        if doc.get("kind") == "Deployment" and meta.get("name") == "checkout" and meta.get("namespace") == "payments":
            (web,) = [c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "web"]
            if stage in ("inflated", "broken"):
                web["resources"]["requests"]["memory"] = "256Mi"
            if stage == "broken":
                doc["spec"]["replicas"] = 4
                web["image"] = "hashicorp/http-echo:1.0.0"
            break
    else:
        sys.exit("render-broken-base: payments/checkout Deployment not found")
elif task == "b-0022b":
    # Already broken as shipped; sanity-check the three faults are present so a
    # refreshed healthy copy cannot silently render a fixed base.
    faults = {"shelfview": False, "price-refresh": False, "search-api": False}
    for doc in workloads:
        meta = doc.get("metadata", {})
        if doc.get("kind") == "Deployment" and meta.get("name") == "shelfview":
            faults["shelfview"] = doc["spec"].get("replicas") == 0
        if doc.get("kind") == "CronJob" and meta.get("name") == "price-refresh":
            faults["price-refresh"] = doc["spec"].get("suspend") is True
        if doc.get("kind") == "Deployment" and meta.get("name") == "search-api":
            (web,) = [c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "web"]
            faults["search-api"] = web["readinessProbe"]["httpGet"]["port"] == 9099
    missing = [k for k, v in faults.items() if not v]
    if missing:
        sys.exit(f"render-broken-base: b-0022b manifests do not carry the seeded fault(s): {missing}")
else:
    sys.exit(f"render-broken-base: no render rule for task {task!r}")

dump("10-workloads.yaml", workloads)
PY

cat > "${OUT}/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - 00-gating.yaml
  - 10-workloads.yaml
YAML

echo "rendered ${TASK} ${STAGE} stage into ${OUT}"
