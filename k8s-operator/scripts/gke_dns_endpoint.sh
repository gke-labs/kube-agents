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
# share anything else: k8s-operator/scripts/common.sh, hack/ci-env.sh, and
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
# Never fails the caller. A cluster that cannot be described -- no permission, no
# network, a name that does not exist -- yields the empty string, which is the
# command that ran before this helper existed. Reaching an ordinary public
# cluster must not become contingent on an extra API call succeeding.
#
# shellcheck disable=SC2034  # GKE_DNS_ENDPOINT_FLAG is read by the callers, not here.
gke_dns_endpoint_flag() {
  GKE_DNS_ENDPOINT_FLAG=""
  local cluster=$1 location=$2 project=$3
  [ -n "$cluster" ] && [ -n "$location" ] && [ -n "$project" ] || return 0
  gke_supports_dns_endpoint || return 0

  local described endpoint external
  if ! described=$(gcloud container clusters describe "$cluster" \
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
}
