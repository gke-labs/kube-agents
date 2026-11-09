#!/usr/bin/env bash
# ==============================================================================
# Bench plant sweep for one host cluster
# ==============================================================================
# Removes what a bench case's stack planted on the host cluster and a killed
# run left behind: the objects a stack labels managed-by=kube-agents-bench
# (bench/README.md, "Certification suite", fixture rule 5) and the node pools
# whose GCE resource labels carry the same pair. A stack's own destroy is the
# first remover; this is the backstop hack/ci-teardown.sh runs at every lease
# start and end, since a run killed between apply and destroy runs no destroy,
# and a leftover ComputeClass or webhook is read by the next lease's audits
# while a leftover pool bills.
#
# Dry run by default: every leg lists the names it matches and prints what it
# would delete. --delete acts. Each leg lists first and deletes by name, so the
# dry-run lines are the delete's exact targets. The admission kinds go first
# (a planted hook must not refuse the deletes after it), the readiness rules
# next (the controller untaints before the pools go), the compute classes
# before the pools (a class with nodePoolAutoCreation could provision a new
# pool after the sweep), the pools last and --async: a synchronous pool delete
# is minutes, and the next sweep lists what is still going. Kubernetes deletes
# are --wait=false for the same reason: a class held by a finalizer is listed
# again next time instead of hanging this run.
#
# The script authenticates itself into a private KUBECONFIG, so a hand run
# never acts on whatever context the shell happens to hold. It never deletes
# the host's default-pool, and it never selects anything without the label:
# the chart's objects carry app.kubernetes.io/part-of=kube-agents, the seeded
# fleet's carry managed-by=kube-agents-seeded-fleet, and GKE's own carry
# neither. tests/test_ci_sweep_bench_plants.py runs it against stub kubectl
# and gcloud.
#
# Exit 0 when every leg listed and every matched object was deleted (or would
# be); 1 when a list or a delete failed, after every leg has run; 2 on usage.
# ==============================================================================

set -uo pipefail

readonly PLANT_SELECTOR="managed-by=kube-agents-bench"
# Cluster-scoped kinds a stack can plant, in delete order (header comment).
# nodereadinessrules is the CRD of kubernetes-sigs/node-readiness-controller;
# GKE does not ship it, so on a stock host the group is absent, which counts
# as absent, not failed, the same way hack/ci-teardown.sh counts VAP on an
# older cluster.
readonly PLANT_KINDS=(
  validatingwebhookconfigurations.admissionregistration.k8s.io
  mutatingwebhookconfigurations.admissionregistration.k8s.io
  nodereadinessrules.readiness.node.x-k8s.io
  computeclasses.cloud.google.com
)
# The gcloud filter is client-side; a pool without the key evaluates false.
# STOPPING is a pool already deleting: a second delete answers 409.
readonly POOL_LIST_FILTER="config.resourceLabels.managed-by=kube-agents-bench AND status!=STOPPING"
readonly POOL_NAME_NEVER_DELETED="default-pool"
readonly ABSENT_KIND_RE="doesn't have a resource type"
readonly KUBECTL_REQUEST_TIMEOUT="${BENCH_SWEEP_KUBECTL_REQUEST_TIMEOUT:-30s}"
readonly EXIT_FAILED=1
readonly EXIT_USAGE=2

usage() {
  cat >&2 <<USAGE
usage: $0 --project <id> --cluster <name> --location <region-or-zone> [--delete]
Lists the bench plants on the cluster (dry run); --delete removes them.
USAGE
  exit "${EXIT_USAGE}"
}

# Sourced for gke_dns_endpoint_flag, so the get-credentials below picks the
# same control-plane endpoint every other CI get-credentials uses.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/installer/gke_dns_endpoint.sh
source "${SCRIPT_DIR}/../scripts/installer/gke_dns_endpoint.sh"

PROJECT=""
CLUSTER=""
LOCATION=""
ACT=0
# A value flag given last with nothing after it is a usage error, not a loop:
# `shift 2` on one remaining argument fails and leaves it in place. Checked
# in the main shell, since usage exits and a subshell's exit would not.
need_value() { # <flag>
  echo "ERROR: $1 needs a value." >&2
  usage
}
while [ $# -gt 0 ]; do
  case "$1" in
    --project) [ $# -ge 2 ] || need_value "$1"; PROJECT="$2"; shift 2 ;;
    --cluster) [ $# -ge 2 ] || need_value "$1"; CLUSTER="$2"; shift 2 ;;
    --location) [ $# -ge 2 ] || need_value "$1"; LOCATION="$2"; shift 2 ;;
    --delete) ACT=1; shift ;;
    -h | --help) usage ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage ;;
  esac
