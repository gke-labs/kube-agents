#!/usr/bin/env bash
#
# Confirm the tag a deploy just set actually reached the gateway's pod template.
#
# The agent redeploy's only change is `helm upgrade --set
# platformAgent.deployment.image.tag=<sha>` on the PlatformAgent CR, and the
# operator is free to ignore it. resolveAgentImage
# (k8s-operator/internal/controller/manifest_helpers.go) consults
# spec.deployment.tag only when spec.deployment.image is set *and* carries no
# tag or digest of its own, so there are two ways for the deploy's tag to stop
# meaning anything: a `kubectl patch` pinning a full reference, which then
# decides the image outright; or an unset spec.deployment.image, which sends
# the operator to its own default image and past the tag entirely.
#
# The check is tag-only. It does not compare the repository, because a mirrored
# install legitimately serves the release images from its own registry -- so a
# CR pointed at a foreign repository on this deploy's tag passes here. That is
# a cluster-write-access problem rather than one a CI read-back can police.
#
# Such a pin survives, which is the part worth spelling out because the chart
# does render spec.deployment.image on every upgrade
# (charts/kube-agents/templates/platform-agent-cr.yaml). It renders the bare
# repository, byte-identical release to release, and Helm computes a plain JSON
# merge patch between the previous and new rendered manifests for a custom
# resource rather than the three-way merge it does for built-in types -- a
# field the same in both is simply absent from the patch, so the live value is
# never touched. `kubectl get platformagent -o yaml --show-managed-fields` is
# how to confirm that on a live CR: had Helm's patch carried the field, Helm
# would own it, and a pin shows `kubectl-patch` owning it instead.
#
# Nothing downstream catches that. With the resolved image unchanged the
# operator writes no new pod template, so `kubectl rollout status` returns
# success against a ReplicaSet that was already complete, and the deploy is
# green while the cluster is untouched. Hence a read-back, and hence its
# position before the rollout gate.
#
# Every release image in the template is checked, not just the agent
# container's. The operator renders the agent container, the
# sandbox-credential-cleanup init container, the platform-agent-dashboard
# container (on by default), and the agent-api-auth sidecar. The credential
# proxy is a Deployment of its own, which this script is not pointed at and so
# does not cover, and neither is the shell sandbox StatefulSet. The proxy's
# image reference is derived from resolveAgentImage's output, so it normally
# moves with the agent's. It does not when the agent image is digest-pinned:
# that path falls back to spec.deployment.tag, so the agent freezes at its
# digest while the proxy rolls forward on every deploy. The operator logs that
# and carries on.
#
# The check is that no release image found is off the tag, not that a
# particular set of them is present. Which containers the operator renders
# depends on the CR -- the dashboard is switchable -- so an expected count
# would be wrong for some valid installs. A
# template that lost a container entirely therefore passes here; the rollout
# gate and the agent's own probes are what cover that.
#
# Which images count comes from images.json -- the first-party entries on the
# release tag policy -- matched on the trailing path segment of the
# repository. Matching on a registry prefix instead would be wrong for a
# mirrored install: `mirror_images.sh` writes <prefix>/<name> and
# thirdPartyImageRegistry falls back to imageRegistry, so a single-prefix
# mirror puts fluent-bit under the same prefix as the release images and a
# prefix rule would demand the deploy's tag of a third-party pin.
#
# Usage: confirm_agent_image.sh <namespace> <deployment> <tag>
#
# The budget is the operator's reconcile latency, not a rollout: it is
# event-driven off the CR write and normally lands in seconds. It deliberately
# sits outside the startupProbe < rollout gate < progressDeadlineSeconds
# ordering that tests/test_gateway_rollout_budgets.py pins, because it waits
# for the Deployment to be written at all -- before the clock those three share
# starts. 300s is the figure scripts/release/wait_for_gke_readiness.sh has used
# for this same read-back; one number for one wait, rather than a second one
# derived nowhere. It has to absorb an operator that is itself mid-rollout: the
# agent and controller redeploys sit in separate concurrency groups and can
# overlap.

set -euo pipefail

namespace="${1:?usage: confirm_agent_image.sh <namespace> <deployment> <tag>}"
deployment="${2:?usage: confirm_agent_image.sh <namespace> <deployment> <tag>}"
tag="${3:?usage: confirm_agent_image.sh <namespace> <deployment> <tag>}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGES_JSON="${IMAGES_JSON:-${REPO_ROOT}/images.json}"

