#!/usr/bin/env bash
# The lane step that stands a self-managed forge up for one eval run, and takes
# it down after (kube-agents#3682, #3683, #3684). A GitHub Enterprise Server
# row (`EVAL_FORGE=ghes`) gets the forwarding proxy in hack/ghes-proxy on a
# small VM in the leased project; a self-managed GitLab row
# (`EVAL_FORGE=gitlab-ce`) gets GitLab CE on a larger one, with the bot (a
# Developer, as on gitlab.com), its two tokens, the group, the project and the
# manifest the cases read made on the VM by root. Both
# stand at the host the forge table names, with a private CA made here and
# kept nowhere else. The two public rows need nothing and this script exits at
# once on them.
#
#   hack/ci-forge.sh up     before hack/ci-deploy.sh: makes the certificates
#                           (and, on gitlab-ce, the two tokens), builds the
#                           proxy image on ghes, deletes any forge VM an
#                           earlier run left, creates this run's VM at the
#                           project's reserved address, waits until the forge
#                           answers over TLS with the CA, proves a pod in the
#                           host cluster reaches it, on gitlab-ce opens the
#                           tunnel the job pod needs, and writes ca.crt (and
#                           the tokens) into
#                           EVAL_FORGE_STATE_DIR, the files the deploy and the
#                           eval refuse to run without.
#   hack/ci-forge.sh down   after hack/ci-eval-pr.sh, from the job's cleanup
#                           trap whatever the eval's exit code: closes the
#                           tunnel, deletes the VM and the files `up` wrote.
#                           Idempotent.
#
# The project carries the once-per-project pieces this script only reads:
# the private zone `eval-internal` with the host's record, the reserved
# address `forge-<row>` it points at, the firewall rule that opens 443 to the
# VPC, and the VM's identity `forge-vm`, a registry reader and nothing else
# (scripts/provision_ci_pool_project.sh; docs/ci-pool-projects.md 5.8). A
# project missing one is refused here, at second zero, by name.
#
# What the job pod can and cannot reach: it runs in the Prow build cluster,
# not in the leased project's VPC, so it resolves no `eval.internal` name and
# reaches no private address. Readiness is therefore read from the VM's own
# serial console (the forge's self-check with the CA), and the path that
# matters, a pod in the host cluster to the forge, is proved with one curl
# from a pod there. On ghes that is all: the eval grades through api.github.com.
# On gitlab-ce the eval's probe, resets and grader call the private host from
# the job pod, so `up` opens a tunnel: a relay pod in the host cluster, a
# port-forward to it on the job pod's loopback 443 (the host is a bare name
# everywhere) and a hosts entry; `down` closes it. Nothing here touches the
# chart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Constants ────────────────────────────────────────────────────────────────
# The label every forge VM carries, with the row as its value: `down` and the
# leftover sweep in `up` find VMs by it, never by name alone.
readonly FORGE_VM_LABEL_KEY="kube-agents-forge"
readonly FORGE_VM_BUILD_LABEL_KEY="kube-agents-build"
# Per-project names (scripts/provision_ci_pool_project.sh writes them; the
# verifier checks them): the VM and its reserved address are `forge-<row>`.
readonly FORGE_NAME_PREFIX="forge"
readonly FORGE_NETWORK_TAG="forge"
readonly FORGE_FIREWALL_RULE="allow-forge-443"
readonly FORGE_DENY_RULE="deny-forge-ingress"
readonly FORGE_DNS_ZONE="eval-internal"
readonly FORGE_VM_SERVICE_ACCOUNT_ID="forge-vm"
readonly FORGE_VM_IMAGE_FAMILY="cos-stable"
readonly FORGE_VM_IMAGE_PROJECT="cos-cloud"
readonly FORGE_VM_NETWORK="default"
# The VM's zone: the region's -a unless FORGE_VM_ZONE says otherwise. The
# reserved address is regional, so any zone of the region serves it.
readonly FORGE_VM_ZONE_SUFFIX="-a"
# The certificates live as long as the run: one day covers the 360m presubmit
# and the 480m nightly ceilings with room, and nothing keeps them after.
readonly FORGE_CERT_DAYS=1
readonly FORGE_READY_POLL_SECONDS="${FORGE_READY_POLL_SECONDS:-5}"
readonly FORGE_SERIAL_TAIL_LINES=40
# Where the certificates and the startup script live until the VM has them:
# the caller's TMPDIR, so a test can own and inspect it.
readonly FORGE_SCRATCH_DIR="${TMPDIR:-/tmp}"
# The metadata items the secrets ride in to the VM, removed once it is up: the
# server key on every row, the two tokens on gitlab-ce.
readonly FORGE_KEY_METADATA_KEY="tls-key"
readonly FORGE_AGENT_TOKEN_METADATA_KEY="agent-token"
readonly FORGE_LEDGER_TOKEN_METADATA_KEY="ledger-token"
# The files beside ca.crt the deploy and the eval read on gitlab-ce.
readonly FORGE_AGENT_TOKEN_FILE_NAME="agent-token"
readonly FORGE_LEDGER_TOKEN_FILE_NAME="ledger-token"
readonly FORGE_BUILD_TIMEOUT_SECONDS="${FORGE_BUILD_TIMEOUT_SECONDS:-900}"
# The submit's own bound: Cloud Build's --timeout ends the build, not a client
# whose log stream stalled after it (kube-agents#3129), so the client gets the
# build's time plus the upload and the stream's tail.
readonly FORGE_BUILD_CLIENT_BOUND_SECONDS="${FORGE_BUILD_CLIENT_BOUND_SECONDS:-$((FORGE_BUILD_TIMEOUT_SECONDS + 120))}"
# The in-cluster reachability check: one pod, one curl, bounded.
readonly FORGE_CLUSTER_CHECK_IMAGE="curlimages/curl:8.11.1@sha256:c1fe1679c34d9784c1b0d1e5f62ac0a79fca01fb6377cdd33e90473c6f9f9a69"
# The VM takes no project-wide ssh key: with the deny rule on its tag, nothing
# but 443 from the VPC reaches it, and nothing could log in if it did.
readonly FORGE_VM_METADATA_SSH="block-project-ssh-keys=TRUE"
readonly FORGE_CLUSTER_CHECK_TIMEOUT="180s"
readonly FORGE_CLUSTER_CHECK_NAMESPACE="default"
# The record `up` leaves beside ca.crt so `down` deletes exactly this run's VM.
readonly FORGE_STATE_RECORD="forge-vm"
# The GHES row: the proxy image name in the project's registry, the version
# header it serves, and the machine that carries it (seconds to ready).
readonly GHES_PROXY_IMAGE_NAME="ghes-proxy"
readonly GHES_PROXY_ENTERPRISE_VERSION="3.17.0"
readonly GHES_PROXY_MACHINE_TYPE="e2-small"
readonly GHES_READY_TIMEOUT_SECONDS="${GHES_READY_TIMEOUT_SECONDS:-300}"
readonly GHES_READY_PATH="/api/v3/meta"
# The GitLab CE row: the image (Docker Hub, pinned), the machine and disk it
# needs, its first boot (6-10 min on a fresh data directory, measured on
# 2026-10-21, so 20 min to give up), the sign-in page as readiness (the
# readiness endpoint is whitelist-gated), the bot the eval expects (its login
# is re-read from the agent token there) and its two tokens: one day, like the
# certificates, GitLab's prefix and 20 hex characters, its minimum.
readonly GITLAB_CE_IMAGE="gitlab/gitlab-ce:19.4.3-ce.0@sha256:483fb4cc5c75237a7a6c7c8b8b2a1832ed790d86e0449ff55e8b6f6d62d2d5f5"
readonly GITLAB_CE_MACHINE_TYPE="e2-standard-4"
readonly GITLAB_CE_BOOT_DISK="50GB"
readonly GITLAB_CE_READY_TIMEOUT_SECONDS="${GITLAB_CE_READY_TIMEOUT_SECONDS:-1200}"
readonly GITLAB_CE_READY_PATH="/users/sign_in"
readonly GITLAB_CE_BOT_LOGIN="kube-agents-eval-bot"
readonly GITLAB_CE_BOT_EMAIL="eval-bot@eval.internal"
readonly GITLAB_CE_TOKEN_DAYS=1
readonly GITLAB_CE_TOKEN_PREFIX="glpat-"
readonly GITLAB_CE_TOKEN_RANDOM_BYTES=10
# The manifest the remediation cases read rides to the VM in metadata (public
# content, the provisioning script's seed) and root commits it there, so the
# bot stays a Developer, as on gitlab.com, and no Owner token exists.
readonly FORGE_MANIFEST_PATH_METADATA_KEY="manifest-path"
readonly FORGE_MANIFEST_MESSAGE_METADATA_KEY="manifest-message"
readonly FORGE_MANIFEST_CONTENT_METADATA_KEY="manifest-content"
# The tunnel from the job pod to the private host: a relay pod in the host
# cluster (socat, pinned) listening on an unprivileged port and forwarding to
# the forge's 443, a port-forward to it on the job pod's loopback 443 (every
# consumer dials the bare host on 443, so no other port serves; the job runs
# as root, as the Prow pod does), kept up by a loop that restarts it when the
# API stream drops, and a line in the hosts file (the knob is the tests').
readonly FORGE_RELAY_POD="forge-relay"
readonly FORGE_RELAY_IMAGE="alpine/socat:1.8.1.3@sha256:603174b77eebaba25ba8f8ebb15e1234d6600409554e95d6fb203fd106150287"
readonly FORGE_RELAY_PORT=8443
readonly FORGE_TUNNEL_LOCAL_PORT=443
readonly FORGE_TUNNEL_HOSTS_FILE="${FORGE_TUNNEL_HOSTS_FILE:-/etc/hosts}"
readonly FORGE_TUNNEL_READY_SECONDS=60
readonly FORGE_TUNNEL_POLL_SECONDS="${FORGE_TUNNEL_POLL_SECONDS:-2}"
readonly FORGE_TUNNEL_RESTART_SECONDS=2
readonly FORGE_TUNNEL_CHECK_MAX_TIME=10
readonly FORGE_TUNNEL_LOG_TAIL_LINES=3
readonly FORGE_STATE_TUNNEL_PID="forge-tunnel-pid"
readonly FORGE_STATE_TUNNEL_LOG="forge-tunnel.log"
readonly FORGE_STATE_TUNNEL_CONTEXT="forge-tunnel-context"

