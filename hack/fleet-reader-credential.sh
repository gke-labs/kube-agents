#!/usr/bin/env bash
# ==============================================================================
# kubectl exec-credential plugin for the seeded fleet's read-only account
# ==============================================================================
# Prints an ExecCredential for $1 (a service account email) on stdout, minting
# an access token by impersonation and caching it on disk between calls.
#
# WHY A PLUGIN AND NOT A TOKEN IN THE KUBECONFIG. An impersonated access token
# lives one hour. `hack/ci-eval-pr.sh` writes the fleet kubeconfigs once, before
# the image build, and the task fan-out that reads them starts hours later --
# recorded whole-job times in that file are 197.9, ~180 and 221.7 minutes
# against a 360-minute deadline. A token baked into the file at write time is
# therefore expired by the time most fleet checks run, and an expired credential
# makes every one of them report `status: "error"`, which per AGENTS.md is an
# absolute rung that reds the presubmit for any case, admitted or not. kubectl
# re-invokes this plugin instead, so the credential is minted against the clock
# of the check rather than the clock of the setup step.
#
# WHY THE DISK CACHE. kubectl's exec-credential cache is per-process, and every
# fleet check is a separate `kubectl` invocation, so an in-memory cache saves
# nothing -- without a cache here, each of the hundreds of checks in a matrix
# run would pay an IAM round trip. The cache turns that back into roughly one
# mint per refresh window per leased project.
#
# WHY NOT gke-gcloud-auth-plugin. It has no impersonation of its own, so it
# would authenticate as whoever ran gcloud -- the Prow runner, which is exactly
# the read-write identity the seeded-fleet reader exists to avoid.
#
# Usage (from a kubeconfig written by hack/fleet-kubeconfigs.sh):
#   exec:
#     apiVersion: client.authentication.k8s.io/v1
#     command: <this script>
#     args: ["seeded-fleet-reader@<project>.iam.gserviceaccount.com"]
#
# Inputs:
#   $1                          service account email to impersonate (required)
#   FLEET_READER_TOKEN_CACHE    cache file; defaults under TMPDIR, per account
# ==============================================================================
set -euo pipefail

# The ExecCredential schema kubectl is told to speak, in the kubeconfig's
# `exec.apiVersion`. The two must match exactly or kubectl rejects the reply.
readonly CRED_API_VERSION="client.authentication.k8s.io/v1"

# What Google issues for an impersonated account unless the organization allows
# extended lifetimes. Assumed rather than read back: `print-access-token` does
# not report an expiry, and asking for one costs another round trip per mint.
readonly TOKEN_LIFETIME_SECONDS=3600

# How long before that expiry a cached token stops being reused. It has to
# cover the longest single check the token might be handed to, since a token
# that expires mid-call errors the check rather than retrying.
readonly REFRESH_MARGIN_SECONDS=600

# An OAuth2 bearer token is a run of unreserved characters. Anything else on
# gcloud's stdout is diagnostic text that leaked, and emitting it as a
# credential is the silent-401 failure documented in hack/fleet-kubeconfigs.sh.
readonly TOKEN_CHARSET='[^A-Za-z0-9._~+/=-]'

_die() {
  echo "fleet-reader-credential: $*" >&2
  exit 1
}

[ $# -ge 1 ] && [ -n "${1:-}" ] || _die "usage: $0 <service-account-email>"
SA="$1"

# One cache per account, so two accounts in one job cannot read each other's
# token. The email is used verbatim: it comes from the kubeconfig this repo
# wrote, and the character class keeps a path separator out of the filename.
CACHE="${FLEET_READER_TOKEN_CACHE:-${TMPDIR:-/tmp}/fleet-reader-token-$(printf '%s' "$SA" | tr -c 'A-Za-z0-9._-' '_')}"

_emit() {
  # Composed by hand rather than with jq: jq is not in every image this runs
  # in, and the only variable field is a token the charset check above has
  # already constrained to characters that need no JSON escaping.
  printf '{"apiVersion":"%s","kind":"ExecCredential","status":{"token":"%s"}}\n' \
    "$CRED_API_VERSION" "$1"
}

# A cached token is reused while it is further from its assumed expiry than the
# refresh margin. Anything unreadable or unparseable is treated as a miss
# rather than an error -- a stale cache must never be the reason a check fails.
if [ -r "$CACHE" ]; then
  minted=""
  cached=""
  IFS=' ' read -r minted cached <"$CACHE" || true
  # A non-numeric first field is a corrupt cache, and `$((...))` on one would
  # abort the script under `set -e` instead of falling through to a fresh mint.
  case "$minted" in
    '' | *[!0-9]*) minted="" ;;
  esac
  if [ -n "$minted" ] && [ -n "$cached" ]; then
    age=$(($(date -u +%s) - minted))
    if [ "$age" -ge 0 ] && [ "$age" -lt $((TOKEN_LIFETIME_SECONDS - REFRESH_MARGIN_SECONDS)) ]; then
      _emit "$cached"
      exit 0
    fi
  fi
fi

# NOT 2>&1. On the SUCCESS path gcloud prints "WARNING: This command is using
# service account impersonation..." to stderr; folding that into stdout yields a
# multi-line blob that reads as a token and authenticates as nobody.
errors="$(mktemp)" || _die "could not create a temporary file"
trap 'rm -f "$errors"' EXIT
token="$(gcloud auth print-access-token --impersonate-service-account="$SA" 2>"$errors")" ||
  _die "could not mint a token for ${SA}: $(tr '\n' ' ' <"$errors")"

[ -n "$token" ] || _die "gcloud returned an empty token for ${SA}"
printf '%s' "$token" | LC_ALL=C grep -q "$TOKEN_CHARSET" &&
  _die "what gcloud returned for ${SA} is not a bare access token; refusing to emit it"

# Written to a temporary file in the cache's own directory and moved into
# place, so a reader concurrent with the fan-out sees either the old token or
# the new one and never a half-written line.
staged="${CACHE}.$$"
(
  umask 077
  printf '%s %s\n' "$(date -u +%s)" "$token" >"$staged"
) && mv -f "$staged" "$CACHE" || rm -f "$staged"

_emit "$token"
