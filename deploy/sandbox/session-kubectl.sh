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
#   2. kubectl runs under the memory admission (deploy/sandbox/kubectl-admit.py),
#      exec'd, so a signal sent to this pid reaches it. The admission in turn
#      execs kubectl, or, for a read it charges, runs kubectl as its child and
#      forwards TERM, INT and HUP to it; either way its exit code is kubectl's.
#      On a session pod it never refuses a command for being busy
#      (KUBECTL_ADMIT_ON_BUSY=run); it refuses only a read larger than the pod
#      could ever hold. A step that needs kubectl's exit code afterwards runs
#      the admission as a child instead, only where that step can fire.
# The wrapper exits with kubectl's code.
#
# Without KUBE_PROXY_URL, a broker URL sends kubectl to the broker (a profile
# worker), and with neither kubectl runs as installed.

readonly REAL_KUBECTL=/usr/local/libexec/kubectl
readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec

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
  export KUBECTL_ADMIT_RUN_DIR=/tmp/kube-agents-session KUBECTL_ADMIT_GUARD_PATH=/usr/local/libexec/shell-memory-guard KUBECTL_ADMIT_ON_BUSY=run
  exec python3 -I /usr/local/libexec/kubectl-admit "$@"
fi
if [ -n "${CREDENTIAL_PROXY_URL:-}" ] || [ -n "${CREDENTIAL_PROXY_EXEC_URL:-}" ]; then
  exec "$CLIENT" kubectl "$@"
fi
exec "$REAL_KUBECTL" "$@"
