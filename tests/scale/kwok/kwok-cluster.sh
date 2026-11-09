#!/usr/bin/env bash
# tests/scale/kwok/kwok-cluster.sh -- a pinned KWOK cluster for the scale lane.
#
# Creates, inspects and removes a local KWOK cluster (binary runtime, so no
# container engine) that the lane seeds with k8s-operator/cmd/kwok-seed and
# measures with baseline.sh. Every CLI call here passes --kubeconfig; nothing
# reads or writes ~/.kube/config. The context kwokctl writes is kwok-NAME.
#
# Usage:
#   tests/scale/kwok/kwok-cluster.sh install-tools
#   tests/scale/kwok/kwok-cluster.sh up NAME
#   tests/scale/kwok/kwok-cluster.sh down NAME
#   tests/scale/kwok/kwok-cluster.sh kubeconfig NAME
#   tests/scale/kwok/kwok-cluster.sh ca NAME
#   tests/scale/kwok/kwok-cluster.sh server NAME
#   tests/scale/kwok/kwok-cluster.sh dir NAME    (kwokctl's directory for the cluster: pids/, logs/, pki/)
#   tests/scale/kwok/kwok-cluster.sh pins        (prints kwok-<version>-k8s-<version>, the cache key stem)
#
# Environment:
#   KWOK_WORKDIR            default: $HOME/.kwok (kwokctl's state and its download cache)
#   KWOK_STATE_DIR          default: $KWOK_WORKDIR/kube-agents (holds NAME.kubeconfig)
#   KWOK_TOOLS_DIR          default: $KWOK_STATE_DIR/bin (install-tools puts kwokctl and kwok here;
#                           every subcommand prepends it to PATH)
#   KWOK_EXTRA_CREATE_ARGS  default: empty (extra flags appended to kwokctl create cluster)
set -euo pipefail

# The two kwok binaries and their release checksums. Bump all three together.
# The workflow's download cache is keyed on `pins`, which prints this version
# and KWOK_KUBE_VERSION, so a bump here misses the old cache by itself.
readonly KWOK_VERSION="v0.8.0"
readonly KWOKCTL_SHA256="d5743166c657283d6e827c7c4c20cacfd9009fd94830f06718ca7b2c7099461f"
readonly KWOK_SHA256="f5a4385a2ae1b9dd7f8acae18e002a75c0ebba124ae9d7259aa8dadb50711f46"
readonly KWOK_RELEASE_URL="https://github.com/kubernetes-sigs/kwok/releases/download"
# The control-plane release kwokctl downloads into its cache; kwokctl reads the
# version from this environment variable (it has no flag for it).
readonly KWOK_KUBE_VERSION="v1.36.1"
readonly DOWNLOAD_RETRIES=5
readonly CREATE_WAIT="3m"
readonly CREATE_TIMEOUT="6m"
# The API server's default Event TTL is 1h; a seeded cluster must keep its
# Events for the whole of a run.
readonly EVENT_TTL="24h"
# kwokctl's API server allocates Service IPs from a /24 (measured: the 254th
# Service of the large-cluster profile failed with "range is full"); a /16
# holds the 4,000 of that profile with room for larger ones. It must not
# overlap the generator's pod CIDRs (10.0.0.0/24 upwards, one per node).
readonly SERVICE_CIDR="10.96.0.0/16"
# kwok-controller plays its node and pod stages (the ones that mark a Node
# Ready and a Pod Running) with four workers each by default, about 60 pods a
# second: 60,000 seeded pods would wait 17 minutes after the last create. At
# 64 workers the controller keeps pace with the seeder (measured: 3,000 pods
# all Running 1 s after the last create, against 34 s at the default). The
# file is written into KWOK_STATE_DIR and handed to kwokctl with --config.
readonly KWOK_STAGE_PARALLELISM=64
readonly KWOK_NODE_LEASE_PARALLELISM=16
readonly KWOK_OPTIONS_FILE_NAME="kwok-options.yaml"
# Only linux/amd64 release assets are pinned above.
readonly SUPPORTED_OS="linux"
readonly SUPPORTED_ARCH="x86_64"
readonly ASSET_SUFFIX="linux-amd64"
readonly CONTEXT_PREFIX="kwok-"
readonly NAME_PATTERN='^[a-z0-9][a-z0-9-]*$'
readonly READYZ_PATH="/readyz"
readonly READYZ_OK="ok"
# Relative to the cluster's directory under KWOK_WORKDIR/clusters.
readonly CONTROLLER_PID_FILE="pids/kwok-controller.pid"
readonly KWOK_WORKDIR="${KWOK_WORKDIR:-$HOME/.kwok}"
readonly KWOK_STATE_DIR="${KWOK_STATE_DIR:-$KWOK_WORKDIR/kube-agents}"
readonly KWOK_TOOLS_DIR="${KWOK_TOOLS_DIR:-$KWOK_STATE_DIR/bin}"
readonly KWOK_EXTRA_CREATE_ARGS="${KWOK_EXTRA_CREATE_ARGS:-}"
export KWOK_WORKDIR KWOK_KUBE_VERSION
export PATH="$KWOK_TOOLS_DIR:$PATH"