timeout="${AGENT_IMAGE_CONFIRM_TIMEOUT:-300}"
interval="${AGENT_IMAGE_CONFIRM_INTERVAL:-5}"

# Reject a non-integer rather than letting it reach the arithmetic below. Under
# `set -u` bash treats `$((SECONDS + abc))` as an unbound variable and dies --
# and because the EXIT trap runs a successful `rm`, the trap's status is what
# the shell exits with, so the step goes green having read nothing. A guard
# whose own misconfiguration is a silent pass is the failure it exists to stop.
require_seconds() {
  case "$2" in
    '' | *[!0-9]*)
      echo "::error::${1} must be a whole number of seconds, got '${2}'."
      exit 1
      ;;
  esac
}
require_seconds AGENT_IMAGE_CONFIRM_TIMEOUT "$timeout"
require_seconds AGENT_IMAGE_CONFIRM_INTERVAL "$interval"

command -v jq >/dev/null 2>&1 || {
  echo "::error::jq is required to read ${IMAGES_JSON}."
  exit 1
}

# Ahead of the jq below, which under `set -e` would otherwise abort on jq's own
# exit status and never reach the guard beneath it -- leaving raw jq stderr and
# an exit code GitHub does not render as a step annotation.
[ -f "$IMAGES_JSON" ] || {
  echo "::error::${IMAGES_JSON} does not exist; cannot tell a release image from a third-party one."
  exit 1
}

# The images a release tags with its own version. Anything else in the pod
# template -- fluent-bit, and whatever a future template adds -- is pinned by
# its own upstream version and must never be expected to carry this tag.
release_names="$(jq -r '.images[] | select(.origin == "first-party" and .tagPolicy == "release") | .name' "$IMAGES_JSON" || true)"
[ -n "$release_names" ] || {
  echo "::error::No first-party release images found in ${IMAGES_JSON}."
  exit 1
}

# name=image, one per line, init containers first.
readonly JSONPATH='{range .spec.template.spec.initContainers[*]}{.name}={.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.name}={.image}{"\n"}{end}'

stderr_file="$(mktemp)"
# Carry the real status through the cleanup. A bare `rm` in an EXIT trap
# succeeds and becomes the shell's exit status, turning a fatal error into a
# green step.
cleanup() {
  local status=$?
  rm -f "$stderr_file"
  exit "$status"
}
trap cleanup EXIT

# Is this image one the release tags itself? Compares the repository's trailing
# path segment against the inventory's `name`. Those are two different things --
# mirror_images.sh writes <prefix>/<name> and says so in as many words, so it
# replaces the segment rather than preserving it. They coincide because
# hack/check-image-inventory.sh check 3c enforces it, failing any chart-rendered
# image under a mirror prefix whose segment names no inventory entry. That check
# is the guarantee this comparison rests on; the same `grep -qxF segment` shape
# is deliberate.
#
# Take the segment before stripping the tag, not after. A registry with a port
# -- registry.local:5000/kube-agents/platform-agent -- puts a colon in the
# first path segment, so stripping the last `:...` off the whole reference
# eats the repository path and leaves the registry host. A digest-pinned image
# on such a registry then stops looking like a release image at all, and the
# failure below reports finding none rather than the pin it exists to name.
is_release_image() {
  local segment="${1%@*}"
  segment="${segment##*/}"
  segment="${segment%%:*}"
  grep -qxF "$segment" <<<"$release_names"
}

# Sets matched and mismatched from a name=image listing.
inspect_template() {
  matched=0
  mismatched=""

  local name image
  while IFS='=' read -r name image; do
    [ -n "$image" ] || continue
    is_release_image "$image" || continue
    matched=$((matched + 1))
    case "$image" in
      *:"$tag") ;;
      *) mismatched="${mismatched}  ${name}: ${image}"$'\n' ;;
    esac
  done <<<"$1"
}