# shellcheck source=hack/ci-env.sh
source "${SCRIPT_DIR}/ci-env.sh"
FORGE_VM_ZONE="${FORGE_VM_ZONE:-${REGION}${FORGE_VM_ZONE_SUFFIX}}"

# ─── The forge table, lifted from the deploy ─────────────────────────────────
# One table for the three scripts: hack/ci-deploy.sh holds it and this script
# lifts it exactly as hack/ci-eval-pr.sh does, so a row added there is a row
# here.
forge_profile_lifted() { # <EVAL_FORGE> -> "<kind> <host> <yes|no>"
  local body
  body="$(sed -n '/^forge_profile() {/,/^}$/p' "${SCRIPT_DIR}/ci-deploy.sh")"
  [ -n "${body}" ] || return 1
  eval "$(sed -n 's/^\(EVAL_FORGE_DEFAULT_[A-Z_]*HOST=.*\)$/\1/p' "${SCRIPT_DIR}/ci-deploy.sh")"
  eval "${body}"
  forge_profile "$1"
}

# The GitLab project the eval derives from the lease, `<group>/<PROJECT_ID><suffix>`
# (hack/ci-deploy.sh's constants, lifted as hack/ci-eval-pr.sh lifts them).
gitlab_project_lifted() { # -> "<group> <suffix>"
  local group suffix
  group="$(sed -n 's/^GITLAB_FORGE_GROUP="\(.*\)"$/\1/p' "${SCRIPT_DIR}/ci-deploy.sh")"
  suffix="$(sed -n 's/^GITLAB_FORGE_PROJECT_SUFFIX="\(.*\)"$/\1/p' "${SCRIPT_DIR}/ci-deploy.sh")"
  [ -n "${group}" ] && [ -n "${suffix}" ] || return 1
  echo "${group} ${suffix}"
}

# The manifest the remediation cases read, seeded into the per-run project:
# the provisioning script's constants (path, message, content), lifted.
manifest_seed_lifted() {
  eval "$(sed -n "/^readonly GITOPS_WORKLOAD_MANIFEST_PATH=/,/^.*'\$/p" "${SCRIPT_DIR}/../scripts/provision_ci_pool_project.sh")"
  [ -n "${GITOPS_WORKLOAD_MANIFEST_PATH:-}" ] && [ -n "${GITOPS_WORKLOAD_MANIFEST_CONTENT:-}" ] || return 1
}

usage() {
  echo "usage: $0 up|down   (EVAL_FORGE, PROJECT_ID and REGION from the environment, as the sibling scripts)" >&2
  exit 64
}

ACTION="${1:-}"
case "${ACTION}" in
  up | down) ;;
  *) usage ;;
esac

EVAL_FORGE="${EVAL_FORGE:-github}"
EVAL_FORGE_PROFILE_RC=0
EVAL_FORGE_PROFILE="$(forge_profile_lifted "${EVAL_FORGE}")" || EVAL_FORGE_PROFILE_RC=$?
if [ "${EVAL_FORGE_PROFILE_RC}" -ne 0 ]; then
  [ "${EVAL_FORGE_PROFILE_RC}" -eq 2 ] || echo "ERROR: EVAL_FORGE='${EVAL_FORGE}' is not a forge this script knows; use github (default), gitlab, ghes or gitlab-ce (forge_profile in hack/ci-deploy.sh)." >&2
  exit 1
fi
read -r EVAL_FORGE_KIND EVAL_FORGE_HOST EVAL_FORGE_SELF_MANAGED <<<"${EVAL_FORGE_PROFILE}"
if [ "${EVAL_FORGE_SELF_MANAGED}" != "yes" ]; then
  echo "Forge ${ACTION}: EVAL_FORGE=${EVAL_FORGE} is ${EVAL_FORGE_HOST}, a public forge; nothing to stand up or take down."
  exit 0
fi

