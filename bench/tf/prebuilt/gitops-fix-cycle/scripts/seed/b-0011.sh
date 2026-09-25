# shellcheck shell=bash
# Seeded-condition assertions for b-0011, sourced by setup.sh after the
# Application reports Synced. wait_for, EXPECT, WAIT_TIMEOUT and kubectl's
# context come from the caller.
#
# Three ready, as the original: the branch's history syncs the healthy stack
# first and the broken head on top of it (setup.sh, run-branch.sh advance),
# so the rollout from 64Mi to 256Mi runs under the quota and leaves one old
# 64Mi pod behind next to two new ones, exactly as the original's in-place
# mutations do. Sync waves in the manifests (gating, then pricer, then the
# rest) keep pricer at 2/2, which the task's ready-floor safeguard requires
# from the first sample. See render-broken-base.sh.
EXPECT=256Mi wait_for "checkout memory request" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].resources.requests.memory}'
EXPECT=hashicorp/http-echo:1.0.0 wait_for "checkout image" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].image}'
EXPECT=4 wait_for "checkout spec.replicas" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.replicas}'
EXPECT=2 wait_for "pricer readyReplicas" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy pricer -o jsonpath='{.status.readyReplicas}'
EXPECT=3 wait_for "checkout readyReplicas (quota-bound)" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.status.readyReplicas}'
wait_for "quota-denied checkout pod event" "${WAIT_TIMEOUT}" \
  bash -c "kubectl -n payments get events --field-selector reason=FailedCreate -o name | head -1"
wait_for "metrics API returns pod data" "${WAIT_TIMEOUT}" \
  bash -c "kubectl top pods -n payments --no-headers 2>/dev/null | head -1"
# shellcheck disable=SC2034  # read by setup.sh, which sources this file
SEED_SUMMARY="payments: checkout 3/4 ready (quota-bound at 256Mi, one 64Mi pod left from the healthy rollout), pricer 2/2"
