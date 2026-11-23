#!/usr/bin/env bash
# Time what the kubectl wrapper and its admission (deploy/sandbox/kubectl-admit.py) add to a
# kubectl call, and print p50 and p95 per case. Run it inside the shell sandbox, where the
# wrapper runs every command through the admission, or in an A2A session pod under the cluster
# view (KUBE_PROXY_URL set). On a session pod the wrapper skips the admission for a command it
# never charges, and for every command when the tier's largest read fits the pod's memory
# limit (every default install). To time the admission itself there, run in a pod where the
# tier does not fit (KUBEAGENTS_KUBECTL_COST_TIER=fleet on a 2Gi session pod, for example), or
# call `python3 -I /usr/local/libexec/kubectl-admit` directly, with the three KUBECTL_ADMIT_*
# settings session-kubectl.sh exports.
# For example:
#
#   kubectl exec -n <ns> <session-pod> -- bash -s -- -n 50 < scripts/dev/measure_kubectl_admission_overhead.sh
#
# Usage:
#   measure_kubectl_admission_overhead.sh [-n RUNS] [-s NAMESPACE] [-c CONTEXT]
#
# Cases, each RUNS times after one untimed warm-up call:
#   direct-version    /usr/local/libexec/kubectl version --client       (no wrapper, no admission)
#   wrapper-version   kubectl version --client       (session pod: the wrapper's shell check, then
#                     kubectl; sandbox: the admission's pass-through)
#   direct-get-one    /usr/local/libexec/kubectl get ns NAMESPACE -o name
#   wrapper-get-one   kubectl get ns NAMESPACE -o name   (session pod: as wrapper-version; sandbox:
#                     slotted, uncharged: a slot, then exec)
#   direct-get-all    /usr/local/libexec/kubectl get namespaces -o name
#   wrapper-get-all   kubectl get namespaces -o name     (session pod where the tier does not fit,
#                     and sandbox: charged: a slot, a reservation, a child; where it fits, as
#                     wrapper-version)
#
# The wrapper's overhead is a wrapper case less its direct case. The get cases reach the
# API server through the kube-proxy, so their spread is mostly the network; run enough of
# them for the p50s to settle. Nothing here changes cluster state. Each call's output is
# discarded; a call that fails stops the script with its exit status.
set -euo pipefail

readonly DEFAULT_RUNS=20
readonly DEFAULT_NAMESPACE="default"
# The image's paths; the environment may point them elsewhere (a dry run with stubs).
readonly KUBECTL_DIRECT="${KUBECTL_DIRECT:-/usr/local/libexec/kubectl}"
readonly KUBECTL_WRAPPER="${KUBECTL_WRAPPER:-/usr/local/bin/kubectl}"

usage() {
  echo "usage: measure_kubectl_admission_overhead.sh [-n RUNS] [-s NAMESPACE] [-c CONTEXT]" >&2
  exit 2
}

runs="$DEFAULT_RUNS"
namespace="$DEFAULT_NAMESPACE"
context=""
while getopts "n:s:c:h" opt; do
  case "$opt" in
    n) runs="$OPTARG" ;;
    s) namespace="$OPTARG" ;;
    c) context="$OPTARG" ;;
    *) usage ;;
  esac
done
case "$runs" in
  '' | *[!0-9]* | 0) echo "RUNS must be a positive integer" >&2; exit 2 ;;
esac

context_args=()
if [ -n "$context" ]; then
  context_args=(--context "$context")
fi

# python3 is in the worker and sandbox images; it times each call with a monotonic clock
# and reports the percentiles, so the shell needs no nanosecond date.
exec python3 -I - "$runs" "$KUBECTL_DIRECT" "$KUBECTL_WRAPPER" "$namespace" "${context_args[@]+"${context_args[@]}"}" <<'PY'
import statistics
import subprocess
import sys
import time

runs = int(sys.argv[1])
direct, wrapper, namespace = sys.argv[2], sys.argv[3], sys.argv[4]
context = sys.argv[5:]
cases = [
    ("direct-version", [direct, "version", "--client"]),
    ("wrapper-version", [wrapper, "version", "--client"]),
    ("direct-get-one", [direct, *context, "get", "ns", namespace, "-o", "name"]),
    ("wrapper-get-one", [wrapper, *context, "get", "ns", namespace, "-o", "name"]),
    ("direct-get-all", [direct, *context, "get", "namespaces", "-o", "name"]),
    ("wrapper-get-all", [wrapper, *context, "get", "namespaces", "-o", "name"]),
]


def once(argv):
    start = time.monotonic()
    rc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL).returncode
    elapsed = time.monotonic() - start
    if rc != 0:
        sys.exit(f"{' '.join(argv)} exited {rc}")
    return elapsed * 1000


def percentile(sorted_ms, p):
    # Nearest rank: the smallest sample at or above p percent of them.
    return sorted_ms[max(0, -(-len(sorted_ms) * p // 100) - 1)]


print(f"{'case':<16} {'runs':>5} {'p50 ms':>8} {'p95 ms':>8} {'mean ms':>8}")
for name, argv in cases:
    once(argv)  # warm-up: page cache, kubeconfig registration, the first TLS session
    samples = sorted(once(argv) for _ in range(runs))
    print(f"{name:<16} {runs:>5} {percentile(samples, 50):>8.1f} {percentile(samples, 95):>8.1f} "
          f"{statistics.fmean(samples):>8.1f}")
PY