done
# Required and non-empty before any call: an empty --project or --cluster
# would otherwise fall through to gcloud's configured defaults.
if [ -z "${PROJECT}" ] || [ -z "${CLUSTER}" ] || [ -z "${LOCATION}" ]; then
  echo "ERROR: --project, --cluster and --location are required." >&2
  usage
fi

if [ "${ACT}" -eq 1 ]; then
  VERB="deleted"
  MODE="delete"
else
  VERB="would delete"
  MODE="dry run"
fi

KUBECONFIG_DIR="$(mktemp -d)"
trap 'rm -rf "${KUBECONFIG_DIR}"' EXIT
KUBECONFIG="${KUBECONFIG_DIR}/config"
export KUBECONFIG
gke_dns_endpoint_flag "${CLUSTER}" "${LOCATION}" "${PROJECT}"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
if ! gcloud container clusters get-credentials "${CLUSTER}" \
  --location "${LOCATION}" --project "${PROJECT}" --quiet $GKE_DNS_ENDPOINT_FLAG; then
  echo "ERROR: could not get credentials for ${CLUSTER} (${LOCATION}, ${PROJECT}); nothing swept." >&2
  exit "${EXIT_FAILED}"
fi

echo "=== Bench plant sweep (${MODE}) on ${CLUSTER} (${LOCATION}, ${PROJECT}): selector ${PLANT_SELECTOR} ==="
MATCHED=0
FAILED=0
ABSENT=0

sweep_kind() { # <kind>
  local kind="$1" names err name
  err="$(mktemp)"
  if ! names="$(kubectl get "${kind}" -l "${PLANT_SELECTOR}" -o name \
    --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" 2>"${err}")"; then
    if grep -q "${ABSENT_KIND_RE}" "${err}"; then
      echo "${kind}: not served on this cluster"
      ABSENT=$((ABSENT + 1))
    else
      echo "✗ ${kind}: list failed: $(tr '\n' ' ' <"${err}")"
      FAILED=$((FAILED + 1))
    fi
    rm -f "${err}"
    return
  fi
  rm -f "${err}"
  while read -r name; do
    [ -n "${name}" ] || continue
    MATCHED=$((MATCHED + 1))
    if [ "${ACT}" -eq 0 ]; then
      echo "${VERB} ${name}"
    elif kubectl delete "${name}" --ignore-not-found --wait=false \
      --request-timeout="${KUBECTL_REQUEST_TIMEOUT}"; then
      echo "${VERB} ${name}"
    else
      echo "✗ delete failed: ${name}"
      FAILED=$((FAILED + 1))
    fi
  done <<<"${names}"
}

for kind in "${PLANT_KINDS[@]}"; do
  sweep_kind "${kind}"
done

# Node pools: the same pair as a GCE resource label on the pool
# (`--labels managed-by=kube-agents-bench` at create). Empty output is zero
# pools; a list that fails is a failed leg.
if POOLS="$(gcloud container node-pools list --cluster "${CLUSTER}" \
  --location "${LOCATION}" --project "${PROJECT}" \
  --filter="${POOL_LIST_FILTER}" --format="value(name)" </dev/null)"; then
  while read -r pool; do
    [ -n "${pool}" ] || continue
    if [ "${pool}" = "${POOL_NAME_NEVER_DELETED}" ]; then
      echo "✗ refusing to delete ${POOL_NAME_NEVER_DELETED} although it carries the label; remove the label or the pool by hand"
      FAILED=$((FAILED + 1))
      continue
    fi
    MATCHED=$((MATCHED + 1))
    if [ "${ACT}" -eq 0 ]; then
      echo "${VERB} node pool ${pool}"
    elif gcloud container node-pools delete "${pool}" --cluster "${CLUSTER}" \
      --location "${LOCATION}" --project "${PROJECT}" --quiet --async </dev/null; then
      echo "${VERB} node pool ${pool} (delete started)"
    else
      echo "✗ delete failed: node pool ${pool}"
      FAILED=$((FAILED + 1))
    fi
  done <<<"${POOLS}"
else
  echo "✗ node pools: list failed"
  FAILED=$((FAILED + 1))
fi

echo "=== Bench plant sweep (${MODE}): ${MATCHED} matched, ${FAILED} failed, ${ABSENT} kinds not served ==="
if [ "${FAILED}" -ne 0 ]; then
  exit "${EXIT_FAILED}"
fi
exit 0
