#!/usr/bin/env bash
# Executes environment teardown and provisioning for release candidate deployment.
set -euo pipefail

./k8s-operator/scripts/teardown.sh --no-confirm
./k8s-operator/scripts/provision.sh --no-confirm
