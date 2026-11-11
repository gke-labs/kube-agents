#!/bin/sh
# kubectl-context-registered: exit 0 when the `--context gke_...` a kubectl argv names is already
# registered, as `credential_proxy_client.py --ensure-kubectl-context` would write it, in the
# kubeconfig kubectl is about to read; exit 1 otherwise.
#
# The sandbox's /usr/local/bin/kubectl wrapper runs it first and starts Python only on exit 1,
# so a call for a registered context skips about 0.2 s of Python startup. It only ever says
# "registered" for entries Python would leave untouched: the context's cluster, context and user
# entries must each hold, inside that entry, the values Python checks, with the current proxy URL,
# CA file and token file. Anything it does not recognise (a quoted value, another layout, a
# symlinked or missing kubeconfig, an unusual argv) exits 1, and Python handles the call as before.
set -u

readonly DEFAULT_CA_FILE="/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt"
readonly DEFAULT_TOKEN_FILE="/var/run/secrets/kubeagents/credential-proxy/token"
readonly USER_NAME="kube-proxy-caller"
readonly CONTEXT_FLAG="--context"
readonly KUBECONFIG_FLAG="--kubeconfig"
readonly END_OF_FLAGS="--"
readonly NOT_REGISTERED=1

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

# The entries Python checks before it leaves a kubeconfig alone (_merge_context_into_kubeconfig_file),
# each one inside a single list item: a cluster named <ctx> with the current server and CA file; a
# context named <ctx> for cluster <ctx> and user kube-proxy-caller; and the kube-proxy-caller user
# with the current token file and nothing else. Lines are compared as whole strings after the indent
# and a list item's `- ` are dropped; no regex is built from a value, so no value can widen a match.
# A layout the parser does not follow (flow style, anchors, quotes) only means a fallback to Python.
awk \
  -v name="name: ${context}" \
  -v server="server: ${proxy_url}/clusters/${context}" \
  -v ca="certificate-authority: ${ca_file}" \
  -v cluster="cluster: ${context}" \
  -v user="user: ${USER_NAME}" \
  -v user_name="name: ${USER_NAME}" \
  -v token="tokenFile: ${token_file}" '
  function finish_item() {
    if (in_item && section == "clusters" && item[name] && item[server] && item[ca]) cluster_ok = 1
    if (in_item && section == "contexts" && item[name] && item[cluster] && item[user]) context_ok = 1
    if (in_item && section == "users" && item[user_name] && user_fields == 1 && user_token) user_ok = 1
    split("", item)
    in_item = 0; in_user = 0; user_fields = 0; user_token = 0
  }
  {
    raw = $0
    sub(/[ \t\r]+$/, "", raw)
    if (raw ~ /^[ \t]*$/ || raw ~ /^[ \t]*#/) next
    match(raw, /^ */)
    indent = RLENGTH
    if (indent == 0 && raw !~ /^- /) {
      # A top-level key starts a section; only clusters, contexts and users hold entries.
      finish_item()
      section = raw
      sub(/:.*/, "", section)
      item_indent = -1
      next
    }
    starts_item = (raw ~ /^ *- /)
    if (starts_item) {
      if (item_indent < 0) item_indent = indent
      if (indent == item_indent) { finish_item(); in_item = 1 }
    }
    if (!in_item) next
    key_indent = indent + (starts_item ? 2 : 0)
    line = raw
    sub(/^ *(- )?/, "", line)
    if (in_user && key_indent <= user_indent) in_user = 0
    if (in_user) {
      user_fields++
      if (line == token) user_token = 1
      next
    }
    if (section == "users" && line == "user:") { in_user = 1; user_indent = key_indent; next }
    item[line] = 1
  }
  END {
    finish_item()
    exit !(cluster_ok && context_ok && user_ok)
  }' "${kubeconfig}"