# The state directory the deploy and the eval read (the same default and the
# same rule in both): absolute, private to the job's pod, never under the
# artifacts.
EVAL_FORGE_STATE_DIR="${EVAL_FORGE_STATE_DIR:-/tmp/eval-forge}"
case "${EVAL_FORGE_STATE_DIR}" in
  /*) ;;
  *) echo "ERROR: EVAL_FORGE_STATE_DIR='${EVAL_FORGE_STATE_DIR}' must be an absolute path: the grader runs under bench/ and reads the CA by this path (kube-agents#3682)." >&2; exit 1 ;;
esac
FORGE_CA_FILE="${EVAL_FORGE_STATE_DIR}/ca.crt"
FORGE_RECORD_FILE="${EVAL_FORGE_STATE_DIR}/${FORGE_STATE_RECORD}"
FORGE_AGENT_TOKEN_FILE="${EVAL_FORGE_STATE_DIR}/${FORGE_AGENT_TOKEN_FILE_NAME}"
FORGE_LEDGER_TOKEN_FILE="${EVAL_FORGE_STATE_DIR}/${FORGE_LEDGER_TOKEN_FILE_NAME}"
FORGE_TUNNEL_PID_FILE="${EVAL_FORGE_STATE_DIR}/${FORGE_STATE_TUNNEL_PID}"
FORGE_TUNNEL_LOG_FILE="${EVAL_FORGE_STATE_DIR}/${FORGE_STATE_TUNNEL_LOG}"
FORGE_TUNNEL_CONTEXT_FILE="${EVAL_FORGE_STATE_DIR}/${FORGE_STATE_TUNNEL_CONTEXT}"

FORGE_VM_NAME="${FORGE_NAME_PREFIX}-${EVAL_FORGE}"
FORGE_ADDRESS_NAME="${FORGE_NAME_PREFIX}-${EVAL_FORGE}"
FORGE_VM_SERVICE_ACCOUNT="${FORGE_VM_SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
export AR_REPO="${AR_REPO:-us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents}"
# The image tag carries the pull request and build like the deploy's, under
# the `pr-` prefix the registry's cleanup policy sweeps after 14 days.
FORGE_IMAGE_TAG="${FORGE_IMAGE_TAG:-pr-${PULL_NUMBER:-local}-${BUILD_ID:-$(date -u +%Y%m%d%H%M%S)}}"

# ─── Helpers ──────────────────────────────────────────────────────────────────
stamp() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }

# Every forge VM in the project, "<name> <zone>" per line, by label; the row is
# optional. Any zone: a run that set FORGE_VM_ZONE differently still gets
# swept.
list_forge_vms() { # [row]
  local filter="labels.${FORGE_VM_LABEL_KEY}:*"
  [ -z "${1:-}" ] || filter="labels.${FORGE_VM_LABEL_KEY}=${1}"
  gcloud compute instances list --project="${PROJECT_ID}" --filter="${filter}" \
    --format='value(name,zone.basename())'
}

delete_forge_vms() { # <label> [row]: deletes every VM list_forge_vms names
  local label="$1" row="${2:-}" name zone deleted=0 vms
  # The list is read first: a failed list behind a process substitution reads
  # as an empty one, and a VM that was there would stay, counted as none.
  vms="$(list_forge_vms "${row}")" || {
    echo "ERROR: could not list the forge VMs in ${PROJECT_ID}; none deleted" >&2
    return 1
  }
  while read -r name zone; do
    [ -n "${name}" ] || continue
    echo "Forge ${ACTION}: deleting ${label} ${name} in ${zone}"
    gcloud compute instances delete "${name}" --zone="${zone}" --project="${PROJECT_ID}" --quiet
    deleted=$((deleted + 1))
  done <<<"${vms}"
  echo "Forge ${ACTION}: ${deleted} ${label} VM(s) deleted"
}

# ─── the tunnel (gitlab-ce): job pod -> relay pod -> forge ───────────────────
hosts_entry() { printf '127.0.0.1 %s' "${EVAL_FORGE_HOST}"; }

close_tunnel() {
  local pid context=()
  if [ -s "${FORGE_TUNNEL_PID_FILE}" ]; then
    read -r pid < "${FORGE_TUNNEL_PID_FILE}"
    # The keeper loop first, then the port-forward it holds, so no restart follows the kill.
    pkill -P "${pid}" 2>/dev/null || true
    kill "${pid}" 2>/dev/null && echo "Forge ${ACTION}: port-forward keeper ${pid} stopped" || echo "Forge ${ACTION}: port-forward keeper ${pid} was already gone"
    rm -f "${FORGE_TUNNEL_PID_FILE}"
  fi
  [ ! -s "${FORGE_TUNNEL_CONTEXT_FILE}" ] || context=(--context "$(cat "${FORGE_TUNNEL_CONTEXT_FILE}")")
  if [ -w "${FORGE_TUNNEL_HOSTS_FILE}" ] && grep -qxF "$(hosts_entry)" "${FORGE_TUNNEL_HOSTS_FILE}"; then
    grep -vxF "$(hosts_entry)" "${FORGE_TUNNEL_HOSTS_FILE}" > "${FORGE_TUNNEL_HOSTS_FILE}.forge" && cat "${FORGE_TUNNEL_HOSTS_FILE}.forge" > "${FORGE_TUNNEL_HOSTS_FILE}"
    rm -f "${FORGE_TUNNEL_HOSTS_FILE}.forge"
    echo "Forge ${ACTION}: hosts entry for ${EVAL_FORGE_HOST} removed"
  fi
  # The relay pod: by name, in the context `up` recorded (a task cluster's
  # get-credentials may have moved the current one); a run that never reached
  # the cluster has none to delete.
  kubectl ${context[@]+"${context[@]}"} delete pod "${FORGE_RELAY_POD}" -n "${FORGE_CLUSTER_CHECK_NAMESPACE}" --ignore-not-found --wait=false >/dev/null 2>&1 \
    && echo "Forge ${ACTION}: relay pod ${FORGE_RELAY_POD} deleted (if it was there)" \
    || echo "Forge ${ACTION}: no cluster credentials to delete ${FORGE_RELAY_POD} with; the pod, if any, goes with the install's teardown"
}

# ─── down ─────────────────────────────────────────────────────────────────────
forge_down() {
  local started=${SECONDS} name zone
  echo "=== [$(stamp)] Forge down: ${FORGE_VM_NAME} (${EVAL_FORGE} at ${EVAL_FORGE_HOST}) in ${PROJECT_ID} ==="
  [ "${EVAL_FORGE_KIND}" != "gitlab" ] || close_tunnel
  # This run's VM first, by the record `up` wrote; then any VM of the row by
  # label, so a run that died between create and record still gets swept.
  if [ -s "${FORGE_RECORD_FILE}" ]; then
    read -r name zone < "${FORGE_RECORD_FILE}"
    echo "Forge down: deleting this run's ${name} in ${zone}"
    gcloud compute instances delete "${name}" --zone="${zone}" --project="${PROJECT_ID}" --quiet 2>/dev/null \
      || echo "Forge down: ${name} was already gone"
  fi
  delete_forge_vms "forge" "${EVAL_FORGE}"
  # Only what `up` wrote: the directory is the caller's (EVAL_FORGE_STATE_DIR=/tmp
  # would otherwise go with it); it is removed when nothing else is in it.
  rm -f "${FORGE_CA_FILE}" "${FORGE_RECORD_FILE}" "${FORGE_AGENT_TOKEN_FILE}" "${FORGE_LEDGER_TOKEN_FILE}" "${FORGE_TUNNEL_PID_FILE}" "${FORGE_TUNNEL_LOG_FILE}" "${FORGE_TUNNEL_CONTEXT_FILE}"
  rmdir "${EVAL_FORGE_STATE_DIR}" 2>/dev/null || true
  echo "✓ Forge down finished in $((SECONDS - started))s; the forge's files are gone from ${EVAL_FORGE_STATE_DIR}"
}

# ─── up: the project's once-per-project pieces, read, never written ──────────
preflight_project_pieces() {
  local missing=() record_ip
  FORGE_IP="$(gcloud compute addresses describe "${FORGE_ADDRESS_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(address)' 2>/dev/null)" || FORGE_IP=""
  [ -n "${FORGE_IP}" ] || missing+=("the reserved address ${FORGE_ADDRESS_NAME} in ${REGION}")
  record_ip="$(gcloud dns record-sets describe "${EVAL_FORGE_HOST}." --zone="${FORGE_DNS_ZONE}" --type=A --project="${PROJECT_ID}" --format='value(rrdatas[0])' 2>/dev/null)" || record_ip=""
  if [ -z "${record_ip}" ]; then
    missing+=("the A record ${EVAL_FORGE_HOST}. in the private zone ${FORGE_DNS_ZONE}")
  elif [ -n "${FORGE_IP}" ] && [ "${record_ip}" != "${FORGE_IP}" ]; then
    echo "ERROR: ${EVAL_FORGE_HOST}. resolves to ${record_ip} in zone ${FORGE_DNS_ZONE}, but the reserved address ${FORGE_ADDRESS_NAME} is ${FORGE_IP}; the record must point at the address the VM takes (docs/ci-pool-projects.md 5.8)." >&2
    exit 1
  fi
  gcloud compute firewall-rules describe "${FORGE_FIREWALL_RULE}" --project="${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1 \
    || missing+=("the firewall rule ${FORGE_FIREWALL_RULE}")
  gcloud compute firewall-rules describe "${FORGE_DENY_RULE}" --project="${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1 \
    || missing+=("the deny rule ${FORGE_DENY_RULE}")
  gcloud iam service-accounts describe "${FORGE_VM_SERVICE_ACCOUNT}" --project="${PROJECT_ID}" --format='value(email)' >/dev/null 2>&1 \
    || missing+=("the VM identity ${FORGE_VM_SERVICE_ACCOUNT}")
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: ${PROJECT_ID} is missing what a self-managed forge needs: $(IFS='; '; echo "${missing[*]}")." >&2
    echo "       These are provisioned once per pool project (scripts/provision_ci_pool_project.sh; by hand per docs/ci-pool-projects.md 5.8); not creating a VM nothing could reach." >&2
    exit 1
  fi
  echo "✓ Project pieces present: ${FORGE_ADDRESS_NAME}=${FORGE_IP}, ${EVAL_FORGE_HOST}. in ${FORGE_DNS_ZONE}, ${FORGE_FIREWALL_RULE}, ${FORGE_DENY_RULE}, ${FORGE_VM_SERVICE_ACCOUNT}"
}

# ─── up: certificates, from the repo's own helper, Linux only ────────────────
make_certificates() {
  FORGE_CERT_DIR="$(mktemp -d "${FORGE_SCRATCH_DIR}/forge-certs.XXXXXX")" # 0700; make-certs.sh takes an empty directory
  bash "${SCRIPT_DIR}/ghes-proxy/make-certs.sh" "${EVAL_FORGE_HOST}" "${FORGE_CERT_DIR}" "${FORGE_CERT_DAYS}"
  # The deploy's gate would refuse this later; refuse it here, before a VM.
  if ! openssl x509 -noout -text -in "${FORGE_CERT_DIR}/ca.crt" 2>/dev/null | grep -q 'CA:TRUE'; then
    echo "ERROR: ${FORGE_CERT_DIR}/ca.crt is not a CA certificate; make-certs.sh needs OpenSSL (Linux), not LibreSSL (kube-agents#3682)." >&2
    exit 1
  fi
  echo "✓ Certificates for ${EVAL_FORGE_HOST} made, valid ${FORGE_CERT_DAYS} day(s); the CA key is gone"
}

# The two tokens the eval reads on gitlab-ce, minted here and set on the bot by
# the VM (rails sets the value it is given), so no token crosses the serial
# console. They live in scratch beside the key until write_state_dir.
mint_gitlab_tokens() {
  local name
  for name in "${FORGE_AGENT_TOKEN_FILE_NAME}" "${FORGE_LEDGER_TOKEN_FILE_NAME}"; do
    (umask 077; printf '%s%s\n' "${GITLAB_CE_TOKEN_PREFIX}" "$(openssl rand -hex "${GITLAB_CE_TOKEN_RANDOM_BYTES}")" > "${FORGE_CERT_DIR}/${name}")
  done
  echo "✓ Two tokens minted for ${GITLAB_CE_BOT_LOGIN} (agent: api, write_repository; ledger: api), valid ${GITLAB_CE_TOKEN_DAYS} day(s)"
}

# ─── up: the GHES proxy ───────────────────────────────────────────────────────
build_ghes_proxy_image() {
  GHES_PROXY_IMAGE="${AR_REPO}/${GHES_PROXY_IMAGE_NAME}:${FORGE_IMAGE_TAG}"
  echo "=== [$(stamp)] Building ${GHES_PROXY_IMAGE} with Cloud Build ==="
  local bound=(timeout --foreground "${FORGE_BUILD_CLIENT_BOUND_SECONDS}")
  command -v timeout >/dev/null 2>&1 || bound=()
  ${bound[@]+"${bound[@]}"} gcloud builds submit "${SCRIPT_DIR}/ghes-proxy" --tag="${GHES_PROXY_IMAGE}" \
    --project="${PROJECT_ID}" --timeout="${FORGE_BUILD_TIMEOUT_SECONDS}s" --quiet >/dev/null || {
    echo "ERROR: building ${GHES_PROXY_IMAGE} failed or did not return within ${FORGE_BUILD_CLIENT_BOUND_SECONDS}s." >&2
    exit 1
  }
  echo "✓ Proxy image built: ${GHES_PROXY_IMAGE}"
}

# The VM's startup script (Container-Optimized OS). It reads the certificates
# and the image from instance metadata, runs the proxy on 443 as the published
# host, proves it to itself through the loopback with the CA, and prints
# `FORGE <event>` lines to the serial console, which `up` reads from outside
# the VPC. A reboot restarts the container.
ghes_startup_script() {
  cat <<'EOF'
#!/bin/bash
set -u
md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
T0=$(date +%s); mark() { echo "FORGE $1 t=$(( $(date +%s) - T0 ))s"; }
HOST=$(md host); IMAGE=$(md image); VERSION=$(md enterprise-version)
mkdir -p /var/ghes/tls
# First boot copies the certificates out of metadata; `up` then removes the key
# from metadata, so a reboot keeps the copies on disk.
[ -s /var/ghes/tls/tls.crt ] || md tls-crt > /var/ghes/tls/tls.crt
[ -s /var/ghes/tls/tls.key ] || md tls-key > /var/ghes/tls/tls.key
[ -s /var/ghes/ca.crt ] || md ca-crt > /var/ghes/ca.crt
chmod 600 /var/ghes/tls/tls.key; chown -R 65532:65532 /var/ghes/tls  # the image runs as the distroless nonroot user
mark script-start
docker-credential-gcr configure-docker --registries "${IMAGE%%/*}" >/dev/null 2>&1
docker pull "$IMAGE" >/dev/null 2>&1 && mark image-pulled || { echo "FORGE failed pull ${IMAGE}"; exit 1; }
if docker inspect ghes-proxy >/dev/null 2>&1; then
  docker start ghes-proxy >/dev/null && mark container-restarted
else
  docker run -d --name ghes-proxy --restart unless-stopped -p 443:443 -v /var/ghes/tls:/etc/ghes-proxy/tls:ro \
    -e GHES_PROXY_LISTEN=:443 -e GHES_PROXY_PUBLIC_HOST="$HOST" -e GHES_PROXY_ENTERPRISE_VERSION="$VERSION" \
    "$IMAGE" >/dev/null && mark container-started
fi
for i in $(seq 1 60); do
  code=$(curl -s --cacert /var/ghes/ca.crt --resolve "${HOST}:443:127.0.0.1" --max-time 10 -o /dev/null -w '%{http_code}' "https://${HOST}/api/v3/meta" 2>/dev/null)
  [ "$code" = "200" ] && { mark ready; break; }
  sleep 2
done
[ "${code:-}" = "200" ] || { echo "FORGE failed never-ready code=${code:-none}"; docker logs ghes-proxy 2>&1 | tail -5; exit 1; }
curl -s --cacert /var/ghes/ca.crt --resolve "${HOST}:443:127.0.0.1" -D - -o /dev/null "https://${HOST}/api/v3/meta" | grep -i 'x-github-enterprise-version' | sed 's/^/FORGE header: /'
echo "FORGE done total=$(( $(date +%s) - T0 ))s"
EOF
}

# The GitLab CE VM's startup script (Container-Optimized OS). Certificates,
# tokens and names come from instance metadata; GitLab runs on 443 as the
# published host with the private certificate; readiness is the sign-in page
# through the loopback with the CA; the first boot then makes the bot, sets
# its two tokens to the values given (one rails call), and creates the group
# and the project through the API, and marks the disk so a reboot skips that.
# `FORGE ready` is printed only once the project exists.
gitlab_startup_script() {
  cat <<'EOF'
#!/bin/bash
set -u
md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
T0=$(date +%s); mark() { echo "FORGE $1 t=$(( $(date +%s) - T0 ))s"; }
HOST=$(md host); IMAGE=$(md image); GROUP=$(md group); PROJECT=$(md project)
BOT=$(md bot-login); BOT_EMAIL=$(md bot-email); TOKEN_DAYS=$(md token-days)
# The omnibus image runs rails as its `git` user (uid 998); the mounted
# private directory is that user's and nobody else's, so rails can read the
# tokens and nothing else on the VM can.
GIT_UID=998
mkdir -p /var/gitlab/config/ssl /var/gitlab/logs /var/gitlab/data /var/gitlab/private /var/gitlab/seed
md manifest-path > /var/gitlab/seed/path; md manifest-message > /var/gitlab/seed/message; md manifest-content > /var/gitlab/seed/content
# First boot copies the secrets out of metadata; `up` then removes them from
# metadata, so a reboot keeps the copies on disk.
[ -s "/var/gitlab/config/ssl/${HOST}.crt" ] || md tls-crt > "/var/gitlab/config/ssl/${HOST}.crt"
[ -s "/var/gitlab/config/ssl/${HOST}.key" ] || md tls-key > "/var/gitlab/config/ssl/${HOST}.key"
[ -s /var/gitlab/ca.crt ] || md ca-crt > /var/gitlab/ca.crt
[ -s /var/gitlab/private/agent-token ] || md agent-token > /var/gitlab/private/agent-token
[ -s /var/gitlab/private/ledger-token ] || md ledger-token > /var/gitlab/private/ledger-token
# root's own token for the seeding calls below: minted here, never leaves the VM.
[ -s /var/gitlab/private/root-token ] || printf 'glpat-%s\n' "$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)" > /var/gitlab/private/root-token
printf 'PRIVATE-TOKEN: %s\n' "$(cat /var/gitlab/private/root-token)" > /var/gitlab/private/root-header
chmod 600 "/var/gitlab/config/ssl/${HOST}.key" /var/gitlab/private/*
chown -R "${GIT_UID}:${GIT_UID}" /var/gitlab/private; chmod 700 /var/gitlab/private
mark script-start
docker pull "$IMAGE" >/dev/null 2>&1 && mark image-pulled || { echo "FORGE failed pull ${IMAGE}"; exit 1; }
if docker inspect gitlab >/dev/null 2>&1; then
  docker start gitlab >/dev/null && mark container-restarted
else
  # The root password is used by nothing after the first boot: the rails call
  # below runs as root inside the container, and sign-up is off.
  ROOT_PW=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  docker run -d --name gitlab --hostname "$HOST" --restart unless-stopped -p 443:443 --shm-size 256m \
    -v /var/gitlab/config:/etc/gitlab -v /var/gitlab/logs:/var/log/gitlab -v /var/gitlab/data:/var/opt/gitlab -v /var/gitlab/private:/var/gitlab/private:ro -v /var/gitlab/seed:/var/gitlab/seed:ro \
    -e GITLAB_OMNIBUS_CONFIG="external_url 'https://${HOST}'; letsencrypt['enable']=false; nginx['redirect_http_to_https']=false; nginx['ssl_certificate']='/etc/gitlab/ssl/${HOST}.crt'; nginx['ssl_certificate_key']='/etc/gitlab/ssl/${HOST}.key'; gitlab_rails['initial_root_password']='${ROOT_PW}'; gitlab_rails['gitlab_signup_enabled']=false; prometheus_monitoring['enable']=false; gitlab_kas['enable']=false" \
    "$IMAGE" >/dev/null && mark container-started
fi
for i in $(seq 1 240); do
  code=$(curl -s --cacert /var/gitlab/ca.crt --resolve "${HOST}:443:127.0.0.1" --max-time 5 -o /dev/null -w '%{http_code}' "https://${HOST}/users/sign_in" 2>/dev/null)
  [ "$code" = "200" ] && { mark gitlab-ready; break; }
  sleep 5
done
[ "${code:-}" = "200" ] || { echo "FORGE failed never-ready code=${code:-none}"; docker logs gitlab 2>&1 | tail -5; exit 1; }
if [ ! -e /var/gitlab/private/seeded ]; then
  # One rails call as root makes what only rails can: the bot (a Developer,
  # as on gitlab.com), its two tokens and root's own token set to the values
  # in the mounted files (never argv or the environment), and the group.
  # Every namespace needs the instance's organization since GitLab 17
  # ("Namespace organization can't be blank" without it). A failed model
  # prints its errors, not its inspect.
  docker exec -e BOT="$BOT" -e BOT_EMAIL="$BOT_EMAIL" -e TOKEN_DAYS="$TOKEN_DAYS" -e GROUP="$GROUP" gitlab gitlab-rails runner '
admin = User.find_by_username("root")
pw = SecureRandom.hex(24)
org = Organizations::Organization.respond_to?(:default_organization) ? Organizations::Organization.default_organization : Organizations::Organization.first
raise "no organization" unless org
def why(m) = (m.respond_to?(:errors) ? m.errors.full_messages.join("; ") : m.inspect)
res = Users::CreateService.new(admin, username: ENV["BOT"], email: ENV["BOT_EMAIL"], name: "Eval Bot", password: pw, password_confirmation: pw, skip_confirmation: true, projects_limit: 10, organization_id: org.id).execute
u = res.respond_to?(:payload) ? (res.payload[:user] || res[:user]) : res
raise "user create failed: #{res.respond_to?(:message) ? res.message : why(u)}" unless u && u.persisted?
{"agent" => ["api", "write_repository"], "ledger" => ["api"]}.each do |name, scopes|
  t = u.personal_access_tokens.build(name: name, scopes: scopes, expires_at: ENV["TOKEN_DAYS"].to_i.days.from_now)
  t.set_token(File.read("/var/gitlab/private/" + name + "-token").strip); t.save!
end
rt = admin.personal_access_tokens.build(name: "forge-seed", scopes: ["api"], expires_at: ENV["TOKEN_DAYS"].to_i.days.from_now)
rt.set_token(File.read("/var/gitlab/private/root-token").strip); rt.save!
g = Groups::CreateService.new(admin, name: ENV["GROUP"], path: ENV["GROUP"], visibility_level: Gitlab::VisibilityLevel::PRIVATE, organization_id: org.id).execute
g = g.payload[:group] if g.respond_to?(:payload)
g = g[:group] if g.is_a?(Hash)
raise "group create failed: #{why(g)}" unless g && g.persisted?
puts "rails-ok group_id=#{g.id} bot_id=#{u.id}"' >/tmp/rails.out 2>&1 && mark rails-done || { echo "FORGE failed rails"; grep -vE '^\s+from ' /tmp/rails.out | head -12; exit 1; }
  GID=$(sed -n 's/^rails-ok group_id=\([0-9]*\) .*/\1/p' /tmp/rails.out); BOT_ID=$(sed -n 's/^rails-ok .*bot_id=\([0-9]*\)$/\1/p' /tmp/rails.out)
  # The project, the bot's Developer membership and the manifest commit go
  # through the API as root (the path the 2026-10-21 spike proved), with the
  # token from the file above; the default branch keeps GitLab's protection,
  # so the bot cannot push it and opens merge requests instead.
  API="https://${HOST}/api/v4"
  api() { curl -s --cacert /var/gitlab/ca.crt --resolve "${HOST}:443:127.0.0.1" -H @/var/gitlab/private/root-header "$@"; }
  code=$(api -X POST "$API/projects" -d "name=${PROJECT}" -d "path=${PROJECT}" -d "namespace_id=${GID}" -d visibility=private -d initialize_with_readme=true -o /tmp/project.json -w '%{http_code}')
  [ "$code" = "201" ] || { echo "FORGE failed project-create http=${code} $(head -c 300 /tmp/project.json)"; exit 1; }
  PID=$(sed -n 's/^{"id":\([0-9]*\).*/\1/p' /tmp/project.json)
  code=$(api -X POST "$API/projects/${PID}/members" -d "user_id=${BOT_ID}" -d access_level=30 -o /dev/null -w '%{http_code}')
  [ "$code" = "201" ] || { echo "FORGE failed bot-membership http=${code}"; exit 1; }
  BRANCH=$(sed -n 's/.*"default_branch":"\([^"]*\)".*/\1/p' /tmp/project.json); BRANCH="${BRANCH:-main}"
  ENC_PATH=$(sed 's|/|%2F|g' /var/gitlab/seed/path)
  code=$(api -X POST "$API/projects/${PID}/repository/files/${ENC_PATH}" --data-urlencode "branch=${BRANCH}" \
    --data-urlencode "commit_message@/var/gitlab/seed/message" --data-urlencode "content@/var/gitlab/seed/content" -o /tmp/seed.json -w '%{http_code}')
  [ "$code" = "201" ] || { echo "FORGE failed seed http=${code} $(head -c 300 /tmp/seed.json)"; exit 1; }
  touch /var/gitlab/private/seeded; mark seeded
