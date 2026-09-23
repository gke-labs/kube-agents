#!/usr/bin/env bash
# ==============================================================================
# Choosing between a cluster's IP and DNS control-plane endpoints
# ==============================================================================
# `gcloud container clusters get-credentials` writes the IP endpoint into the
# kubeconfig unless --dns-endpoint is passed. A cluster we cannot route an IP to
# -- no public endpoint, and we are outside its VPC -- needs the DNS endpoint
# (*.gke.goog) instead.
#
# The flag is not safe to pass blind. gcloud rejects it on a cluster with no DNS
# endpoint configured, and on one whose dnsEndpointConfig.allowExternalTraffic is
# off, so an always-on flag would break clusters that work today.
#
# It is equally unsafe to pass it and read the exit code. For a caller Google
# recognises as internal, gcloud downgrades the allowExternalTraffic rejection to
# a warning and writes a kubeconfig naming the DNS endpoint anyway; the command
# exits 0 and every later kubectl gets HTTP 403 back from Google's frontend.
# Probing by attempting the flag therefore reports success exactly where it is
# most wrong, so this reads the cluster's configuration up front instead.
#
# This file is deliberately dependency-free -- no colours, no state file, no
# print_* helpers -- because it is sourced by three shell libraries that do not
# share anything else: scripts/installer/common.sh, hack/ci-env.sh, and
# scripts/release/common.sh.
#
# The Python equivalent, used by the agent at runtime, is
# agents/platform/scripts/gke_endpoint.py. Keep the two predicates in step.

# Empty until asked, then 1 or 0. gcloud is slow to start and cannot grow a flag
# mid-run. The agent image installs an unpinned google-cloud-cli so the answer is
# always yes there, but these scripts also run on an operator's workstation where
# gcloud is whatever they happen to have -- and an unrecognised flag is a hard
# argparse failure, which would turn "we could have used a better endpoint" into
# "the install stopped".
_GKE_DNS_ENDPOINT_SUPPORTED=""

gke_supports_dns_endpoint() {
  if [ -z "$_GKE_DNS_ENDPOINT_SUPPORTED" ]; then
    if gcloud container clusters get-credentials --help 2>/dev/null | grep -q -- '--dns-endpoint'; then
      _GKE_DNS_ENDPOINT_SUPPORTED=1
    else
      _GKE_DNS_ENDPOINT_SUPPORTED=0
      echo "WARNING: this gcloud does not support --dns-endpoint; using the IP endpoint." >&2
    fi
  fi
  [ "$_GKE_DNS_ENDPOINT_SUPPORTED" = "1" ]
}

# gke_dns_endpoint_flag <cluster> <location> <project>
#
# Sets GKE_DNS_ENDPOINT_FLAG to "--dns-endpoint" when the cluster publishes a DNS
# endpoint that accepts external traffic, and to the empty string otherwise.
# Callers splice $GKE_DNS_ENDPOINT_FLAG in unquoted so that the empty case
# contributes no argument at all.
#
# It assigns rather than echoing so that the memo above can work. Called as
# `$(gke_dns_endpoint_flag ...)` the function runs in a subshell, which discards
# every variable it sets, and each re-run pays gcloud's ~2s start-up for a help
# text whose answer cannot have changed. Assigning keeps the function in the
# caller's shell, so a script that connects to several clusters -- the staging
# workload scripts loop over a map of them -- probes once rather than once per
# cluster.
#
# Never fails the caller, and never reports a failure either. A cluster that
# cannot be described -- no permission, no network, a name that does not exist,
# or a cluster this run has not created yet -- yields the empty string, which is
# the command that ran before this helper existed. Reaching an ordinary public
# cluster must not become contingent on an extra API call succeeding.
#
# shellcheck disable=SC2034  # GKE_DNS_ENDPOINT_FLAG is read by the callers, not here.
gke_dns_endpoint_flag() {
  GKE_DNS_ENDPOINT_FLAG=""
  local cluster=$1 location=$2 project=$3
  [ -n "$cluster" ] && [ -n "$location" ] && [ -n "$project" ] || return 0
  gke_supports_dns_endpoint || return 0

  # `trap - ERR` inside the substitution: under bash 3.2 (macOS's default, and
  # the `curl | bash` audience) the caller's inherited ERR trap fires in this
  # subshell even though the failure is the tested condition of the `if`, and
  # the `|| true` a caller adds outside it cannot reach that. install.sh's
  # on_error then prints a fatal-looking abort banner and rewrites the install
  # report as FAILED; its `exit` only leaves the subshell, so the run carries on
  # having announced a death that did not happen, and any later failure is
  # reported against this line.
  #
  # A describe that fails is the NORMAL path here, not an edge. install.sh
  # resolves this flag in the chat interview to print a get-credentials command,
  # which is step 6 -- the cluster is created by the apply at step 12, so on
  # every fresh install, --dry-run and --generate-only run there is nothing to
  # describe. That miss must stay what the contract above says it is: an empty
  # flag, and a printed command without --dns-endpoint.
  #
  # Every other gcloud probe in install.sh and installer_common.sh clears the
  # trap the same way.
  local described endpoint external
  if ! described=$(trap - ERR; gcloud container clusters describe "$cluster" \
      --location "$location" --project "$project" \
      --format="value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint,controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)" \
      2>/dev/null); then
    return 0
  fi
  # value() emits the two fields tab-separated, and renders the boolean as
  # True/False. A field GKE did not set comes back empty.
  #
  # A row with no separator at all is not the two-field answer that format asked
  # for, so treat it as unknown. Without this the suffix expansion below returns
  # the whole line when it finds no tab, and a lone "True" would read as both a
  # non-empty endpoint and an allowExternalTraffic of True -- this predicate
  # failing open, in the one file whose every other branch fails closed.
  case $described in
    *$'\t'*) ;;
    *) return 0 ;;
  esac
  endpoint=${described%%$'\t'*}
  external=${described#*$'\t'}
  if [ -n "$endpoint" ] && [ "$external" = "True" ]; then
    GKE_DNS_ENDPOINT_FLAG="--dns-endpoint"
  fi
  # Explicit, because the `if` satisfies "never fails the caller" only
  # incidentally. Rewritten as the shorter
  # `[ -n "$endpoint" ] && [ "$external" = "True" ] && GKE_DNS_ENDPOINT_FLAG=…`
  # it would return 1 for every cluster without an externally reachable DNS
  # endpoint, which under the callers' `set -Eeuo pipefail` is an aborted run.
  return 0
}
