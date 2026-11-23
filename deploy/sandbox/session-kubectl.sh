#!/bin/sh
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# /usr/local/bin/kubectl in the worker image (a2a/Dockerfile.worker).
#
# With KUBE_PROXY_URL (a session pod with the cluster view), stock kubectl runs
# against <agent>-kube-proxy, in steps:
#   1. the kubeconfig context is ensured, as before;
#   2. on a pod where the admission can refuse nothing (all_fast below: not gVisor, and
#      a memory limit at or above the figure the image build wrote for the tier the
#      gateway names, so the tier's largest read fits), every argv execs kubectl directly.
#      Elsewhere, an argv the admission never charges (another verb, a named object, a
#      watch, --raw, a file, or a list of known namespaced kinds without -A), judged in
#      the shell without a fork, execs kubectl directly, as before. Any other argv, or
#      one the shell is unsure of, runs under the memory admission (kubectl-admit.py),
#      exec'd, so a signal sent to this pid reaches it. The admission in turn
#      execs kubectl, or, for a read it charges, runs kubectl as its child and
#      forwards TERM, INT and HUP to it; either way its exit code is kubectl's.
#      On a session pod it never refuses a command for being busy
#      (KUBECTL_ADMIT_ON_BUSY=run); it refuses only a read larger than the pod
#      could ever hold. No step follows kubectl in this script.
# The wrapper exits with kubectl's code.
#
# Without KUBE_PROXY_URL, a broker URL sends kubectl to the broker (a profile
# worker), and with neither kubectl runs as installed.

readonly REAL_KUBECTL=/usr/local/libexec/kubectl
readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec

# Whether kubectl-admit.py would pass this argv through uncharged (exit 0), read as its
# parse() reads it: a verb other than get or describe, a named object, --raw, a watch or a
# file. A get or describe listing only the known namespaced kinds below, with no -A, is
# uncharged too. Anything else, an unknown flag or kind included, exits 1: the admission
# decides. Case and parameter expansion only, so this costs no fork.
uncharged() {
  verb='' kind='' skip='' list='' rest=''
  for a do
    case $a in
    get | describe) rest=1 ;;
    esac
  done
  case $rest in
  '') return 0 ;; # no word can be the verb get or describe
  esac
  for a do
    case $skip in
    1)
      skip=''
      continue
      ;;
    esac
    case $a in
    -A | --all-namespaces* | -[!-]*A* | --watch=* | --watch-only=* | -h | --help* | -- | -) return 1 ;;
    --raw | -f | --filename | -k | --kustomize)
      list=no
      skip=1
      ;;
    --raw=* | --filename=* | --kustomize=* | -f?* | -k?* | -w | --watch | --watch-only) list=no ;;
    -[olnLcsv]) skip=1 ;;
    -[olnLcsv]?* | --*=*) ;;
    --namespace | --context | --kubeconfig | --cluster | --user | --server | --token | --as | --as-group | \
      --as-uid | --certificate-authority | --client-certificate | --client-key | --tls-server-name | \
      --username | --password | --request-timeout | --cache-dir | --kuberc | --v | --vmodule | \
      --log-flush-frequency | --profile | --profile-output | --selector | --field-selector | --output | \
      --template | --sort-by | --label-columns | --chunk-size | --subresource) skip=1 ;;
    --no-headers | --show-labels | --show-kind | --ignore-not-found | --show-managed-fields | \
      --insecure-skip-tls-verify | --warnings-as-errors | --match-server-version) ;;
    -*) return 1 ;;
    *)
      case $verb in
      '')
        verb=$a
        case $a in
        get | describe) ;;
        *) return 0 ;;
        esac
        ;;
      *)
        case $kind in
        '') kind=$a ;;
        *) return 0 ;; # a second word: named objects
        esac
        ;;
      esac
      ;;
    esac
  done
  case $list in
  no) return 0 ;;
  esac
  case $kind in
  *,*/*) return 1 ;;
  */*) return 0 ;; # kind/name
  esac
  rest=$kind
  while :; do
    case ${rest%%,*} in
    pods | pod | po | events | event | ev | services | service | svc | configmaps | configmap | cm | secrets | \
      secret | endpoints | ep | serviceaccounts | serviceaccount | sa | persistentvolumeclaims | \
      persistentvolumeclaim | pvc | deployments | deployment | deploy | statefulsets | statefulset | sts | \
      daemonsets | daemonset | ds | replicasets | replicaset | rs | jobs | job | cronjobs | cronjob | cj | \
      networkpolicies | networkpolicy | netpol | roles | role | rolebindings | rolebinding) ;;
    *) return 1 ;; # empty, all, cluster-scoped, a group suffix, a CRD or unknown
    esac
    case $rest in
    *,*) rest=${rest#*,} ;;
    *) return 0 ;;
    esac
  done
}