fi
mark ready
echo "FORGE done total=$(( $(date +%s) - T0 ))s"
EOF
}

# The ephemeral external address is the VM's way out (the pool projects have
# no Cloud NAT): github.com for the proxy, Docker Hub for both images. The
# deny rule on the tag and the blocked project ssh keys are what keep that
# address from answering anything but 443 from the VPC. The row sets the
# machine, the startup script, the metadata and the files that ride in it.
create_forge_vm() {
  local startup
  startup="$(mktemp "${FORGE_SCRATCH_DIR}/forge-startup.XXXXXX")"
  FORGE_STARTUP_FILE="${startup}"
  "${FORGE_ROW_STARTUP_FN}" > "${startup}"
  echo "=== [$(stamp)] Creating ${FORGE_VM_NAME} (${FORGE_ROW_MACHINE_TYPE}, ${FORGE_VM_ZONE}, ${FORGE_IP}, ephemeral external address) ==="
  gcloud compute instances create "${FORGE_VM_NAME}" \
    --project="${PROJECT_ID}" --zone="${FORGE_VM_ZONE}" \
    --machine-type="${FORGE_ROW_MACHINE_TYPE}" \
    --image-family="${FORGE_VM_IMAGE_FAMILY}" --image-project="${FORGE_VM_IMAGE_PROJECT}" \
    ${FORGE_ROW_DISK_FLAG:+"${FORGE_ROW_DISK_FLAG}"} \
    --network="${FORGE_VM_NETWORK}" --subnet="${FORGE_VM_NETWORK}" \
    --private-network-ip="${FORGE_IP}" \
    --service-account="${FORGE_VM_SERVICE_ACCOUNT}" --scopes=cloud-platform \
    --tags="${FORGE_NETWORK_TAG}" \
    --labels="${FORGE_VM_LABEL_KEY}=${EVAL_FORGE},${FORGE_VM_BUILD_LABEL_KEY}=${BUILD_ID:-local}" \
    --metadata="host=${EVAL_FORGE_HOST},${FORGE_ROW_METADATA},${FORGE_VM_METADATA_SSH}" \
    --metadata-from-file="startup-script=${startup},tls-crt=${FORGE_CERT_DIR}/tls.crt,${FORGE_KEY_METADATA_KEY}=${FORGE_CERT_DIR}/tls.key,ca-crt=${FORGE_CERT_DIR}/ca.crt${FORGE_ROW_METADATA_FILES}" \
    --quiet >/dev/null
  rm -f "${startup}"
  # The record first: a wait that fails runs `down`, which reads it.
  [ -d "${EVAL_FORGE_STATE_DIR}" ] || install -d -m 700 "${EVAL_FORGE_STATE_DIR}"
  printf '%s %s\n' "${FORGE_VM_NAME}" "${FORGE_VM_ZONE}" > "${FORGE_RECORD_FILE}"
  echo "✓ ${FORGE_VM_NAME} created"
}

