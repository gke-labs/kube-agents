# shellcheck shell=bash disable=SC2034
# Sourced by run.sh, which reads CHANNEL, START, CREATE_FLAGS and POOL_FLAGS and calls plant, before, break_it and after.
# 10: version skew: the pool stays at 1.31 while the master climbs; what GKE allows, and an old kubectl
CHANNEL=EXTENDED; START=1.31; POOL_FLAGS="--num-nodes 1 --machine-type e2-standard-2"
OLD_KUBECTL_VERSION=v1.29.0; KUBECTL_RELEASES=https://dl.k8s.io/release
# old_kubectl: fetch the kubectl release for this host's OS and CPU into the gitignored .bin/, check it against the
# release's published SHA-256, and run it against the cluster.
old_kubectl(){ local os arch url bin="$H/.bin/kubectl-$OLD_KUBECTL_VERSION"; os=$(uname -s | tr '[:upper:]' '[:lower:]')
  case $(uname -m) in x86_64|amd64) arch=amd64 ;; arm64|aarch64) arch=arm64 ;; *) echo "no kubectl build for $(uname -m)" >&2; return 1 ;; esac
  url="$KUBECTL_RELEASES/$OLD_KUBECTL_VERSION/bin/$os/$arch/kubectl"
  if [ ! -x "$bin" ]; then mkdir -p "$H/.bin"; curl -fsSL -o "$bin.part" "$url" &&
      [ "$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$bin.part")" = "$(curl -fsSL "$url.sha256")" ] ||
      { rm -f "$bin.part"; echo "could not fetch or verify $url" >&2; return 1; }
    chmod +x "$bin.part"; mv "$bin.part" "$bin"; fi
  "$bin" --context "$CTX" get nodes; }
plant(){ pause_deploy steady 1; }
before(){ ev skew before G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version)'; }
break_it(){ upgrade_master "$(newest_patch EXTENDED 1.32)"; upgrade_master "$(newest_patch EXTENDED 1.33)"; ev skew two-behind G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version,status)'; local V; V=$(newest_patch EXTENDED 1.34); require_version "$V"; note skew "attempt: master -> $V with the pools three minors behind"; attempt_refusal skew ev skew three-behind-attempt G container clusters upgrade "$CLUSTER" --master --cluster-version "$V" --zone "$ZONE" --quiet --timeout "$MASTER_UPGRADE_TIMEOUT"; wait_ops; }
after(){ ev skew after G container clusters describe "$CLUSTER" --zone "$ZONE" --format='value(currentMasterVersion)'; ev skew pools-after G container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='table(name,version,status)'; ev skew old-kubectl old_kubectl; ev skew steady K -n scen get pods -o wide; }