# Whether $1 is a decimal the guard would read, of at most 18 digits (so shell arithmetic, 64-bit
# in dash, cannot overflow) and no leading zero (no octal), and at least $2. Else false.
at_least() {
  case $1 in
  '' | 0* | *[!0-9]*) return 1 ;;
  esac
  [ ${#1} -le 18 ] && [ $(($1 >= $2)) -eq 1 ]
}

# Whether the admission could refuse no command on this pod, so every one may run kubectl
# directly. On a session pod (KUBECTL_ADMIT_ON_BUSY=run) its only refusal is a read larger than
# the guard's watermark less the shell's floor; the image build wrote, per cost tier, the smallest
# memory limit at which the tier's largest read fits (deploy/sandbox/kubectl_session_fastpath.py).
# True only when the tier is named and listed there, the kernel is not gVisor (whose watermark
# also falls per process), no limit or tier file overrides what the admission reads, and every
# limit the guard or the admission would take is a decimal at or above the tier's figure: the
# guard's variable when set, and cgroup v2 memory.max, which must be readable. Anything else, an
# unreadable file included, is false, and the paths below apply. Builtins only: no fork.
all_fast() {
  case ${KUBEAGENTS_KUBECTL_COST_TIER:-} in
  '' | [!a-z]* | *[!a-z0-9-]*) return 1 ;;
  esac
  [ ${#KUBEAGENTS_KUBECTL_COST_TIER} -le 32 ] || return 1
  [ -r /usr/local/share/kube-agents/kubectl-session-fastpath ] || return 1
  [ -e /tmp/kube-agents-session/kubectl-cost-tier ] && return 1
  [ -e /tmp/kube-agents-session/memory-limit-bytes ] && return 1
  f_min=''
  while read -r f_tier f_bytes; do
    case $f_tier in
    "$KUBEAGENTS_KUBECTL_COST_TIER")
      f_min=$f_bytes
      break
      ;;
    esac
  done </usr/local/share/kube-agents/kubectl-session-fastpath
  case $f_min in
  '' | 0* | *[!0-9]*) return 1 ;;
  esac
  [ ${#f_min} -le 18 ] || return 1
  f_release=''
  read -r f_release 2>/dev/null </proc/sys/kernel/osrelease
  case $f_release in
  '' | *[Gg][Vv][Ii][Ss][Oo][Rr]*) return 1 ;; # unreadable, or the guard's GVISOR_RELEASE_MARK
  esac
  case ${SANDBOX_MEMORY_LIMIT_BYTES:-} in
  '') ;;
  *) at_least "$SANDBOX_MEMORY_LIMIT_BYTES" "$f_min" || return 1 ;;
  esac
  f_max=''
  read -r f_max 2>/dev/null </sys/fs/cgroup/memory.max
  at_least "$f_max" "$f_min"
}

if [ -n "${KUBE_PROXY_URL:-}" ]; then
  case " $* " in
  *" --context"*)
    /usr/local/libexec/kubectl-context-registered "$@" 2>/dev/null || PYTHONDONTWRITEBYTECODE=1 python3 -I "$CLIENT" --ensure-kubectl-context "$@" 2>/dev/null || true
    ;;
  *)
    if [ ! -f "${HOME}/.kube/config" ]; then
      PYTHONDONTWRITEBYTECODE=1 python3 -I "$CLIENT" --ensure-kubectl-context "$@" 2>/dev/null || true
    fi
    ;;
  esac
  if all_fast || uncharged "$@"; then
    exec "$REAL_KUBECTL" "$@"
  fi
  export KUBECTL_ADMIT_RUN_DIR=/tmp/kube-agents-session KUBECTL_ADMIT_GUARD_PATH=/usr/local/libexec/shell-memory-guard KUBECTL_ADMIT_ON_BUSY=run
  exec python3 -I /usr/local/libexec/kubectl-admit "$@"
fi
if [ -n "${CREDENTIAL_PROXY_URL:-}" ] || [ -n "${CREDENTIAL_PROXY_EXEC_URL:-}" ]; then
  exec "$CLIENT" kubectl "$@"
fi
exec "$REAL_KUBECTL" "$@"