# Readiness, read from the serial console: the startup script's own check
# through the loopback with the CA. `FORGE ready` within the deadline, or
# `FORGE failed`, or the deadline; the last lines are shown on either failure.
wait_for_serial_ready() { # <timeout seconds>
  local deadline=$(( SECONDS + $1 )) serial failed_reads=0 last_read_error=""
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    # A read that fails is not an empty console: it is counted, and its error
    # is what the deadline message shows, so a refused read is not blamed on
    # the VM.
    if ! serial="$(gcloud compute instances get-serial-port-output "${FORGE_VM_NAME}" --zone="${FORGE_VM_ZONE}" --project="${PROJECT_ID}" 2>&1)"; then
      failed_reads=$((failed_reads + 1)); last_read_error="$(tail -n 1 <<<"${serial}")"; serial=""
    fi
    if grep -q 'FORGE ready' <<<"${serial}"; then
      echo "✓ ${FORGE_VM_NAME} reports ready: $(grep -oE 'FORGE (ready|header:).*' <<<"${serial}" | tr '\n' ' ')"
      return 0
    fi
    if grep -q 'FORGE failed' <<<"${serial}"; then
      echo "ERROR: ${FORGE_VM_NAME}'s startup failed: $(grep -oE 'FORGE failed.*' <<<"${serial}" | tail -1)" >&2
      grep -E 'FORGE|startup-script' <<<"${serial}" | tail -n "${FORGE_SERIAL_TAIL_LINES}" >&2
      return 1
    fi
    sleep "${FORGE_READY_POLL_SECONDS}"
  done
  echo "ERROR: ${FORGE_VM_NAME} did not report ready within $1s; the serial console's last lines follow." >&2
  if [ "${failed_reads}" -gt 0 ]; then
    echo "ERROR: ${failed_reads} serial-console read(s) failed; the last error: ${last_read_error}" >&2
  fi
  gcloud compute instances get-serial-port-output "${FORGE_VM_NAME}" --zone="${FORGE_VM_ZONE}" --project="${PROJECT_ID}" 2>/dev/null \
    | grep -E 'FORGE|startup-script' | tail -n "${FORGE_SERIAL_TAIL_LINES}" >&2 || true
  return 1
}

