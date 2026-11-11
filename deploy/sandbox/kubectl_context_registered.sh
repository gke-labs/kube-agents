#!/bin/sh
# kubectl-context-registered: exit 0 when the `--context gke_...` a kubectl argv names is already
# registered, as `credential_proxy_client.py --ensure-kubectl-context` would write it, in the
# kubeconfig kubectl is about to read; exit 1 otherwise.
#
# The sandbox's /usr/local/bin/kubectl wrapper runs it first and starts Python only on exit 1,
# so a call for a registered context skips about 0.2 s of Python startup. It never reads the YAML
# itself: it says "registered" only when Python's merge recorded, for these exact file bytes and this
# proxy URL, CA file and token file, that it found the context current or wrote it. Anything else
# (no record, an edited file, a symlinked or missing kubeconfig or record, an unusual argv) exits 1,
# and Python handles the call as before.
set -u

readonly DEFAULT_CA_FILE="/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt"
readonly DEFAULT_TOKEN_FILE="/var/run/secrets/kubeagents/credential-proxy/token"
readonly CONTEXT_FLAG="--context"
readonly KUBECONFIG_FLAG="--kubeconfig"
readonly END_OF_FLAGS="--"
readonly NOT_REGISTERED=1
# Beside a kubeconfig `<dir>/<name>`, Python's record is `<dir>/.<name>.contexts-registered`
# (REGISTERED_MARKER_NAME in credential_proxy_client.py).
readonly MARKER_SUFFIX=".contexts-registered"

proxy_url="${KUBE_PROXY_URL:-}"
proxy_url="${proxy_url%/}"
[ -n "${proxy_url}" ] || exit "${NOT_REGISTERED}"

# The last --context before `--` wins, as in kubectl; a --kubeconfig of its own is Python's case.
context=""
expect_value=""
for argument in "$@"; do
  if [ -n "${expect_value}" ]; then
    context="${argument}"
    expect_value=""
    continue
  fi
  case "${argument}" in
  "${END_OF_FLAGS}") break ;;
  "${KUBECONFIG_FLAG}" | "${KUBECONFIG_FLAG}"=*) exit "${NOT_REGISTERED}" ;;
  "${CONTEXT_FLAG}") expect_value=1 ;;
  "${CONTEXT_FLAG}"=*) context="${argument#"${CONTEXT_FLAG}"=}" ;;
  esac
done

# gke_<project>_<location>_<cluster>, each part [a-z0-9][a-z0-9-]*, as parse_gke_context requires.
case "${context}" in
gke_*_*_*) ;;
*) exit "${NOT_REGISTERED}" ;;
esac
parts="${context#gke_}"
old_ifs="${IFS}"
set -f # a part must not be glob-expanded against the working directory
IFS=_
# shellcheck disable=SC2086 # splitting on `_` is the point
set -- ${parts}
IFS="${old_ifs}"
set +f
[ "$#" -eq 3 ] || exit "${NOT_REGISTERED}"
for part in "$@"; do
  case "${part}" in
  "" | -* | *[!a-z0-9-]*) exit "${NOT_REGISTERED}" ;;
  esac
done

# The file kubectl reads: KUBECONFIG when it names one file, else ~/.kube/config.
kubeconfig="${KUBECONFIG:-}"
case "${kubeconfig}" in
*:*) exit "${NOT_REGISTERED}" ;;
"") kubeconfig="${HOME:-}/.kube/config" ;;
esac
[ -f "${kubeconfig}" ] && [ ! -L "${kubeconfig}" ] || exit "${NOT_REGISTERED}"

ca_file="${KUBE_PROXY_CA_FILE:-${DEFAULT_CA_FILE}}"
token_file="${CREDENTIAL_PROXY_TOKEN_FILE:-${DEFAULT_TOKEN_FILE}}"

# Python's merge records, beside each kubeconfig it checks and under its lock, one line per context it
# found current or wrote: `<context> <sha256 of the file> <sha256 of proxy URL, CA file and token file>`.
# Only that exact line for this file's bytes and this environment says "registered": any edit to the
# file since, or another proxy URL, CA file or token file, falls back to Python, which checks the file
# with its own YAML reader and records the line again.
kubeconfig_dir="$(dirname -- "${kubeconfig}")"
kubeconfig_name="$(basename -- "${kubeconfig}")"
marker="${kubeconfig_dir}/.${kubeconfig_name}${MARKER_SUFFIX}"
[ -f "${marker}" ] && [ ! -L "${marker}" ] || exit "${NOT_REGISTERED}"
file_digest="$(sha256sum <"${kubeconfig}")" || exit "${NOT_REGISTERED}"
env_digest="$(printf '%s\n%s\n%s' "${proxy_url}" "${ca_file}" "${token_file}" | sha256sum)" || exit "${NOT_REGISTERED}"
grep -Fqx -- "${context} ${file_digest%% *} ${env_digest%% *}" "${marker}"