usage() { sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH (PATH starts with $KWOK_TOOLS_DIR; run install-tools for kwokctl and kwok)"
  done
}

kubeconfig_path() { echo "$KWOK_STATE_DIR/$1.kubeconfig"; }
cluster_dir() { echo "$KWOK_WORKDIR/clusters/$1"; }
ca_path() { echo "$(cluster_dir "$1")/pki/ca.crt"; }
cluster_exists() { kwokctl get clusters 2>/dev/null | grep -qx "$1"; }

# fetch_tool NAME SHA256: download NAME-linux-amd64 for KWOK_VERSION into
# KWOK_TOOLS_DIR unless the binary there already matches the pinned checksum
# (a stronger test than its --version, which a damaged binary can still print).
fetch_tool() {
  local name="$1" sum="$2" dest asset
  dest="$KWOK_TOOLS_DIR/$name"
  asset="$name-$ASSET_SUFFIX"
  if [ -x "$dest" ] && echo "$sum  $dest" | sha256sum -c --quiet - >/dev/null 2>&1; then
    log "$name $KWOK_VERSION is already in $KWOK_TOOLS_DIR (sha256 verified)"
    return 0
  fi
  log "downloading $asset $KWOK_VERSION into $KWOK_TOOLS_DIR"
  curl -fsSL --retry "$DOWNLOAD_RETRIES" --retry-all-errors -o "$dest.download" "$KWOK_RELEASE_URL/$KWOK_VERSION/$asset"
  if ! echo "$sum  $dest.download" | sha256sum -c --quiet -; then
    rm -f "$dest.download"
    die "$asset does not match the pinned sha256 $sum"
  fi
  chmod +x "$dest.download"
  mv "$dest.download" "$dest"
  log "$name $KWOK_VERSION installed, sha256 verified"
}

install_tools() {
  local os arch
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  if [ "$os" != "$SUPPORTED_OS" ] || [ "$arch" != "$SUPPORTED_ARCH" ]; then
    die "install-tools pins $ASSET_SUFFIX binaries only and this host is $os/$arch; put kwokctl and kwok $KWOK_VERSION on PATH yourself"
  fi
  need curl sha256sum
  mkdir -p "$KWOK_TOOLS_DIR"
  fetch_tool kwokctl "$KWOKCTL_SHA256"
  fetch_tool kwok "$KWOK_SHA256"
}

server_url() {
  local kc
  kc="$(kubeconfig_path "$1")"
  [ -f "$kc" ] || die "$kc does not exist; run '$0 up $1' first"
  need kubectl
  kubectl config view --kubeconfig "$kc" --context "$CONTEXT_PREFIX$1" --minify -o jsonpath='{.clusters[0].cluster.server}'
}