# The server key (and the tokens, on gitlab-ce) reached the VM through
# instance metadata, which compute.instances.get returns and the agent under
# test holds (compute.viewer in the leased project). Once the forge is up they
# live on the VM's disk alone; this runs before the deploy, so no agent exists
# while they are there. The insert's audit entry carries no metadata values
# (read on a dev project's instances.insert entry, 2026-10-28).
remove_key_from_metadata() {
  gcloud compute instances remove-metadata "${FORGE_VM_NAME}" --zone="${FORGE_VM_ZONE}" --project="${PROJECT_ID}" \
    --keys="${FORGE_ROW_SECRET_METADATA_KEYS}" >/dev/null || {
    echo "ERROR: could not remove ${FORGE_ROW_SECRET_METADATA_KEYS} from ${FORGE_VM_NAME}'s metadata; the key must not stay readable there." >&2
    return 1
  }
  echo "✓ ${FORGE_ROW_SECRET_METADATA_KEYS} removed from ${FORGE_VM_NAME}'s metadata; the secrets are on the VM's disk alone"
}

# The path the run depends on: a pod in the host cluster resolves the host
# through the private zone, crosses the firewall rule and verifies the proxy
# with the CA. One curl, one disposable pod. FORGE_SKIP_CLUSTER_CHECK=1 for a
# project with no host cluster yet.
check_from_cluster() {
  if [ "${FORGE_SKIP_CLUSTER_CHECK:-}" = "1" ]; then
    echo "Forge up: in-cluster reachability check skipped (FORGE_SKIP_CLUSTER_CHECK=1)"
    return 0
  fi
  local pod code
  pod="forge-check-$(date +%s)"
  gke_dns_endpoint_flag "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}"
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --region "${REGION}" --project "${PROJECT_ID}" --quiet \
    ${GKE_DNS_ENDPOINT_FLAG} >/dev/null || {
    echo "ERROR: could not fetch credentials for ${CLUSTER_NAME} in ${PROJECT_ID}; not running the in-cluster check against another context (FORGE_SKIP_CLUSTER_CHECK=1 for a project with no host cluster yet)." >&2
    return 1
  }
  code="$(kubectl run "${pod}" -n "${FORGE_CLUSTER_CHECK_NAMESPACE}" --image="${FORGE_CLUSTER_CHECK_IMAGE}" \
    --restart=Never --rm -i --quiet --pod-running-timeout="${FORGE_CLUSTER_CHECK_TIMEOUT}" \
    --env="FORGE_CA=$(cat "${FORGE_CERT_DIR}/ca.crt")" --env="FORGE_URL=https://${EVAL_FORGE_HOST}${FORGE_ROW_READY_PATH}" \
    --command -- sh -c 'printf "%s\n" "$FORGE_CA" > /tmp/ca.crt && curl -sS --cacert /tmp/ca.crt --max-time 20 -o /dev/null -w "%{http_code}" "$FORGE_URL"' \
    | tr -d '\r\n' | tail -c 3)" || true # curl exits non-zero with its 000 on a connection failure; keep what stdout held
  if [ "${code}" != "200" ]; then
    echo "ERROR: a pod in ${CLUSTER_NAME} could not reach https://${EVAL_FORGE_HOST}${FORGE_ROW_READY_PATH} with the CA (got '${code:-nothing}'); the zone, the firewall rule or the VM is wrong, and the deploy would fail after the build." >&2
    return 1
  fi
  echo "✓ A pod in ${CLUSTER_NAME} reaches https://${EVAL_FORGE_HOST}${FORGE_ROW_READY_PATH} with the CA (200)"
}

