#!/usr/bin/env bash
# The lane step that stands a self-managed forge up for one eval run, and takes
# it down after (kube-agents#3682, #3684). A GitHub Enterprise Server row
# (`EVAL_FORGE=ghes`) gets the forwarding proxy in hack/ghes-proxy on a small
# VM in the leased project, at the host the forge table names, with a private
# CA made here and kept nowhere else. The two public rows need nothing and this
# script exits at once on them.
#
#   hack/ci-forge.sh up     before hack/ci-deploy.sh: makes the certificates,
#                           builds the proxy image into the project's registry,
#                           deletes any forge VM an earlier run left, creates
#                           this run's VM at the project's reserved address,
#                           waits until the proxy answers over TLS with the CA,
#                           proves a pod in the host cluster reaches it, and
#                           writes ca.crt into EVAL_FORGE_STATE_DIR, the file
#                           the deploy and the eval refuse to run without.
#   hack/ci-forge.sh down   after hack/ci-eval-pr.sh, from the job's cleanup
#                           trap whatever the eval's exit code: deletes the
#                           VM and the files `up` wrote. Idempotent.
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
# serial console (the proxy's self-check with the CA), and the path that
# matters, a pod in the host cluster to the forge, is proved with one curl
# from a pod there. Nothing here mints a token, reads a secret or touches the
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
# The metadata item the server key rides in to the VM, removed once it is up.
readonly FORGE_KEY_METADATA_KEY="tls-key"
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

# ─── down ─────────────────────────────────────────────────────────────────────
forge_down() {
  local started=${SECONDS} name zone
  echo "=== [$(stamp)] Forge down: ${FORGE_VM_NAME} (${EVAL_FORGE} at ${EVAL_FORGE_HOST}) in ${PROJECT_ID} ==="
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
  rm -f "${FORGE_CA_FILE}" "${FORGE_RECORD_FILE}"
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

# The ephemeral external address is the VM's way to github.com: the pool
# projects have no Cloud NAT, and the proxy forwards every request there. The
# deny rule on the tag and the blocked project ssh keys are what keep that
# address from answering anything but 443 from the VPC.
create_ghes_vm() {
  local startup
  startup="$(mktemp "${FORGE_SCRATCH_DIR}/forge-startup.XXXXXX")"
  FORGE_STARTUP_FILE="${startup}"
  ghes_startup_script > "${startup}"
  echo "=== [$(stamp)] Creating ${FORGE_VM_NAME} (${GHES_PROXY_MACHINE_TYPE}, ${FORGE_VM_ZONE}, ${FORGE_IP}, ephemeral external address) ==="
  gcloud compute instances create "${FORGE_VM_NAME}" \
    --project="${PROJECT_ID}" --zone="${FORGE_VM_ZONE}" \
    --machine-type="${GHES_PROXY_MACHINE_TYPE}" \
    --image-family="${FORGE_VM_IMAGE_FAMILY}" --image-project="${FORGE_VM_IMAGE_PROJECT}" \
    --network="${FORGE_VM_NETWORK}" --subnet="${FORGE_VM_NETWORK}" \
    --private-network-ip="${FORGE_IP}" \
    --service-account="${FORGE_VM_SERVICE_ACCOUNT}" --scopes=cloud-platform \
    --tags="${FORGE_NETWORK_TAG}" \
    --labels="${FORGE_VM_LABEL_KEY}=${EVAL_FORGE},${FORGE_VM_BUILD_LABEL_KEY}=${BUILD_ID:-local}" \
    --metadata="host=${EVAL_FORGE_HOST},image=${GHES_PROXY_IMAGE},enterprise-version=${GHES_PROXY_ENTERPRISE_VERSION},${FORGE_VM_METADATA_SSH}" \
    --metadata-from-file="startup-script=${startup},tls-crt=${FORGE_CERT_DIR}/tls.crt,${FORGE_KEY_METADATA_KEY}=${FORGE_CERT_DIR}/tls.key,ca-crt=${FORGE_CERT_DIR}/ca.crt" \
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

# The server key reached the VM through instance metadata, which
# compute.instances.get returns and the agent under test holds (compute.viewer
# in the leased project). Once the proxy is up the key lives on the VM's disk
# alone; this runs before the deploy, so no agent exists while it is there.
remove_key_from_metadata() {
  gcloud compute instances remove-metadata "${FORGE_VM_NAME}" --zone="${FORGE_VM_ZONE}" --project="${PROJECT_ID}" \
    --keys="${FORGE_KEY_METADATA_KEY}" >/dev/null || {
    echo "ERROR: could not remove ${FORGE_KEY_METADATA_KEY} from ${FORGE_VM_NAME}'s metadata; the key must not stay readable there." >&2
    return 1
  }
  echo "✓ ${FORGE_KEY_METADATA_KEY} removed from ${FORGE_VM_NAME}'s metadata; the key is on the VM's disk alone"
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
    --env="FORGE_CA=$(cat "${FORGE_CERT_DIR}/ca.crt")" --env="FORGE_URL=https://${EVAL_FORGE_HOST}${GHES_READY_PATH}" \
    --command -- sh -c 'printf "%s\n" "$FORGE_CA" > /tmp/ca.crt && curl -sS --cacert /tmp/ca.crt --max-time 20 -o /dev/null -w "%{http_code}" "$FORGE_URL"' \
    | tr -d '\r\n' | tail -c 3)" || true # curl exits non-zero with its 000 on a connection failure; keep what stdout held
  if [ "${code}" != "200" ]; then
    echo "ERROR: a pod in ${CLUSTER_NAME} could not reach https://${EVAL_FORGE_HOST}${GHES_READY_PATH} with the CA (got '${code:-nothing}'); the zone, the firewall rule or the VM is wrong, and the deploy would fail after the build." >&2
    return 1
  fi
  echo "✓ A pod in ${CLUSTER_NAME} reaches https://${EVAL_FORGE_HOST}${GHES_READY_PATH} with the CA (200)"
}

write_state_dir() {
  install -m 644 "${FORGE_CERT_DIR}/ca.crt" "${FORGE_CA_FILE}"
  rm -rf "${FORGE_CERT_DIR}"
  echo "✓ ${FORGE_CA_FILE} written; the deploy and the eval read it from there"
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
  case "${EVAL_FORGE_KIND}" in
    github) ;;
    *)
      echo "ERROR: EVAL_FORGE=${EVAL_FORGE} needs a ${EVAL_FORGE_KIND} forge built per run, which this script does not do yet (kube-agents#3683)." >&2
      exit 1 ;;
  esac
  preflight_project_pieces
  make_certificates
  build_ghes_proxy_image
  delete_forge_vms "leftover forge"
  create_ghes_vm
  # From here a failure takes the VM down again: the job's trap calls `down`
  # too, but a refusal here must not depend on it.
  if ! wait_for_serial_ready "${GHES_READY_TIMEOUT_SECONDS}" || ! remove_key_from_metadata || ! check_from_cluster; then
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
