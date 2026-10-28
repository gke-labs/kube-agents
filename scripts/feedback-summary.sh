#!/usr/bin/env bash
# Print the copy-pasteable usage and feedback summary of one kube-agents install.
# Writes nothing: kubectl get on the PlatformAgent resource, plus two kubectl
# exec calls into the gateway pod (curl on its loopback and on the inference
# gateway Service) for the optional task and token figures; those need
# pods/exec, and --no-exec skips them. Every flag is passed through to
# agents/platform/scripts/feedback_summary.py (--namespace, --name, --context,
# --kubeconfig, --no-exec). Exit 3 means the block named an identifier of the
# install and was not printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATOR="$SCRIPT_DIR/../agents/platform/scripts/feedback_summary.py"

exec python3 "$GENERATOR" "$@"