deadline=$((SECONDS + timeout))
while true; do
  # A stale or missing read is the expected first answer, not a failure: the
  # operator reconciles asynchronously and the deploy returns before it does.
  # kubectl's own error is kept rather than discarded, because an expired
  # credential and a slow operator look identical from here until the deadline.
  listing="$(kubectl get "deployment/${deployment}" -n "$namespace" -o jsonpath="$JSONPATH" 2>"$stderr_file" || true)"
  inspect_template "$listing"

  if [ "$matched" -gt 0 ] && [ -z "$mismatched" ]; then
    echo "Operator applied tag ${tag} to all ${matched} release image(s) in ${deployment}."
    exit 0
  fi

  if [ "$SECONDS" -ge "$deadline" ]; then
    if [ "$matched" -eq 0 ] && grep -qi 'not found' "$stderr_file"; then
      # Distinct from a template that is present but carries no release image:
      # the object may never arrive under this name. buildStatefulSet renders
      # the gateway as a StatefulSet when custom RWO storage meets multiple
      # replicas, and the CR diagnostics below would then indict a CR that is
      # fine.
      echo "::error::No Deployment ${deployment} in ${namespace} after ${timeout}s."
    elif [ "$matched" -eq 0 ]; then
      echo "::error::Found no first-party release image in ${deployment} after ${timeout}s. Read back:"
      echo "${listing:-  <nothing>}"
    else
      echo "::error::${deployment} is not running the tag ${tag} this deploy set. Still on:"
      printf '%s' "$mismatched"
    fi
    if [ -s "$stderr_file" ]; then
      echo "kubectl also reported:"
      sed 's/^/  /' "$stderr_file"
    fi
    # Three causes worth separating, and the CR tells them apart, because
    # resolveAgentImage reaches spec.deployment.tag only when
    # spec.deployment.image is set and unpinned:
    #
    #   pinned  -- a tag or digest on spec.deployment.image outranks the tag
    #   unset   -- no spec.deployment.image at all, so the operator uses its
    #              own default image and never reads the tag either
    #   neither -- the CR is fine and the operator is not: absent,
    #              crash-looping, or returning early, so the pod template was
    #              never re-rendered
    #
    # Which one it is decides the remedy printed below. Naming a pin
    # unconditionally sends the reader after a CR that is fine; blaming the
    # operator unconditionally does the reverse.
    #
    # Only the image and tag are read: DeploymentSpec also carries env with
    # literal values, and these logs are public.
    cr_listing="$(kubectl get platformagent -n "$namespace" \
      -o jsonpath='{range .items[*]}{.metadata.name}={.spec.deployment.image}={.spec.deployment.tag}{"\n"}{end}' 2>/dev/null || true)"
    pinned=""
    unset_image=""
    if [ -n "$cr_listing" ]; then
      echo "spec.deployment on the CR:"
      while IFS='=' read -r cr_name cr_image cr_tag; do
        [ -n "$cr_name" ] || continue
        echo "  ${cr_name}: image=${cr_image:-<unset>} tag=${cr_tag}"
        if [ -z "$cr_image" ]; then
          unset_image="yes"
          continue
        fi
        case "${cr_image##*/}" in
          *:* | *@*) pinned="yes" ;;
        esac
      done <<<"$cr_listing"
    fi
    echo "operator status:"
    kubectl get platformagent -n "$namespace" \
      -o jsonpath='{range .items[*]}  {.metadata.name}: {.status.phase}{"\n"}{range .status.conditions[*]}    {.type}={.status} {.reason}{"\n"}{end}{end}' || true
    if [ -n "$pinned" ]; then
      echo "An image above carries a tag or digest. That pin outranks spec.deployment.tag, the only field this deploy sets. Clear it with:"
      echo "  kubectl patch platformagent <name> -n ${namespace} --type=merge \\"
      echo "    -p '{\"spec\":{\"deployment\":{\"image\":\"<repository, no tag>\"}}}'"
    elif [ -n "$unset_image" ]; then
      echo "An image above is unset. The operator then serves its own default image and never reads spec.deployment.tag, so this deploy's tag was ignored before any pin could matter. Set the repository with:"
      echo "  kubectl patch platformagent <name> -n ${namespace} --type=merge \\"
      echo "    -p '{\"spec\":{\"deployment\":{\"image\":\"<repository, no tag>\"}}}'"
    else
      echo "No CR above pins or omits spec.deployment.image, so the CR is not the cause here. Read the status: an operator that is absent, crash-looping, or returning early leaves the pod template as it was."
    fi
    exit 1
  fi

  sleep "$interval"
done
