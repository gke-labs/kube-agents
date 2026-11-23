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
#   2. kubectl runs as a child, not exec'd, so step 3 sees its exit code. It
#      runs in the foreground: a background job's stdin would be /dev/null,
#      and `kubectl apply -f -` reads stdin. A stop reaches it anyway: the
#      worker adapter signals the harness's whole process group;
#   3. after a non-zero exit, when the gateway has said kube-proxy is read-only
#      (KUBE_PROXY_MODE) and the adapter has named a record file, the broker
#      client records a write attempt if the argv tried a change
#      (credential_proxy_client.record_kubectl_write_attempt). It reads the
#      argv and the exit code, never kubectl's output. A call that exits 0, or
#      runs with no read-only mode, starts no Python here.
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
  "$REAL_KUBECTL" "$@"
  rc=$?
  if [ "$rc" -ne 0 ] && [ -n "${CREDENTIAL_PROXY_REFUSAL_FILE:-}" ]; then
    case "${KUBE_PROXY_MODE:-}" in
    read-only | read-only-dry-run)
      PYTHONDONTWRITEBYTECODE=1 python3 -I "$CLIENT" --record-kubectl-write-attempt "$rc" "$@" </dev/null || true
      ;;
    esac
  fi
  exit "$rc"
fi
if [ -n "${CREDENTIAL_PROXY_URL:-}" ] || [ -n "${CREDENTIAL_PROXY_EXEC_URL:-}" ]; then
  exec "$CLIENT" kubectl "$@"
fi
exec "$REAL_KUBECTL" "$@"