up() {
  local name="$1" kc ctx controller_pid_file options_file
  need kwokctl kwok kubectl
  kc="$(kubeconfig_path "$name")"
  ctx="$CONTEXT_PREFIX$name"
  mkdir -p "$KWOK_STATE_DIR"
  # An existing cluster is checked the way a new one is, below: a first `up`
  # that died after kwokctl created it leaves the cluster listed, and a reboot
  # leaves it listed with nothing running.
  if cluster_exists "$name"; then
    log "kwok cluster $name already exists; checking it"
    [ -f "$kc" ] || die "$kc is missing for the existing cluster $name; run '$0 down $name' and then 'up $name' to recreate it"
  else
    log "creating kwok cluster $name (Kubernetes $KWOK_KUBE_VERSION, kwok $KWOK_VERSION, binary runtime, workdir $KWOK_WORKDIR)"
    options_file="$KWOK_STATE_DIR/$KWOK_OPTIONS_FILE_NAME"
    cat >"$options_file" <<EOF
apiVersion: config.kwok.x-k8s.io/v1alpha1
kind: KwokConfiguration
options:
  podPlayStageParallelism: $KWOK_STAGE_PARALLELISM
  nodePlayStageParallelism: $KWOK_STAGE_PARALLELISM
  nodeLeaseParallelism: $KWOK_NODE_LEASE_PARALLELISM
EOF
    # The scheduler is off because the seeder places every pod itself through
    # spec.nodeName; kwok-controller still marks the pods Running.
    # shellcheck disable=SC2086 # KWOK_EXTRA_CREATE_ARGS is a space-separated flag list by contract.
    kwokctl create cluster --name "$name" --runtime binary --kubeconfig "$kc" \
      --config "$options_file" \
      --disable kube-scheduler \
      --extra-args "kube-apiserver=event-ttl=$EVENT_TTL" \
      --extra-args "kube-apiserver=service-cluster-ip-range=$SERVICE_CIDR" \
      --kwok-controller-binary "$(command -v kwok)" \
      --quiet-pull --wait "$CREATE_WAIT" --timeout "$CREATE_TIMEOUT" $KWOK_EXTRA_CREATE_ARGS
  fi
  [ "$(kubectl --kubeconfig "$kc" --context "$ctx" get --raw "$READYZ_PATH")" = "$READYZ_OK" ] \
    || die "cluster $name is up but $READYZ_PATH did not answer $READYZ_OK; run '$0 down $name' and then 'up $name'"
  # kwokctl's --wait covers the API server only. A kwok-controller that died
  # at start (an unknown flag through KWOK_EXTRA_CREATE_ARGS, for one) leaves
  # a cluster whose pods never turn Running, which the seeder would report as
  # a timeout twenty minutes later.
  controller_pid_file="$(cluster_dir "$name")/$CONTROLLER_PID_FILE"
  if ! [ -f "$controller_pid_file" ] || ! kill -0 "$(cat "$controller_pid_file")" 2>/dev/null; then
    die "cluster $name is up but kwok-controller is not running (see $(cluster_dir "$name")/logs/); run '$0 down $name' and then 'up $name'"
  fi
  log "cluster $name is ready at $(server_url "$name") (context $ctx, kubeconfig $kc)"
}

down() {
  local name="$1" kc scratch
  need kwokctl
  kc="$(kubeconfig_path "$name")"
  if cluster_exists "$name"; then
    log "deleting kwok cluster $name"
    if [ -f "$kc" ]; then
      kwokctl delete cluster --name "$name" --kubeconfig "$kc"
    else
      # kwokctl prunes the cluster's context from --kubeconfig, and its default
      # is ~/.kube/config. With no state file left, hand it a scratch file.
      scratch="$(mktemp)"
      kwokctl delete cluster --name "$name" --kubeconfig "$scratch"
      rm -f "$scratch"
    fi
  else
    log "no kwok cluster named $name"
  fi
  # The kubeconfig, and what baseline.sh keyed on the cluster's name beside
  # it: the proxy's tokens, its TLS pair and the caller kubeconfig. The files
  # are NAME.<what>, and NAME_PATTERN admits no dot, so `down scale` cannot
  # reach a sibling cluster's `scale-2.*` files.
  rm -f "$KWOK_STATE_DIR/$name."*
}

pins() { echo "kwok-$KWOK_VERSION-k8s-$KWOK_KUBE_VERSION"; }

[ $# -ge 1 ] || { usage >&2; exit 2; }
case "$1" in
  -h|--help) usage; exit 0 ;;
  install-tools)
    [ $# -eq 1 ] || die "install-tools takes no argument"
    install_tools
    ;;
  pins)
    [ $# -eq 1 ] || die "pins takes no argument"
    pins
    ;;
  up|down|kubeconfig|ca|server|dir)
    [ $# -eq 2 ] || die "$1 needs exactly one argument, the cluster NAME"
    [[ "$2" =~ $NAME_PATTERN ]] || die "cluster NAME must match $NAME_PATTERN"
    case "$1" in
      up) up "$2" ;;
      down) down "$2" ;;
      dir)
        [ -d "$(cluster_dir "$2")" ] || die "$(cluster_dir "$2") does not exist; is cluster $2 up in $KWOK_WORKDIR?"
        cluster_dir "$2"
        ;;
      kubeconfig)
        [ -f "$(kubeconfig_path "$2")" ] || die "$(kubeconfig_path "$2") does not exist; run '$0 up $2' first"
        kubeconfig_path "$2"
        ;;
      ca)
        [ -f "$(ca_path "$2")" ] || die "$(ca_path "$2") does not exist; is cluster $2 up in $KWOK_WORKDIR?"
        ca_path "$2"
        ;;
      server) server_url "$2"; echo ;;
    esac
    ;;
  *) echo "unknown subcommand: $1" >&2; usage >&2; exit 2 ;;
esac