# The tunnel (gitlab-ce): the eval's probe, resets and grader call the private
# host from the job pod by its bare name, so the host must answer on the
# pod's loopback 443. A relay pod in the host cluster forwards to the forge,
# a port-forward brings it to the loopback and stays up until `down`, and the
# hosts file maps the name. The cluster credentials are the check's.
open_tunnel() {
  local code deadline
  if [ "${FORGE_SKIP_CLUSTER_CHECK:-}" = "1" ]; then
    gke_dns_endpoint_flag "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}"
    # shellcheck disable=SC2086
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --region "${REGION}" --project "${PROJECT_ID}" --quiet ${GKE_DNS_ENDPOINT_FLAG} >/dev/null || {
      echo "ERROR: could not fetch credentials for ${CLUSTER_NAME} in ${PROJECT_ID}; the gitlab-ce row needs the host cluster for its tunnel." >&2
      return 1
    }
  fi
  kubectl delete pod "${FORGE_RELAY_POD}" -n "${FORGE_CLUSTER_CHECK_NAMESPACE}" --ignore-not-found --wait=true >/dev/null
  kubectl run "${FORGE_RELAY_POD}" -n "${FORGE_CLUSTER_CHECK_NAMESPACE}" --image="${FORGE_RELAY_IMAGE}" --restart=Never \
    --labels="app.kubernetes.io/part-of=kube-agents-eval,${FORGE_VM_LABEL_KEY}=${EVAL_FORGE}" --port="${FORGE_RELAY_PORT}" \
    -- "TCP-LISTEN:${FORGE_RELAY_PORT},fork,reuseaddr" "TCP:${FORGE_IP}:443" >/dev/null || {
    echo "ERROR: could not create the relay pod ${FORGE_RELAY_POD} in ${CLUSTER_NAME}." >&2
    return 1
  }
  kubectl wait --for=condition=Ready "pod/${FORGE_RELAY_POD}" -n "${FORGE_CLUSTER_CHECK_NAMESPACE}" --timeout="${FORGE_CLUSTER_CHECK_TIMEOUT}" >/dev/null || {
    echo "ERROR: the relay pod ${FORGE_RELAY_POD} did not become Ready within ${FORGE_CLUSTER_CHECK_TIMEOUT}." >&2
    return 1
  }
  [ -d "${EVAL_FORGE_STATE_DIR}" ] || install -d -m 700 "${EVAL_FORGE_STATE_DIR}"
  kubectl config current-context > "${FORGE_TUNNEL_CONTEXT_FILE}" 2>/dev/null || true
  # A port-forward dies with its API stream (a control-plane roll, the relay's
  # node); the keeper restarts it for the run's length, and `down` kills both.
  nohup bash -c 'while :; do kubectl port-forward -n "$1" "pod/$2" --address 127.0.0.1 "$3:$4"; sleep "$5"; done' _ \
    "${FORGE_CLUSTER_CHECK_NAMESPACE}" "${FORGE_RELAY_POD}" "${FORGE_TUNNEL_LOCAL_PORT}" "${FORGE_RELAY_PORT}" "${FORGE_TUNNEL_RESTART_SECONDS}" \
    > "${FORGE_TUNNEL_LOG_FILE}" 2>&1 &
  echo "$!" > "${FORGE_TUNNEL_PID_FILE}"
  if ! grep -qxF "$(hosts_entry)" "${FORGE_TUNNEL_HOSTS_FILE}" 2>/dev/null; then
    [ -w "${FORGE_TUNNEL_HOSTS_FILE}" ] || {
      echo "ERROR: ${FORGE_TUNNEL_HOSTS_FILE} is not writable, so ${EVAL_FORGE_HOST} cannot point at the tunnel; run as root, or set FORGE_TUNNEL_HOSTS_FILE to a hosts file the resolver reads." >&2
      return 1
    }
    printf '%s\n' "$(hosts_entry)" >> "${FORGE_TUNNEL_HOSTS_FILE}"
  fi
  deadline=$(( SECONDS + FORGE_TUNNEL_READY_SECONDS ))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    code="$(curl -sS --cacert "${FORGE_CERT_DIR}/ca.crt" --resolve "${EVAL_FORGE_HOST}:${FORGE_TUNNEL_LOCAL_PORT}:127.0.0.1" --max-time "${FORGE_TUNNEL_CHECK_MAX_TIME}" -o /dev/null -w '%{http_code}' \
      "https://${EVAL_FORGE_HOST}${FORGE_ROW_READY_PATH}" 2>/dev/null)" || true
    [ "${code}" = "200" ] && break
    sleep "${FORGE_TUNNEL_POLL_SECONDS}"
  done
  if [ "${code:-}" != "200" ]; then
    echo "ERROR: the tunnel to ${EVAL_FORGE_HOST} did not answer on 127.0.0.1:${FORGE_TUNNEL_LOCAL_PORT} within ${FORGE_TUNNEL_READY_SECONDS}s (got '${code:-nothing}'); the port-forward's log: $(tail -n "${FORGE_TUNNEL_LOG_TAIL_LINES}" "${FORGE_TUNNEL_LOG_FILE}" 2>/dev/null | tr '\n' ' ')" >&2
    return 1
  fi
  echo "✓ Tunnel open: ${EVAL_FORGE_HOST} answers on 127.0.0.1:${FORGE_TUNNEL_LOCAL_PORT} through ${FORGE_RELAY_POD} (pid $(cat "${FORGE_TUNNEL_PID_FILE}"))"
}

# The manifest the remediation cases read, written to scratch for the VM's
# metadata: the provisioning script's seed, root commits it on the VM.
write_manifest_seed() {
  manifest_seed_lifted || { echo "ERROR: could not lift the manifest seed from scripts/provision_ci_pool_project.sh." >&2; exit 1; }
  printf '%s' "${GITOPS_WORKLOAD_MANIFEST_PATH}" > "${FORGE_CERT_DIR}/manifest-path"
  printf '%s' "${GITOPS_WORKLOAD_MANIFEST_MESSAGE}" > "${FORGE_CERT_DIR}/manifest-message"
  printf '%s\n' "${GITOPS_WORKLOAD_MANIFEST_CONTENT}" > "${FORGE_CERT_DIR}/manifest-content"
  echo "✓ ${GITOPS_WORKLOAD_MANIFEST_PATH} staged for the VM to seed into ${FORGE_GITLAB_PROJECT}"
}

write_state_dir() {
  install -m 644 "${FORGE_CERT_DIR}/ca.crt" "${FORGE_CA_FILE}"
  if [ "${EVAL_FORGE_KIND}" = "gitlab" ]; then
    install -m 600 "${FORGE_CERT_DIR}/${FORGE_AGENT_TOKEN_FILE_NAME}" "${FORGE_AGENT_TOKEN_FILE}"
    install -m 600 "${FORGE_CERT_DIR}/${FORGE_LEDGER_TOKEN_FILE_NAME}" "${FORGE_LEDGER_TOKEN_FILE}"
  fi
  rm -rf "${FORGE_CERT_DIR}"
  echo "✓ ${FORGE_CA_FILE} written$([ "${EVAL_FORGE_KIND}" = "gitlab" ] && echo ", with ${FORGE_AGENT_TOKEN_FILE_NAME} and ${FORGE_LEDGER_TOKEN_FILE_NAME}"); the deploy and the eval read them from there"
}

forge_up() {
  local started=${SECONDS}
  # The server key and the startup script live in scratch until the VM has
  # them; no exit, refused or failed, leaves either behind.
  trap 'rm -rf "${FORGE_CERT_DIR:-}" "${FORGE_STARTUP_FILE:-}"' EXIT
  echo "=== [$(stamp)] Forge up: ${EVAL_FORGE} at ${EVAL_FORGE_HOST} in ${PROJECT_ID} (${FORGE_VM_NAME}) ==="
  if [ -e "${FORGE_CA_FILE}" ]; then
    echo "ERROR: ${FORGE_CA_FILE} already exists; a forge from an earlier run is still recorded in ${EVAL_FORGE_STATE_DIR}. Run '$0 down' first, so no stale CA reaches the deploy." >&2
    exit 1
  fi
  # The row: what the VM runs, how long it may take, what rides in metadata.
  local ready_timeout group suffix
  case "${EVAL_FORGE_KIND}" in
    github)
      FORGE_ROW_MACHINE_TYPE="${GHES_PROXY_MACHINE_TYPE}"; FORGE_ROW_DISK_FLAG=""
      FORGE_ROW_STARTUP_FN=ghes_startup_script; FORGE_ROW_READY_PATH="${GHES_READY_PATH}"
      FORGE_ROW_SECRET_METADATA_KEYS="${FORGE_KEY_METADATA_KEY}"; FORGE_ROW_METADATA_FILES=""
      ready_timeout="${GHES_READY_TIMEOUT_SECONDS}" ;;
    gitlab)
      read -r group suffix <<<"$(gitlab_project_lifted || true)"
      [ -n "${group:-}" ] && [ -n "${suffix:-}" ] || { echo "ERROR: could not lift GITLAB_FORGE_GROUP/GITLAB_FORGE_PROJECT_SUFFIX from hack/ci-deploy.sh; not creating a VM whose project the eval would not find." >&2; exit 1; }
      FORGE_GITLAB_PROJECT="${group}/${PROJECT_ID}${suffix}"
      FORGE_ROW_MACHINE_TYPE="${GITLAB_CE_MACHINE_TYPE}"; FORGE_ROW_DISK_FLAG="--boot-disk-size=${GITLAB_CE_BOOT_DISK}"
      FORGE_ROW_STARTUP_FN=gitlab_startup_script; FORGE_ROW_READY_PATH="${GITLAB_CE_READY_PATH}"
      FORGE_ROW_SECRET_METADATA_KEYS="${FORGE_KEY_METADATA_KEY},${FORGE_AGENT_TOKEN_METADATA_KEY},${FORGE_LEDGER_TOKEN_METADATA_KEY}"
      ready_timeout="${GITLAB_CE_READY_TIMEOUT_SECONDS}" ;;
  esac
  preflight_project_pieces
  make_certificates
  if [ "${EVAL_FORGE_KIND}" = "github" ]; then
    build_ghes_proxy_image
    FORGE_ROW_METADATA="image=${GHES_PROXY_IMAGE},enterprise-version=${GHES_PROXY_ENTERPRISE_VERSION}"
  else
    mint_gitlab_tokens
    write_manifest_seed
    FORGE_ROW_METADATA="image=${GITLAB_CE_IMAGE},group=${group},project=${PROJECT_ID}${suffix},bot-login=${GITLAB_CE_BOT_LOGIN},bot-email=${GITLAB_CE_BOT_EMAIL},token-days=${GITLAB_CE_TOKEN_DAYS}"
    FORGE_ROW_METADATA_FILES=",${FORGE_AGENT_TOKEN_METADATA_KEY}=${FORGE_CERT_DIR}/${FORGE_AGENT_TOKEN_FILE_NAME},${FORGE_LEDGER_TOKEN_METADATA_KEY}=${FORGE_CERT_DIR}/${FORGE_LEDGER_TOKEN_FILE_NAME}"
    FORGE_ROW_METADATA_FILES="${FORGE_ROW_METADATA_FILES},${FORGE_MANIFEST_PATH_METADATA_KEY}=${FORGE_CERT_DIR}/manifest-path,${FORGE_MANIFEST_MESSAGE_METADATA_KEY}=${FORGE_CERT_DIR}/manifest-message,${FORGE_MANIFEST_CONTENT_METADATA_KEY}=${FORGE_CERT_DIR}/manifest-content"
  fi
  delete_forge_vms "leftover forge"
  create_forge_vm
  # From here a failure takes the VM down again: the job's trap calls `down`
  # too, but a refusal here must not depend on it.
  if ! wait_for_serial_ready "${ready_timeout}" || ! remove_key_from_metadata || ! check_from_cluster; then
    forge_down
    exit 1
  fi
  if [ "${EVAL_FORGE_KIND}" = "gitlab" ] && ! open_tunnel; then
    forge_down
    exit 1
  fi
  write_state_dir
  echo "=== [$(stamp)] Forge up finished in $((SECONDS - started))s: ${EVAL_FORGE_HOST} is ${FORGE_VM_NAME} at ${FORGE_IP} ==="
}

case "${ACTION}" in
  up) forge_up ;;
  down) forge_down ;;
esac
