#!/usr/bin/env bash
# Smoke test for the agent shell sandbox image: starts a container, connects to
# it the way the agent pod does, and checks the things this image exists to
# provide.
#
# Worth having as a file rather than a checklist because none of it is visible
# from the Dockerfile. Whether a variable reaches the agent's shell depends on
# sshd's parser, the session type, and which of three mechanisms sets it; the
# first version of this image got PATH right and CREDENTIAL_PROXY_URL wrong,
# and every static check in the repository passed on it.
#
# Usage: deploy/sandbox/smoke-test.sh [image] [port]
#
# Needs Docker 26 or later: the image trees are mounted the way the operator
# mounts them, as read-only subpaths of the data volume, and `volume-subpath`
# arrived in 26.0.
#
# shellcheck disable=SC2016
#   Remote commands are single-quoted throughout and that is the point: the
#   expansion has to happen in the sandbox, not in this shell. A double-quoted
#   `echo "$PATH"` would test the caller's PATH and pass.
#
# No `set -e`: a failing check is data this script reports, not a reason to
# abandon the run. `check` counts them and the exit status at the bottom is the
# verdict.
set -uo pipefail

readonly MIN_DOCKER_MAJOR=26
# The operator's layout, from shellSandboxImageTrees, shellSandboxImageTreeHomes
# and the ancestor pins it derives in
# k8s-operator/internal/controller/shell_sandbox_manifests.go. "" is the data
# root. Each pin is a home's ancestor mounted over itself so it cannot be
# renamed aside with the read-only trees inside it.
readonly IMAGE_TREES=(skills scripts governance)
readonly IMAGE_TREE_HOMES=("" profiles/platform)
readonly IMAGE_TREE_PINS=(profiles profiles/platform)
# shellSandboxImageTreesMode: what the operator sets SANDBOX_IMAGE_TREES to.
readonly IMAGE_TREES_MODE=read-only-mounts

IMAGE="${1:-agent-sandbox:latest}"
PORT="${2:-12222}"
NAME="sandbox-smoke-$$"

# Checked before anything is created, so an old daemon costs a message and
# nothing to clean up. Without volume-subpath the main run fails with a --mount
# parse error that says nothing about the version.
docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
docker_major=${docker_version%%.*}
if ! [[ "$docker_major" =~ ^[0-9]+$ ]] || [ "$docker_major" -lt "$MIN_DOCKER_MAJOR" ]; then
  echo "this smoke test needs Docker $MIN_DOCKER_MAJOR or later (--mount volume-subpath);" >&2
  echo "the daemon reports '${docker_version:-nothing, is it running?}'" >&2
  exit 1
fi

WORK=$(mktemp -d)
# Named volumes rather than bind mounts, for the ownership. A bind mount arrives
# owned by whoever ran this script; a named volume is seeded from the image, so
# /opt/data arrives agent-owned and /var/lib/sandbox-sshd root-owned — which is
# what a PVC does and what the entrypoint's root-ownership check expects. It
# also means nothing here has to chown a 0700 directory back out of the
# container before `rm -rf` can finish.
DATA_VOL="$NAME-data"
SSHD_VOL="$NAME-sshd"
PASS=0
FAIL=0

cleanup() {
  docker rm -f "$NAME" "$NAME-nourl" "$NAME-badsshd" "$NAME-prepare" "$NAME-plant" \
    "$NAME-nomounts" >/dev/null 2>&1
  docker volume rm -f "$DATA_VOL" "$SSHD_VOL" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

check() { # check <label> <expected-substring> <actual>
  if [[ -n "$2" && "$3" == *"$2"* ]]; then
    echo "PASS  $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $1"
    # An empty expectation matches everything, so it is a broken assertion
    # rather than a passing one. Say which, or it reads as a real failure.
    [ -n "$2" ] || echo "        (empty expectation — the assertion is wrong, not the image)"
    echo "        want substring: $2"
    echo "        got: $3"
    FAIL=$((FAIL + 1))
  fi
}

check_absent() { # check_absent <label> <forbidden-substring> <actual>
  if [[ "$3" != *"$2"* ]]; then
    echo "PASS  $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $1"
    echo "        must not contain: $2"
    echo "        got: $3"
    FAIL=$((FAIL + 1))
  fi
}

ssh-keygen -q -t ed25519 -N '' -f "$WORK/id" -C sandbox-smoke
mkdir -p "$WORK/keys"
cp "$WORK/id.pub" "$WORK/keys/authorized_keys"
chmod 644 "$WORK/keys/authorized_keys"

# IdentitiesOnly: without it ssh also offers every key in the caller's agent, and
# a refused login comes back as "Too many authentication failures" — which passes
# a naive check for a refusal while proving nothing about why.
SSH_OPTS=(-i "$WORK/id" -p "$PORT" -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=5)
SSH=(ssh "${SSH_OPTS[@]}" agent@127.0.0.1)

# The shell container's mounts over the data volume, as the operator renders
# them: each pin read-write over itself, then every <home>/<tree> read-only.
# Docker mounts shallower destinations first, so the order here does not matter.
TREE_MOUNTS=()
for pin in "${IMAGE_TREE_PINS[@]}"; do
  TREE_MOUNTS+=(--mount "type=volume,src=$DATA_VOL,dst=/opt/data/$pin,volume-subpath=$pin")
done
for home in "${IMAGE_TREE_HOMES[@]}"; do
  for tree in "${IMAGE_TREES[@]}"; do
    TREE_MOUNTS+=(--mount "type=volume,src=$DATA_VOL,dst=/opt/data/${home:+$home/}$tree,volume-subpath=${home:+$home/}$tree,readonly")
  done
done

# Starts the sandbox the way the operator's StatefulSet does: the init
# container's prepare step first, then the shell with the trees mounted
# read-only. The prepare run carries the init container's securityContext --
# read-only root filesystem, every capability dropped but the three it needs,
# no privilege escalation -- so a prepare step that writes outside the volume or
# needs more than that fails here rather than on a cluster. Its output is kept in
# PREPARE_LOG, because the repairs it makes are only ever logged there.
#
# Waits for sshd to answer rather than sleeping: the host-key generation on a
# first start is slow enough on a loaded runner to lose a fixed sleep to, and a
# flaky smoke test gets deleted rather than debugged.
PREPARE_LOG=""
start_sandbox() {
  docker rm -f "$NAME" "$NAME-prepare" >/dev/null 2>&1
  if ! PREPARE_LOG=$(docker run --rm --name "$NAME-prepare" --read-only \
    --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --security-opt no-new-privileges \
    -v "$DATA_VOL:/opt/data" \
    "$IMAGE" --prepare-image-trees 2>&1); then
    echo "FAIL  the prepare step exited non-zero; its output follows" >&2
    echo "$PREPARE_LOG" >&2
    return 1
  fi
  docker run -d --name "$NAME" -p "$PORT:2222" \
    -v "$WORK/keys:/etc/ssh-authorized:ro" \
    -v "$DATA_VOL:/opt/data" \
    "${TREE_MOUNTS[@]}" \
    -v "$SSHD_VOL:/var/lib/sandbox-sshd" \
    -e "SANDBOX_IMAGE_TREES=$IMAGE_TREES_MODE" \
    -e CREDENTIAL_PROXY_URL=http://127.0.0.1:9999 \
    -e CREDENTIAL_PROXY_TOKEN_FILE=/var/run/secrets/kubeagents/credential-proxy/token \
    "$IMAGE" >/dev/null || return 1
  for _ in $(seq 30); do
    ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "FAIL  sandbox never accepted connections; logs follow" >&2
  docker logs "$NAME" >&2
  return 1
}

# plant_offline <sh script>: stops the sandbox and runs the script as uid 1000
# against its data volume, the stand-in for a volume written before the upgrade
# or a plant the running sandbox's mounts would refuse. The next start_sandbox is
# what has to repair it.
plant_offline() {
  docker rm -f "$NAME" >/dev/null 2>&1
  docker run --rm --name "$NAME-plant" --user 1000:1000 --entrypoint sh \
    -v "$DATA_VOL:/opt/data" "$IMAGE" -c "$1"
}

# mount_state <path>: how the running sandbox sees <path> in its own mount table,
# which is what the entrypoint's gate reads too. The last matching line wins, as
# it does for a lookup.
mount_state() {
  docker exec "$NAME" awk -v p="$1" '
    $5 == p { found = 1; ro = 0; n = split($6, o, ","); for (i = 1; i <= n; i++) if (o[i] == "ro") ro = 1 }
    END { if (!found) print "not a mount point"; else if (ro) print "read-only mount"; else print "writable mount" }
  ' /proc/self/mountinfo 2>&1
}

# check_trees_from_image <label>: every tree in every home is a read-only mount
# holding exactly the image's copy. diff runs over ssh as the agent, which can
# read both sides and change neither.
check_trees_from_image() {
  local home tree path
  for home in "${IMAGE_TREE_HOMES[@]}"; do
    for tree in "${IMAGE_TREES[@]}"; do
      path="/opt/data/${home:+$home/}$tree"
      check "$1: $path is a read-only mount" "read-only mount" "$(mount_state "$path")"
      check "$1: $path matches /opt/defaults/$tree" "identical" \
        "$("${SSH[@]}" "diff -r /opt/defaults/$tree $path && echo identical" 2>&1)"
    done
  done
}

echo "== 1. a sandbox with no key mounted must fail loudly =="
# sshd would otherwise start happily and refuse every connection with
# "Permission denied (publickey)", which reads as a key mismatch on the agent
# side and sends whoever is debugging it to the wrong pod.
check "exits with a pointed message when no key is mounted" "the agent could not log in" \
  "$(docker run --rm "$IMAGE" 2>&1)"

echo
echo "== 2. startup =="
start_sandbox || exit 1
logs=$(docker logs "$NAME" 2>&1)
check "generated host keys on first start" "generating ed25519 host key" "$logs"
check "reached exec" "ready; starting" "$logs"
check "sshd is pid 1" "sshd" "$(docker exec "$NAME" ps -o comm= -p 1 2>&1)"
# From inside the container: the state directory is 0700 root:root, so listing it
# from the host would fail for reasons unrelated to whether the keys are there.
check "host keys landed on their volume" "ssh_host_ed25519_key" \
  "$(docker exec "$NAME" ls /var/lib/sandbox-sshd 2>&1)"
check "the data volume is the agent's" "1000" \
  "$(docker exec "$NAME" stat -c '%u' /opt/data 2>&1)"
# start_sandbox has already failed the run if the prepare step exited non-zero
# under the init container's restrictions; this is that it did its job.
check "the prepare step staged the image trees" "image trees prepared" "$PREPARE_LOG"
check "and the shell's entrypoint found them mounted read-only" "are read-only mounts" "$logs"
# Every file, not the top directory: a tree chowned to root at the top and left
# agent-owned below is writable wherever the mount is not.
check "every file in the staged trees is root's and writable by nobody else" "all root, go-w" \
  "$(docker exec "$NAME" sh -c 'out=$(find /opt/data/scripts /opt/data/profiles/platform/skills \( ! -user root -o -perm /022 \) 2>&1) &&
    [ -z "$out" ] && echo "all root, go-w" || echo "$out"' 2>&1)"

echo
echo "== 3. who may log in =="
check "the agent's key works" "agent" "$("${SSH[@]}" whoami 2>&1)"
# sshd's own default, which is the home. It is not where the agent works: Hermes
# is sent TERMINAL_CWD=/opt/data by the operator, because this home is root-owned
# and on the container's ephemeral overlay. The image cannot enforce that —
# asserted here so the two halves of the arrangement are visible together.
check "the session starts in the agent's home" "/home/agent" "$("${SSH[@]}" pwd 2>&1)"
# Every session shares this home, so anything one could leave in it that bash or
# python3 loads unasked would run in every later one: a startup file, a ~/bin
# that Debian's stock .profile puts first on PATH, a usercustomize or .pth file
# in the user site-packages. deploy/sandbox/Dockerfile has the list. The home is
# root-owned and empty instead, and these are the routes it closes.
check "the home is root's and only root may write it" "755 root root" \
  "$("${SSH[@]}" 'stat -c "%a %U %G" ~' 2>&1)"
check "and holds nothing but .ssh and .hermes" "nothing else" \
  "$("${SSH[@]}" 'ls -A ~ | grep -vxE "[.]ssh|[.]hermes" || echo nothing else' 2>&1)"
for startup in .bashrc .bash_profile .bash_login .profile; do
  check "the model cannot write ~/$startup" "Permission denied" \
    "$("${SSH[@]}" "echo 'echo planted' > ~/$startup" 2>&1)"
done
check "nor create ~/bin" "Permission denied" "$("${SSH[@]}" 'mkdir ~/bin' 2>&1)"
check "nor the Python user site-packages" "Permission denied" \
  "$("${SSH[@]}" 'mkdir -p "$(python3 -m site --user-site)"' 2>&1)"
check "the data volume is writable" "ok" \
  "$("${SSH[@]}" 'touch /opt/data/probe && echo ok' 2>&1)"
# One path, two directories: /opt/data is also the agent pod's Hermes home, and
# the marker is how a script or a person tells which side of the SSH connection
# it is looking at.
check "the data volume says which /opt/data it is" "shell sandbox" \
  "$("${SSH[@]}" 'cat /opt/data/.sandbox' 2>&1)"
check "root is refused" "Permission denied" \
  "$(ssh "${SSH_OPTS[@]}" root@127.0.0.1 whoami 2>&1)"
# Two things stop a third account from using the same key: AllowUsers names the
# two that may log in, and AuthorizedKeysFile is %h-relative so an account with
# no authorized_keys of its own has nothing to authenticate against. Refusing
# root alone would prove only PermitRootLogin. uid 1002 because 1001 is hermes,
# and a useradd that fails on a duplicate uid would make this pass for the wrong
# reason.
docker exec "$NAME" useradd -m -u 1002 intruder >/dev/null 2>&1
check "AllowUsers refuses another account holding the same key" "Permission denied" \
  "$(ssh "${SSH_OPTS[@]}" intruder@127.0.0.1 whoami 2>&1)"

echo
echo "== 3b. the hermes principal =="
# The account trusted agent-pod code connects as. It exists so that a caller
# reaching in for a cluster command does not run as the login the model's own
# commands run as; see deploy/sandbox/Dockerfile.
HERMES_SSH=(ssh "${SSH_OPTS[@]}" hermes@127.0.0.1)
check "the same key opens a hermes session" "hermes" "$("${HERMES_SSH[@]}" whoami 2>&1)"
check "hermes gets the forwarded proxy URL too" "http://127.0.0.1:9999" \
  "$("${HERMES_SSH[@]}" 'echo "$CREDENTIAL_PROXY_URL"' 2>&1)"
check "hermes gets the wrapper PATH too" "/opt/credential-proxy/bin/kubectl" \
  "$("${HERMES_SSH[@]}" 'command -v kubectl' 2>&1)"

# The property the whole account exists for: nothing the model leaves behind
# changes what an agent-pod caller sees. The first route found was a shim on
# PATH from ~/.bashrc, which bash sources for a non-interactive `ssh host cmd`,
# and its output would have come back as a trusted tool result. The home is
# root-owned now, so the plant itself fails (section 3 checks the refusals), and
# the shim reaches neither account.
"${SSH[@]}" 'mkdir -p ~/bin && printf "#!/bin/sh\necho FORGED\n" > ~/bin/kubectl && chmod +x ~/bin/kubectl' >/dev/null 2>&1
"${SSH[@]}" 'printf "export PATH=\$HOME/bin:\$PATH\n" > ~/.bashrc' >/dev/null 2>&1
check_absent "a shim the model tries to plant in its home does not reach a hermes session" "FORGED" \
  "$("${HERMES_SSH[@]}" 'kubectl version' 2>&1)"
check_absent "nor a later agent session" "FORGED" \
  "$("${SSH[@]}" 'kubectl version' 2>&1)"

# The model must not be able to reach hermes' authorized_keys, either by writing
# into its home or by authorising a new key in its own.
check "the agent cannot enter the hermes home" "Permission denied" \
  "$("${SSH[@]}" 'ls /home/hermes/.ssh' 2>&1)"

# The kubeconfig directory the platform MCP server writes to. A kubeconfig names
# an exec credential plugin and kubectl runs it, so one the model can author is
# arbitrary code execution as hermes. It lives inside the 0700 home for that
# reason and not for tidiness.
check "hermes can write its kubeconfig directory" "700 hermes" \
  "$("${HERMES_SSH[@]}" 'stat -c "%a %U" /home/hermes/.kubeconfigs' 2>&1)"
check "the agent cannot write a kubeconfig for hermes to use" "Permission denied" \
  "$("${SSH[@]}" 'touch /home/hermes/.kubeconfigs/planted.yaml' 2>&1)"
# The same property one level up: not what a hermes session *sources*, but what
# an agent-pod caller runs over it. Two scripts are forwarded in as hermes --
# `forge.py` for every forge verb (agents/platform/scripts/forge.py, `_forward`)
# and `resolver.py` for the issues sweep's poll (github-issue-resolver,
# `_forward_to_sandbox`) -- and the copy of each under /opt/data sits in a home
# the model owns: read-only under the operator, but outside it a root-owned copy
# the model can rename aside. So the forwarded path is a root-owned staging
# directory instead; section 4b tries to edit both copies. Both are checked:
# resolver.py's hop was left under /opt/data when forge.py's was moved, which is
# what a per-script check would not have let happen.
for forwarded in forge.py resolver.py; do
  check "the forwarded $forwarded is root-owned and writable by nobody else" "755 root root" \
    "$("${SSH[@]}" "stat -c '%a %U %G' /opt/vcs/libexec/platform/$forwarded" 2>&1)"
  check "the model cannot rewrite the $forwarded hermes runs" "Permission denied" \
    "$("${SSH[@]}" "echo '# planted' >> /opt/vcs/libexec/platform/$forwarded" 2>&1)"
done
check "nor take the directory out from under it" "Permission denied" \
  "$("${SSH[@]}" 'mv /opt/vcs/libexec/platform /opt/vcs/libexec/platform.bak' 2>&1)"
# And they run from there, which is the other half: every module either imports
# is staged beside it, so nothing has to be found under /opt/data. `--help` is
# enough to prove that -- argparse only prints usage once the module-level
# imports have all resolved.
check "hermes can run the forwarded forge.py" "usage:" \
  "$("${HERMES_SSH[@]}" 'python3 /opt/vcs/libexec/platform/forge.py --help' 2>&1)"
check "hermes can run the forwarded resolver.py" "usage:" \
  "$("${HERMES_SSH[@]}" 'python3 /opt/vcs/libexec/platform/resolver.py --help' 2>&1)"
# And that the closure really is closed: nothing either one loaded came off the
# agent-owned directories their own sys.path appends put behind the staging
# directory. This is the build guard's check, re-run against the running image,
# because the thing it protects is a runtime property.
check "nothing hermes imports resolves outside the staging directory" "clean" \
  "$("${HERMES_SSH[@]}" 'cd /opt/vcs/libexec/platform && python3 -c "
import sys
sys.path.insert(0, \"/opt/vcs/libexec/platform\")
import forge, resolver
bad = [m.__name__ for m in list(sys.modules.values())
       if getattr(m, \"__file__\", None)
       and (m.__file__.startswith(\"/opt/data\") or m.__file__.startswith(\"/opt/defaults\"))]
print(\" \".join(bad) or \"clean\")
"' 2>&1)"

# Generated on the host so the private half never has to be copied back out of
# the container: the model only needs the public half to authorise it.
ssh-keygen -q -t ed25519 -N '' -f "$WORK/rogue" -C sandbox-smoke-rogue
check "the agent's authorized_keys is root's" "644 root" \
  "$("${SSH[@]}" 'stat -c "%a %U" ~/.ssh/authorized_keys' 2>&1)"
# The directory as well as the file. Replacing a file takes write on the
# directory rather than on the file, so with an agent-owned .ssh the model could
# move the root-owned file aside and write its own in its place, and the append
# below would still be refused. Moved straight back if it ever succeeds, so the
# rest of the run keeps a working login.
check "the agent's .ssh is root's" "755 root" \
  "$("${SSH[@]}" 'stat -c "%a %U" ~/.ssh' 2>&1)"
check "the model cannot move authorized_keys aside" "Permission denied" \
  "$("${SSH[@]}" 'mv ~/.ssh/authorized_keys ~/.ssh/aside && mv ~/.ssh/aside ~/.ssh/authorized_keys && echo moved' 2>&1)"
"${SSH[@]}" "printf '%s\n' '$(cat "$WORK/rogue.pub")' >> ~/.ssh/authorized_keys" >/dev/null 2>&1
check "the model cannot authorise a new key for its own account" "Permission denied" \
  "$(ssh -i "$WORK/rogue" "${SSH_OPTS[@]:2}" agent@127.0.0.1 whoami 2>&1)"
# Asserted as a refusal rather than as the absence of "hermes" in the output:
# sshd's own denial names the account it refused ("hermes@127.0.0.1: Permission
# denied"), so a check for that substring can never pass however well the image
# behaves.
check "the same key does not open a hermes session" "Permission denied" \
  "$(ssh -i "$WORK/rogue" "${SSH_OPTS[@]:2}" hermes@127.0.0.1 whoami 2>&1)"

echo
echo "== 3c. the host keys are not the model's =="
# Both clients pin the host key with StrictHostKeyChecking=accept-new, which is
# worth nothing if the sandboxed account holds the private half. An earlier build
# kept these under the model's volume and chowned them to uid 1000 — the pin
# still looked configured, and the model could read the key it pinned.
check "the agent cannot read the host private key" "Permission denied" \
  "$("${SSH[@]}" 'cat /var/lib/sandbox-sshd/ssh_host_ed25519_key' 2>&1)"
# Mode bits on the key file alone would not settle this. Ownership of the
# directory is what stops the model renaming it aside and having the entrypoint
# populate a replacement it controls on the next start.
check "the host key directory is root's" "700 root" \
  "$(docker exec "$NAME" stat -c '%a %U' /var/lib/sandbox-sshd 2>&1)"
check "the agent cannot write the host key directory" "Permission denied" \
  "$("${SSH[@]}" 'touch /var/lib/sandbox-sshd/planted' 2>&1)"
# And the split has to stay a split: nested under the data volume, everything
# above is undone by the mount point the model owns.
check_absent "the host keys are not under the model's volume" "/opt/data" \
  "$(docker exec "$NAME" sh -c 'grep "^HostKey" /etc/ssh/sshd_config' 2>&1)"
# The entrypoint refuses rather than trusting the deployment to get this right.
# --entrypoint, then chown, then the real entrypoint: the only way to hand it a
# state directory the sandboxed account owns.
check "the entrypoint refuses a state directory the model could write" "not root" \
  "$(docker run --rm --name "$NAME-badsshd" -v "$WORK/keys:/etc/ssh-authorized:ro" \
    --entrypoint bash "$IMAGE" -c \
    'chown agent:agent /var/lib/sandbox-sshd && exec /usr/local/bin/sandbox-entrypoint /usr/sbin/sshd -D -e' 2>&1)"

echo
echo "== 4. what the agent's tools need to find =="
check "python3 exists (execute_code probes for it)" "python3" \
  "$("${SSH[@]}" 'command -v python3' 2>&1)"
check "tar exists (file sync is tar over ssh, not sftp)" "tar" \
  "$("${SSH[@]}" 'command -v tar' 2>&1)"
# Not SSH_OPTS: sftp spells the port -P, and -p means something else entirely.
check "no sftp subsystem is advertised" "subsystem request failed" \
  "$(sftp -i "$WORK/id" -P "$PORT" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes \
    agent@127.0.0.1 </dev/null 2>&1)"

echo
echo "== 4b. what the skills need to find =="
# The shell moved here, so the files a SKILL.md tells the model to run had to
# follow it. The image stages them at /opt/defaults and the prepare step copies
# them onto the volume, because a PVC mounting over /opt/data would otherwise
# hide anything baked there; the shell then mounts each copy read-only.
#
# fleet-audit rather than any skill: Hermes' own ssh backend separately uploads a
# skills tree to ~/.hermes/skills, and that tree is the *chat* profile's — it does
# not contain fleet-audit, pr-conversation, or any of the gke-* skills. Naming one
# of the 19 it lacks is what makes this a test of the baked tree.
check "the platform agent's own skills are on the volume" "fleet-audit" \
  "$("${SSH[@]}" 'ls /opt/data/skills' 2>&1)"
check "and a skill's scripts came with it" "audit_report.py" \
  "$("${SSH[@]}" 'ls /opt/data/skills/fleet-audit/scripts' 2>&1)"
check "the governance SOPs are readable" "compliance_audit_sop.md" \
  "$("${SSH[@]}" 'ls /opt/data/governance' 2>&1)"
# The whole import closure in one call. Each of these is reachable from a skill
# script the model runs, and a missing one shows up as an ImportError deep in a
# skill rather than as anything anybody would connect to this image.
check "the shared-script closure imports" "ok" \
  "$("${SSH[@]}" 'python3 -c "import sys; sys.path.insert(0, \"/opt/data/scripts\"); import sandbox_exec, forge, pr_triggers, github_token_refresh, gitops_workspace; print(\"ok\")"' 2>&1)"
check "and so does a skill script that imports across trees" "ok" \
  "$("${SSH[@]}" 'python3 -c "import sys; sys.path.insert(0, \"/opt/data/scripts\"); sys.path.insert(0, \"/opt/data/skills/fleet-audit/scripts\"); import audit_report; print(\"ok\")"' 2>&1)"
# The failure this replaces named an interpreter, not a script: four of these
# started `#!/opt/hermes/.venv/bin/python3`, a path that exists in the agent image
# and not in this one, so `./audit_report.py` died with "no such file or
# directory" pointing at a venv. python:3.14-slim has no /usr/bin/python3 either,
# so there is nothing to fall through to.
check_absent "no script names an interpreter this image does not have" "/opt/hermes/" \
  "$("${SSH[@]}" 'grep -rh "^#!" /opt/data/scripts /opt/data/skills | sort -u' 2>&1)"
# And the shebang actually dispatches, rather than only looking right. bash reports
# a missing interpreter as "No such file or directory" against the script's own
# path, which reads as a missing script.
check_absent "a script invoked directly starts" "No such file" \
  "$("${SSH[@]}" '/opt/data/skills/fleet-audit/scripts/audit_report.py --help' 2>&1)"
# The tests are the bulk of the tree and nothing here runs them.
check_absent "the unit tests did not come along" "test_audit_report.py" \
  "$("${SSH[@]}" 'ls /opt/data/skills/fleet-audit/scripts' 2>&1)"
# cluster_agent_profile.py cannot work here — it shells out to `hermes profile
# create` and writes the agent pod's PVC — and four SKILL.md files name it. The
# stub is what the model gets, so the failure explains itself instead of reading
# as a broken image.
stub_out=$("${SSH[@]}" 'python3 /opt/data/scripts/cluster_agent_profile.py create --name x 2>&1; echo "rc=$?"' 2>&1)
check "the agent-pod-only stub says why" "does not run in the shell sandbox" "$stub_out"
check "and fails rather than reporting success" "rc=1" "$stub_out"
# $HERMES_HOME and the literal /opt/data both appear in the SKILL.md files, and
# gitops_workspace.agent_home() reads PLATFORM_AGENT_HOME. sshd starts sessions
# with none of them, so the entrypoint puts them on its SetEnv line.
check "HERMES_HOME reaches a non-login session" "/opt/data" \
  "$("${SSH[@]}" 'echo "$HERMES_HOME"' 2>&1)"
check "PLATFORM_AGENT_HOME too" "/opt/data" \
  "$("${SSH[@]}" 'echo "$PLATFORM_AGENT_HOME"' 2>&1)"
check "and the reference forms in the skills resolve to the same file" "ok" \
  "$("${SSH[@]}" 'cmp -s "$HERMES_HOME"/scripts/forge.py /opt/data/scripts/forge.py && echo ok' 2>&1)"
# The delivery is image-owned, and the model cannot change it: the model runs
# these files, and so does the agent pod's code over ssh, so an edit that stuck
# would be code the next caller runs as if it had shipped. Each tree is a
# root-owned read-only mount. The permission check comes before the mount's, so
# the model's write to a file gets EACCES; section 12 shows the mount refusing an
# unlink with EROFS.
check "the model cannot edit what it runs" "Permission denied" \
  "$("${SSH[@]}" 'echo "# planted" >> /opt/data/scripts/forge.py' 2>&1)"
check_absent "and the file is unchanged" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/data/scripts/forge.py' 2>&1)"
# The other end of the boundary section 3b set up. What an agent-pod caller runs
# is the root-owned staging copy, which is untouched whatever happens above.
check_absent "the edit does not reach the copy hermes runs" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/vcs/libexec/platform/forge.py' 2>&1)"
# And the same for the issues sweep's entry point, which reaches /opt/data on
# every tick of an install with the sandbox on.
check "nor its own copy of the resolver" "Permission denied" \
  "$("${SSH[@]}" 'echo "# planted" >> /opt/data/skills/github-issue-resolver/scripts/resolver.py' 2>&1)"
check_absent "and the resolver is unchanged" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/data/skills/github-issue-resolver/scripts/resolver.py' 2>&1)"
check_absent "the edit does not reach the resolver hermes runs" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/vcs/libexec/platform/resolver.py' 2>&1)"
# Nor can it replace a tree without editing it. Each tree is a mount point, so
# renaming it aside is EBUSY; so are profiles/ and profiles/platform, which the
# operator mounts over themselves for this reason alone. rename(2) checks only
# the directory it renames, so without those pins moving profiles/ aside and
# recreating profiles/platform/scripts beneath a new one would work. Every
# destination is in the source's own directory, so the rename is attempted on
# one mount and EBUSY, not a cross-device EXDEV, is what answers.
for moved in /opt/data/scripts /opt/data/profiles/platform/scripts \
  /opt/data/profiles/platform /opt/data/profiles; do
  check "$moved cannot be renamed aside" "Device or resource busy" \
    "$("${SSH[@]}" "mv $moved $moved.aside" 2>&1)"
done
# A link resolves to the file on the read-only mount, so a write through it
# reaches that file and is refused the same way. It is the route a file tool's
# write takes when it follows a link the model left in its scratch space.
check "a write through a link the model plants is refused too" "Permission denied" \
  "$("${SSH[@]}" 'mkdir -p /opt/data/scratch && ln -sf /opt/data/scripts/forge.py /opt/data/scratch/l &&
    echo "# planted" >> /opt/data/scratch/l' 2>&1)"
# The staging copy the trees come from, which several scripts also append to
# sys.path: root-owned, so the model cannot drop in a module for that append to
# find.
check "the image's staging copy is not the model's either" "Permission denied" \
  "$("${SSH[@]}" 'touch /opt/defaults/scripts/x' 2>&1)"

echo
echo "== 4c. the working directory Hermes cds into =="
# Every terminal command Hermes sends opens with `builtin cd -- <cwd> || exit
# 126`, and nothing in its SSH backend creates <cwd> on the remote. The
# delegated-kanban path is where that lands: the dispatcher mkdirs a scratch
# workspace on the agent pod's PVC and pins it as the worker's TERMINAL_CWD,
# this pod has a different ReadWriteOnce PVC, and so every command the card runs
# exits 126 with no output. /usr/local/bin/sandbox-session-command is the fix
# and this section is its test.
#
# Sent on the wire the way ssh.py sends it — `bash -c <shlex.quote(script)>` —
# rather than approximated. The wrapper parses that exact encoding, so a test
# that handed it the script any other way would exercise nothing.
# hermes_ssh <cwd-word> <command> [ssh option...]: the shape base.py builds.
# Anything after the command is handed to ssh ahead of the destination, which
# is how section 4e names a profile the way the agent image's client does.
hermes_ssh() {
  local script quoted
  script=$(printf 'builtin cd -- %s || exit 126\neval %s\n__hermes_ec=$?\nexit $__hermes_ec' \
    "$1" "'$2'")
  quoted=${script//\'/\'\"\'\"\'}
  shift 2
  ssh "${SSH_OPTS[@]}" "$@" agent@127.0.0.1 "bash -c '$quoted'"
}

WS="/opt/data/kanban/workspaces/smoke-$$"
# Asserted rather than assumed: if the path already existed the next check would
# pass without the wrapper doing anything.
check "the scratch workspace does not exist beforehand" "No such file" \
  "$("${SSH[@]}" "ls -d $WS" 2>&1)"
check "a wrapped command whose cwd is missing runs in it instead of exiting 126" "$WS" \
  "$(hermes_ssh "$WS" 'pwd' 2>&1)"
check "and the directory it created belongs to the model" "1000" \
  "$("${SSH[@]}" "stat -c '%u' $WS" 2>&1)"

# The pre-existing failure has to survive. A cwd that cannot be created must
# still fail, and fail the same way, rather than be papered over into something
# that runs in the wrong directory.
uncreatable=$(hermes_ssh /nonexistent-root/ws 'pwd' 2>&1; echo "rc=$?")
check "an uncreatable working directory still exits 126" "rc=126" "$uncreatable"
check "and the wrapper says which directory it could not create" \
  "could not create /nonexistent-root/ws" "$uncreatable"

# _quote_cwd_for_cd emits a bare `~` and rewrites `~/x` through $HOME, so the
# target is a shell word and has to be expanded on this side. A wrapper that
# took it for a literal path would name '$HOME' in its message below instead.
check "a bare ~ cwd resolves to this pod's home" "/home/agent" \
  "$(hermes_ssh '~' 'pwd' 2>&1)"
# The home is root-owned, so the wrapper cannot create this one and the command
# exits 126 as above. The path in its message is what shows the word expanded.
check "a \$HOME-relative cwd with a space stays one word" "could not create /home/agent/smoke ws" \
  "$(hermes_ssh "\$HOME/'smoke ws'" 'pwd' 2>&1)"

# Everything that is not a Hermes wrapper has to pass through untouched. tar
# over the connection is how file sync moves whole directories in both
# directions, and it is the traffic a ForceCommand is likeliest to break.
check "tar over the connection still streams" "etc/hostname" \
  "$("${SSH[@]}" 'tar cf - -C / etc/hostname' 2>/dev/null | tar tf - 2>&1)"
plain=$("${SSH[@]}" 'echo hello' 2>&1)
check "a plain command with no cd line is unchanged" "hello" "$plain"
check_absent "and is not mistaken for a broken wrapper" "sandbox-session-command:" "$plain"

# ForceCommand replaces the login shell as well as a command, so an interactive
# session has to be started by hand or ssh'ing in to debug this pod stops
# working.
check "an interactive session still gets a shell" "agent" \
  "$(ssh "${SSH_OPTS[@]}" agent@127.0.0.1 <<<'whoami' 2>&1)"

# The drift alarm. This fix parses a string tools/environments/base.py owns, and
# the failure mode of a base-image bump that reshapes it is silence: the mkdir
# stops happening and cards go back to exiting 126 for no visible reason. A
# wrapper carrying __hermes_ec and no cd line is what that looks like from here,
# and it has to be loud.
drift=$(printf 'echo hi\n__hermes_ec=0\nexit $__hermes_ec')
drift_out=$("${SSH[@]}" "bash -c '$drift'" 2>&1)
check "a Hermes wrapper with no cd line is reported, not ignored" \
  "no longer being applied" "$drift_out"
check "and the command still runs" "hi" "$drift_out"

echo
echo "== 4d. the kanban variables the SSH crossing drops =="
# The dispatcher sets HERMES_KANBAN_TASK and HERMES_KANBAN_WORKSPACE in the
# worker's process environment and nothing carries them over the connection, so
# the worker protocol's own `cd $HERMES_KANBAN_WORKSPACE` — unquoted — collapses
# to a bare `cd`, which goes to $HOME rather than doing nothing. Three probe
# cards run in parallel on a live install showed it: one wrote its output into
# the shared /home/agent, exit 0, nothing in the output to say so. The wrapper
# derives both from the cd target.
KWS="/opt/data/kanban/workspaces/t_5eeded01"
check "a scratch workspace yields the task id" "task=[t_5eeded01]" \
  "$(hermes_ssh "$KWS" 'echo "task=[$HERMES_KANBAN_TASK]"' 2>&1)"
check "and the workspace path" "ws=[$KWS]" \
  "$(hermes_ssh "$KWS" 'echo "ws=[$HERMES_KANBAN_WORKSPACE]"' 2>&1)"
# The property that makes the derivation safe to leave on: it comes from the
# `<...>/workspaces/<id>` prefix, not from the cwd, so a command the model runs
# from a subdirectory still reports the workspace rather than the subdirectory.
check "a subdirectory still reports the workspace, not itself" "ws=[$KWS]" \
  "$(hermes_ssh "$KWS/build/out" 'echo "ws=[$HERMES_KANBAN_WORKSPACE]"' 2>&1)"
# The other board layout workspaces_root() produces.
KBWS="/opt/data/kanban/boards/ops/workspaces/t_5eeded02"
check "the per-board workspace layout resolves too" "ws=[$KBWS] task=[t_5eeded02]" \
  "$(hermes_ssh "$KBWS" 'echo "ws=[$HERMES_KANBAN_WORKSPACE] task=[$HERMES_KANBAN_TASK]"' 2>&1)"
# Absent beats wrong. A script that builds an absolute path from a workspace
# that is not its own writes outside it, so anything that is not a task id under
# a kanban `workspaces/` directory has to leave both unset.
check "a directory that is not a task id sets nothing" "ws=[] task=[]" \
  "$(hermes_ssh /opt/data/kanban/workspaces/scratchpad \
    'echo "ws=[$HERMES_KANBAN_WORKSPACE] task=[$HERMES_KANBAN_TASK]"' 2>&1)"
check "nor does a workspaces directory outside kanban" "ws=[] task=[]" \
  "$(hermes_ssh /opt/data/other/workspaces/t_5eeded01 \
    'echo "ws=[$HERMES_KANBAN_WORKSPACE] task=[$HERMES_KANBAN_TASK]"' 2>&1)"
check "nor an ordinary working directory" "ws=[] task=[]" \
  "$(hermes_ssh /opt/data 'echo "ws=[$HERMES_KANBAN_WORKSPACE] task=[$HERMES_KANBAN_TASK]"' 2>&1)"
# The failure as the probe card actually hit it, end to end.
check "the unquoted protocol idiom stays in the workspace" "$KWS" \
  "$(hermes_ssh "$KWS" 'cd $HERMES_KANBAN_WORKSPACE && pwd' 2>&1)"
check_absent "and does not land in the home every card shares" "/home/agent" \
  "$(hermes_ssh "$KWS" 'cd $HERMES_KANBAN_WORKSPACE && pwd' 2>&1)"
"${SSH[@]}" "rm -rf $KWS /opt/data/kanban/boards /opt/data/kanban/workspaces/scratchpad /opt/data/other" >/dev/null 2>&1

echo
echo "== 4e. the profile home the SSH crossing drops =="
# HERMES_HOME names the *profile* home in the agent container — a Cluster Agent
# worker sees /opt/data/profiles/cluster-<x> — and nothing carries a process
# environment across the connection, so the entrypoint's SetEnv line can only
# name one static value and it names the root. Section 3 above checks that
# value; this section checks the wrapper narrowing it back per session.
# cluster_preflight.sh is what shows when it does not: it reads the default
# profile's USER.md and kubeconfig.yaml and reports the Cluster Agent has no
# identity, or passes on an identity that is not its own.
CP="/opt/data/profiles/cluster-smoke"
# Staged by the entrypoint from $SANDBOX_HOME_ROOTS, and it has no kubeconfig —
# which is the second case below.
PP="/opt/data/profiles/platform"
"${SSH[@]}" "mkdir -p $CP && printf 'kubeconfig\n' > $CP/kubeconfig.yaml" >/dev/null 2>&1
check "the profile the entrypoint stages is there to narrow to" "$PP" \
  "$("${SSH[@]}" "ls -d $PP" 2>&1)"
check "a command run in a profile home sees that home" "home=[$CP]" \
  "$(hermes_ssh "$CP" 'echo "home=[$HERMES_HOME]"' 2>&1)"
check "and the kubeconfig pinned inside it" "kc=[$CP/kubeconfig.yaml]" \
  "$(hermes_ssh "$CP" 'echo "kc=[$KUBECONFIG]"' 2>&1)"
# The Cluster Agent's own commands run from a kanban workspace beneath its
# profile home, not from the home itself, so the derivation has to survive the
# depth — and both derivations have to happen, not one or the other.
CPWS="$CP/kanban/workspaces/t_5eeded03"
check "a workspace beneath it still resolves the home" "home=[$CP] ws=[$CPWS]" \
  "$(hermes_ssh "$CPWS" 'echo "home=[$HERMES_HOME] ws=[$HERMES_KANBAN_WORKSPACE]"' 2>&1)"
# Unset beats pointing at a file that is not there. `kubectl` with a KUBECONFIG
# naming a missing path fails with an empty-config error on every invocation,
# which turns "this profile has no credential yet" into "kubectl is broken".
check "a profile with no kubeconfig narrows the home and no more" "home=[$PP] kc=[]" \
  "$(hermes_ssh "$PP" 'echo "home=[$HERMES_HOME] kc=[$KUBECONFIG]"' 2>&1)"
# Everything outside profiles/ is the default profile, and the root is what it
# wants. The profiles directory itself has no profile name in it.
check "an ordinary working directory leaves both as sshd set them" "home=[/opt/data] kc=[]" \
  "$(hermes_ssh "/opt/data/scratch/smoke-$$" 'echo "home=[$HERMES_HOME] kc=[$KUBECONFIG]"' 2>&1)"
check "and so does the profiles directory itself" "home=[/opt/data] kc=[]" \
  "$(hermes_ssh /opt/data/profiles 'echo "home=[$HERMES_HOME] kc=[$KUBECONFIG]"' 2>&1)"
# PLATFORM_AGENT_HOME names the data root, not a profile home
# (gitops_workspace.agent_home()), and narrowing it would put every clone the
# GitOps skills make outside the credential proxy's workspace root.
check "PLATFORM_AGENT_HOME is not narrowed with it" "data=[/opt/data]" \
  "$(hermes_ssh "$CP" 'echo "data=[$PLATFORM_AGENT_HOME]"' 2>&1)"

# The shape the dispatcher actually produces, which none of the cases above
# reach: a card's scratch workspace on the default board is
# `<root>/kanban/workspaces/<id>`, under no profile home, so the cwd says
# nothing about which profile is speaking. A Cluster Agent card kept the root,
# its preflight read the default profile's USER.md, and it blocked on every
# dispatch. The agent image's ssh client now names the profile on every
# connection (deploy/docker/ssh-wrapper.sh puts it in the client's environment,
# ssh_config.d/10-sandbox-profile-home.conf sends it). This half drives the
# sandbox's side of that from the runner's own client, with the value spelled
# out through -o SetEnv; the client's side, wrapper and drop-in and the
# connection sharing they have to survive, is 4f below, from the agent image.
hermes_ssh_named() { # hermes_ssh_named <HERMES_PROFILE_HOME> <cwd-word> <command>
  hermes_ssh "$2" "$3" -o "SetEnv=HERMES_PROFILE_HOME=$1"
}
SWS="/opt/data/kanban/workspaces/t_5eeded04"
check "a shared-root workspace alone leaves the root, which is the gap" "home=[/opt/data]" \
  "$(hermes_ssh "$SWS" 'echo "home=[$HERMES_HOME]"' 2>&1)"
check "the profile the client names narrows it, kubeconfig and kanban variables with it" \
  "home=[$CP] kc=[$CP/kubeconfig.yaml] ws=[$SWS]" \
  "$(hermes_ssh_named "$CP" "$SWS" 'echo "home=[$HERMES_HOME] kc=[$KUBECONFIG] ws=[$HERMES_KANBAN_WORKSPACE]"' 2>&1)"
# The two pods' data roots are different volumes that happen to share a path,
# so the value is read as a name and rebased onto this one.
check "a home under another root is rebased by name" "home=[$CP]" \
  "$(hermes_ssh_named /mnt/agent-data/profiles/cluster-smoke "$SWS" 'echo "home=[$HERMES_HOME]"' 2>&1)"
check "the client's name beats a cwd under another profile" "home=[$CP]" \
  "$(hermes_ssh_named "$CP" "$PP/kanban/workspaces/t_5eeded05" 'echo "home=[$HERMES_HOME]"' 2>&1)"
# A worker on the default profile sends the root. Not a profile and not an
# error: the root is what that worker wants.
root_named=$(hermes_ssh_named /opt/data "$SWS" 'echo "home=[$HERMES_HOME]"' 2>&1)
check "the root itself names no profile" "home=[/opt/data]" "$root_named"
check_absent "and is not remarked on" "sandbox-session-command:" "$root_named"
# A profile the agent pod has and this volume does not yet. The mirror runs on
# the agent pod's start and when a profile is scaffolded, and a card dispatched
# inside that window has to say why its preflight is about to read the wrong tree.
unmirrored=$(hermes_ssh_named /opt/data/profiles/cluster-absent "$SWS" 'echo "home=[$HERMES_HOME]"' 2>&1)
check "a profile not mirrored yet falls back to the working directory" "home=[/opt/data]" "$unmirrored"
check "and says so" "profile cluster-absent is not mirrored into the sandbox yet" "$unmirrored"
# The client picks among the homes this volume has, and nothing else.
check "a traversal in the name is refused" "home=[/opt/data]" \
  "$(hermes_ssh_named /opt/data/profiles/.. "$SWS" 'echo "home=[$HERMES_HOME]"' 2>&1)"
check "and so is a name with a slash in it" "home=[/opt/data]" \
  "$(hermes_ssh_named /opt/data/profiles/../../etc "$SWS" 'echo "home=[$HERMES_HOME]"' 2>&1)"
# An AcceptEnv inside a Match block replaces the global list rather than adding
# to it, so the agent's block restates the locale or loses it.
check "the agent account still accepts the locale" "lang=[C.smoke]" \
  "$(ssh "${SSH_OPTS[@]}" -o SetEnv=LANG=C.smoke agent@127.0.0.1 'echo "lang=[$LANG]"' 2>&1)"
check "the hermes account is not offered the profile home" "named=[]" \
  "$(ssh "${SSH_OPTS[@]}" -o "SetEnv=HERMES_PROFILE_HOME=$CP" hermes@127.0.0.1 'echo "named=[$HERMES_PROFILE_HOME]"' 2>&1)"

echo
echo "== 4f. the agent image's own client names the profile =="
# The other half: the wrapper and the drop-in as the agent image carries them,
# through the ssh client the agent image carries, against this sandbox.
# docker-build.yml builds the platform image with load: true before it reaches
# this script, so the image is in the daemon there; elsewhere the section says
# it skipped rather than failing a run that has nothing to do with the agent
# image. --add-host gives the sandbox the name the operator would give it,
# because the drop-in matches on that name and a Hostname rewrite in a client
# config would defeat it: `Match host` sees the name after Hostname
# substitution. --network host reaches the published port the way the runner's
# own client does, and the key travels on stdin rather than a bind mount, so
# nothing on the host has to be readable by the image's uid. `ssh` is resolved
# through the image's PATH on purpose: the wrapper is what it has to find.
AGENT_IMAGE="${AGENT_IMAGE:-platform-agent:latest}"
SANDBOX_ALIAS=smoke-shell-0.smoke-shell.smoke.svc.cluster.local
# agent_ssh <HERMES_HOME for the client, empty for unset> <host> <command>
# [ssh option...]: one ssh from a fresh container of the agent image.
agent_ssh() {
  local hermes_home=$1 host=$2 command=$3
  shift 3
  docker run --rm -i --network host --add-host "$SANDBOX_ALIAS:127.0.0.1" \
    -e "SMOKE_HERMES_HOME=$hermes_home" --entrypoint sh "$AGENT_IMAGE" -c '
      umask 077 && mkdir -p /tmp/smoke && cat >/tmp/smoke/id || exit 1
      if [ -n "$SMOKE_HERMES_HOME" ]; then export HERMES_HOME=$SMOKE_HERMES_HOME; else unset HERMES_HOME; fi
      port=$1 host=$2 command=$3; shift 3
      exec ssh -n -i /tmp/smoke/id -p "$port" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=DEBUG1 \
        "$@" "agent@$host" "$command"' _ "$PORT" "$host" "$command" "$@" <"$WORK/id"
}
if docker image inspect "$AGENT_IMAGE" >/dev/null 2>&1; then
  # The client's debug log is the only place it says what it sent.
  named=$(agent_ssh "$CP" "$SANDBOX_ALIAS" 'echo "home=[$HERMES_HOME] named=[$HERMES_PROFILE_HOME]"' 2>&1)
  check "the client sends the worker's HERMES_HOME" "setting env HERMES_PROFILE_HOME = \"$CP\"" "$named"
  check "and the session narrows to it" "home=[$CP] named=[$CP]" "$named"
  unset_out=$(agent_ssh "" "$SANDBOX_ALIAS" 'echo "home=[$HERMES_HOME] named=[$HERMES_PROFILE_HOME]"' 2>&1)
  check_absent "an unset HERMES_HOME sends nothing" "setting env HERMES_PROFILE_HOME" "$unset_out"
  check "and still connects" "home=[/opt/data] named=[]" "$unset_out"
  elsewhere=$(agent_ssh "$CP" 127.0.0.1 'echo "named=[$HERMES_PROFILE_HOME]"' 2>&1)
  check_absent "a host that is not a sandbox is sent nothing" "setting env HERMES_PROFILE_HOME" "$elsewhere"
  check "and still connects" "named=[]" "$elsewhere"

  # Connection sharing, which is how Hermes actually connects: every ssh it
  # spawns carries ControlMaster=auto and one ControlPath per user@host:port,
  # so every process in the pod rides the master the first one opened. A
  # profile carried by SetEnv would be the master's here, not the caller's; a
  # SendEnv'd variable is forwarded by the master from the caller's own
  # environment. One container, so the two sessions share a control socket:
  # the master is opened as the platform profile, the multiplexed session asks
  # as the cluster profile, and the cluster profile is what has to arrive.
  mux=$(docker run --rm -i --network host --add-host "$SANDBOX_ALIAS:127.0.0.1" \
    -e "SMOKE_MASTER_HOME=$PP" -e "SMOKE_CLIENT_HOME=$CP" --entrypoint sh "$AGENT_IMAGE" -c '
      umask 077 && mkdir -p /tmp/smoke && cat >/tmp/smoke/id || exit 1
      port=$1 host=$2
      opts="-n -i /tmp/smoke/id -p $port -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=5 -o ControlPath=/tmp/smoke/control"
      HERMES_HOME=$SMOKE_MASTER_HOME ssh $opts -o ControlMaster=yes -o ControlPersist=60 "agent@$host" \
        "echo \"master: home=[\$HERMES_HOME] named=[\$HERMES_PROFILE_HOME]\""
      HERMES_HOME=$SMOKE_CLIENT_HOME ssh $opts -o ControlMaster=auto "agent@$host" \
        "echo \"mux: home=[\$HERMES_HOME] named=[\$HERMES_PROFILE_HOME]\""
      ssh -o ControlPath=/tmp/smoke/control -O exit "agent@$host" 2>/dev/null' _ "$PORT" "$SANDBOX_ALIAS" <"$WORK/id" 2>&1)
  check "the master session is its own profile" "master: home=[$PP] named=[$PP]" "$mux"
  check "a session multiplexed over it is the caller's, not the master's" "mux: home=[$CP] named=[$CP]" "$mux"
else
  echo "SKIP  $AGENT_IMAGE is not loaded; docker-build.yml loads it before this script and runs these there"
fi
"${SSH[@]}" "rm -rf $CP $SWS $PP/kanban /opt/data/scratch/smoke-$$" >/dev/null 2>&1

echo
echo "== 5. credential-proxy wrappers =="
for cli in kubectl gcloud; do
  check "$cli resolves to the wrapper, not 'command not found'" "/opt/credential-proxy/bin/$cli" \
    "$("${SSH[@]}" "command -v $cli" 2>&1)"
done
# Non-login is the shape of every command the agent sends, and the case that
# reads no /etc/profile. This is the check that caught the original bug: PATH
# arrived and CREDENTIAL_PROXY_URL did not, so the wrappers resolved and then
# refused to run.
check "CREDENTIAL_PROXY_URL crosses into a non-login session" "http://127.0.0.1:9999" \
  "$("${SSH[@]}" 'echo "$CREDENTIAL_PROXY_URL"' 2>&1)"
check "and into a login session" "http://127.0.0.1:9999" \
  "$("${SSH[@]}" 'bash -l -c "echo \$CREDENTIAL_PROXY_URL"' 2>&1)"
# The URL alone is not enough to reach the broker: off the agent's pod it
# authenticates every caller, so a session holding the address and not the token
# path gets a 401 from every wrapper rather than a connection error.
check "CREDENTIAL_PROXY_TOKEN_FILE crosses too" "/var/run/secrets/kubeagents/credential-proxy/token" \
  "$("${SSH[@]}" 'echo "$CREDENTIAL_PROXY_TOKEN_FILE"' 2>&1)"
check "the wrapper dispatches rather than refusing to start" "credential proxy" \
  "$("${SSH[@]}" 'kubectl version 2>&1' 2>&1)"
check "the wrappers are on PATH" "/opt/credential-proxy/bin:" \
  "$("${SSH[@]}" 'echo "$PATH"' 2>&1)"
# A login shell runs /etc/profile, which overwrites PATH wholesale; profile.d is
# what puts the wrappers back. Both paths, because only one of them is sshd's.
check "PATH survives /etc/profile in a login shell" "/opt/credential-proxy/bin/kubectl" \
  "$("${SSH[@]}" 'bash -l -c "command -v kubectl"' 2>&1)"

echo
echo "== 5b. the version-control skill's local git =="
# The only git in the image, and gh nowhere at all. Asserted by what the name
# resolves to rather than by which PATH entry wins, in both session shapes:
# /etc/profile overwrites PATH in a login shell, and profile.d is what puts
# /opt/vcs/bin back there.
check "the name git resolves to the local git" "/opt/vcs/bin/git" \
  "$("${SSH[@]}" 'command -v git' 2>&1)"
check "and in a login session too" "/opt/vcs/bin/git" \
  "$("${SSH[@]}" "bash -l -c 'command -v git'" 2>&1)"
check "gh is not there" "absent" \
  "$("${SSH[@]}" 'command -v gh >/dev/null 2>&1 && echo present || echo absent' 2>&1)"
check "nor in a login session" "absent" \
  "$("${SSH[@]}" "bash -l -c 'command -v gh >/dev/null 2>&1 && echo present || echo absent'" 2>&1)"
# A working copy that names its own hooks directory still gets the empty one:
# the wrapper's `-c` outranks the repository's config.
check "a repository's own hooksPath does not reach the wrapper" "/opt/vcs/share/no-hooks" \
  "$("${SSH[@]}" 'git init -q /tmp/sh && git -C /tmp/sh config core.hooksPath .githooks && git -C /tmp/sh config core.hooksPath' 2>&1)"
"${SSH[@]}" 'rm -rf /tmp/sh' >/dev/null 2>&1
check "the local git is a real git" "git version" \
  "$("${SSH[@]}" '/opt/vcs/libexec/git --version' 2>&1)"
# The message, not the exit status: example.invalid resolves nowhere, so an
# https ls-remote fails on an image that still ships git-remote-https too, and a
# check on failure alone would pass there. "not a git command" is git's external
# dispatch failing to find the helper -- the shape it prints when
# /usr/lib/git-core/git exists, which it does here. The Dockerfile guard asserts
# the same thing at build time; this asserts it of the image that was actually
# pulled, over the transport the agent uses.
check "and has no https transport" "is not a git command" \
  "$("${SSH[@]}" '/opt/vcs/libexec/git ls-remote https://example.invalid/x.git' 2>&1)"
# ssh:// has no helper to delete; what closes it is that the image ships no ssh
# client. Asserted as an absence, the file-shaped control the design asks for,
# and then by git's own failure to exec one.
check "and has no ssh client" "absent" \
  "$("${SSH[@]}" 'command -v ssh >/dev/null 2>&1 && echo present || echo absent' 2>&1)"
check "so ssh:// goes nowhere" "cannot run ssh" \
  "$("${SSH[@]}" '/opt/vcs/libexec/git ls-remote ssh://example.invalid/x' 2>&1)"
# The other half of the check above: a git broken outright also fails to reach a
# network, and would pass it. This is what says the disarming was surgical.
check "but still reads a local repository" "rc=0" \
  "$("${SSH[@]}" '/opt/vcs/libexec/git init -q /tmp/sm && /opt/vcs/libexec/git ls-remote file:///tmp/sm >/dev/null; echo rc=$?' 2>&1 | tail -1)"
"${SSH[@]}" 'rm -rf /tmp/sm' >/dev/null 2>&1

echo
echo "== 6. a restart must not change the host key or lose the model's work =="
# Hermes connects with StrictHostKeyChecking=accept-new, which accepts a key it
# has never seen and refuses one that changed. A regenerated host key is not a
# prompt, it is every later command failing until known_hosts is cleared by hand.
# Two files, one on each side of the durability line: /opt/data/probe was
# written in section 3 and this one goes on the container's own disk, where the
# home the shell would default to also is.
"${SSH[@]}" 'touch /tmp/ephemeral-probe' >/dev/null 2>&1
before=$(ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | awk '{print $3}')
start_sandbox || exit 1
after=$(ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 2>/dev/null | awk '{print $3}')
check "same host key after a recycle" "$before" "$after"
check_absent "the second start reused the volume's keys" "generating ed25519" \
  "$(docker logs "$NAME" 2>&1)"
# The other half of the reason the volumes exist, and the reason the operator
# sends TERMINAL_CWD=/opt/data: without it the shell defaults to `~`, which is
# the container overlay below, and a live install ran for five days with the
# model's work on the wrong side of this line.
check "the model's files on the data volume survived the recycle" "probe" \
  "$("${SSH[@]}" 'ls /opt/data' 2>&1)"
check_absent "the ones on the container's disk did not" "ephemeral-probe" \
  "$("${SSH[@]}" 'ls -a /tmp' 2>&1)"
# The other side of that line, and the reason the prepare step replaces rather
# than merges: the skills, SOPs and shared scripts are image-owned, so every
# start has to leave each tree exactly the image's copy. Merging would leave a
# script deleted from the image sitting on the volume looking current for as
# long as the PVC lives. Section 4b's edits were refused rather than undone, so
# the tail is a spot check and the diff against /opt/defaults is the claim.
check_absent "the image-owned trees are the image's copy" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/data/scripts/forge.py' 2>&1)"
check_trees_from_image "after a recycle"

echo
echo "== 7. an unconfigured proxy warns, it does not crash =="
# Expected state until #737 Part C makes the credential proxy reachable from
# outside the agent pod: file and code-execution tools still have to work.
#
# Also the image run outside the operator, as sections 1, 3c and 8 are: no
# prepare step, no mounts, no SANDBOX_IMAGE_TREES. The entrypoint stages the
# trees itself as root-owned copies, which stops an in-place edit and not a
# rename, and it has to say so.
docker rm -f "$NAME-nourl" >/dev/null 2>&1
docker run -d --name "$NAME-nourl" -v "$WORK/keys:/etc/ssh-authorized:ro" "$IMAGE" >/dev/null
sleep 3
nourl_logs=$(docker logs "$NAME-nourl" 2>&1)
check "says so in the log" "CREDENTIAL_PROXY_URL is unset" "$nourl_logs"
check "starts sshd anyway" "sshd" "$(docker exec "$NAME-nourl" ps -o comm= -p 1 2>&1)"
check "with no mounts the trees are staged as root-owned copies" "root root" \
  "$(docker exec "$NAME-nourl" stat -c '%U' /opt/data/scripts /opt/data/profiles/platform/scripts 2>&1 | tr '\n' ' ')"
check "and the log says a rename is still possible there" "can still rename a tree aside" "$nourl_logs"
docker rm -f "$NAME-nourl" >/dev/null 2>&1

echo
echo "== 8. a newline in a forwarded value is an sshd_config injection =="
# The pod environment is not attacker-controlled today. It is the only untrusted
# input this entrypoint copies into a file that decides who may log in, which is
# a short enough distance to be worth a guard and a test.
out=$(docker run --rm -v "$WORK/keys:/etc/ssh-authorized:ro" \
  -e $'CREDENTIAL_PROXY_URL=http://x\nPermitRootLogin yes' "$IMAGE" 2>&1)
check "refuses the value" "contains a newline, quote or backslash" "$out"
check_absent "and does not start sshd with it" "ready; starting" "$out"

echo
echo "== 8b. a declared mount mode with no mounts must not start =="
# SANDBOX_IMAGE_TREES=read-only-mounts is the operator saying the trees are
# mounted read-only. The entrypoint checks the mount table rather than taking
# that on trust, and a tree that is not mounted at all has to stop the start as
# surely as one mounted writable: either way the model could change what it
# runs. The key is mounted so that the gate is the only thing left to refuse.
# Detached and polled rather than run in the foreground, because an image
# without the gate starts sshd and would never return.
docker rm -f "$NAME-nomounts" >/dev/null 2>&1
docker run -d --name "$NAME-nomounts" -v "$WORK/keys:/etc/ssh-authorized:ro" \
  -e "SANDBOX_IMAGE_TREES=$IMAGE_TREES_MODE" "$IMAGE" >/dev/null
for _ in $(seq 30); do
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME-nomounts" 2>/dev/null)" = false ] && break
  sleep 1
done
nomounts=$(docker inspect -f 'running=[{{.State.Running}}] exit=[{{.State.ExitCode}}]' "$NAME-nomounts" 2>&1)
nomounts_logs=$(docker logs "$NAME-nomounts" 2>&1)
docker rm -f "$NAME-nomounts" >/dev/null 2>&1
check "the sandbox stops rather than starting" "running=[false]" "$nomounts"
check_absent "with a non-zero status" "exit=[0]" "$nomounts"
check "and names the first tree that is not a read-only mount" "/opt/data/governance" "$nomounts_logs"
check_absent "and never reaches sshd" "ready; starting" "$nomounts_logs"

echo
echo "== 9. a symlink planted under /opt/data must not survive a recycle =="
# The volume outlives the pod and uid 1000 owns most names on it, so a link
# written during one session is input to the *next* start -- which runs as root
# and, before this guard, followed it. Three paths matter: the marker file,
# which root writes with `cat >`; the profile home root, which root chowns on
# its way back up to $DATA; and a tree path, which the prepare step fills and
# the shell then mounts. Each is aimed somewhere that would matter:
# /etc/ld.so.preload is loaded into every process sshd forks, /opt holds the
# credential-proxy shims that start each session's PATH, and a tree pointed into
# the model's scratch space would mount the model's own files as if they had
# shipped.
#
# The running sandbox refuses every plant that goes through a mount point, and
# that is checked first. So the plant is then made offline as uid 1000, which is
# also what a volume from before the upgrade looks like, when the model owned
# all of it. mv rather than rm -rf: the trees inside are root's now, and uid
# 1000 can rename one within a directory it owns but cannot empty it.
PLANT_HOME_LINK='mv /opt/data/profiles /opt/data/profiles.old && ln -s /opt /opt/data/profiles &&
  rm -f /opt/data/.sandbox && ln -s /etc/ld.so.preload /opt/data/.sandbox &&
  ls -ld /opt/data/profiles /opt/data/.sandbox'
check "the running sandbox refuses the home link" "Device or resource busy" \
  "$("${SSH[@]}" "$PLANT_HOME_LINK" 2>&1)"
planted=$(plant_offline "$PLANT_HOME_LINK" 2>&1)
# Without this the whole section passes when the plant silently failed.
check "a volume can carry the links in the first place" "/opt/data/profiles -> /opt" "$planted"
start_sandbox || exit 1
check "the prepare step says it removed the home link" "removed a symlink at /opt/data/profiles" \
  "$PREPARE_LOG"
check "the shell's start says it removed the marker link" "removed a symlink at /opt/data/.sandbox" \
  "$(docker logs "$NAME" 2>&1)"
check "/opt is still root's" "0" "$(docker exec "$NAME" stat -c '%u' /opt 2>&1)"
check "the marker's target was never created" "absent" \
  "$(docker exec "$NAME" sh -c '[ -e /etc/ld.so.preload ] && echo present || echo absent' 2>&1)"
check "the marker is a real file again" "regular file" \
  "$(docker exec "$NAME" stat -c '%F' /opt/data/.sandbox 2>&1)"
check "and holds the marker text" "the shell sandbox's /opt/data" \
  "$(docker exec "$NAME" cat /opt/data/.sandbox 2>&1)"
for home_dir in /opt/data/profiles /opt/data/profiles/platform; do
  check "$home_dir is a directory the agent owns again" "directory 1000" \
    "$(docker exec "$NAME" stat -c '%F %u' "$home_dir" 2>&1)"
done

# The tree path itself, one component further in than the chown walk goes.
# Docker and kubelet bind whatever sits at a subpath, so the prepare step has to
# clear a link at the tree path before it stages the tree, not only the links
# above it.
PLANT_TREE_LINK='mv /opt/data/profiles/platform/scripts /opt/data/profiles/platform/scripts.old &&
  mkdir -p /opt/data/scratch/x && echo "# planted" > /opt/data/scratch/x/forge.py &&
  ln -s /opt/data/scratch/x /opt/data/profiles/platform/scripts &&
  ls -ld /opt/data/profiles/platform/scripts'
check "the running sandbox refuses the tree link" "Device or resource busy" \
  "$("${SSH[@]}" "$PLANT_TREE_LINK" 2>&1)"
planted=$(plant_offline "$PLANT_TREE_LINK" 2>&1)
check "a volume can carry a link at a tree path" \
  "/opt/data/profiles/platform/scripts -> /opt/data/scratch/x" "$planted"
start_sandbox || exit 1
check "the prepare step says it removed the tree link" \
  "removed a symlink at /opt/data/profiles/platform/scripts" "$PREPARE_LOG"
# Removed, not followed: the link's target is still the model's own directory,
# with the model's file in it, rather than chowned to root or overwritten.
check "and left the link's target alone" "1000 # planted" \
  "$(docker exec "$NAME" sh -c 'stat -c %u /opt/data/scratch/x && cat /opt/data/scratch/x/forge.py' 2>&1 | tr '\n' ' ')"
check_trees_from_image "after the planted links"

echo
echo "== 10. a plain file where a home root belongs must not wedge the start =="
# Same volume, same uid 1000, one step sideways from section 9: a regular file
# rather than a link. The symlink pass does not reach it -- `rm` on a symlink is
# not `rm` on a file, deliberately -- and `install -d` exits 71 on a path that
# exists and is not a directory, which `set -e` turns into a start that never
# finishes. Nothing on the volume would have cleared it, so on a volume the model
# owned whole this was a permanent CrashLoopBackOff a session could arrange with
# one `touch`, and the pod you would exec into to undo it is the pod that is
# down. The home root is a mount point now, so the running sandbox refuses it;
# the prepare step still meets whatever an older volume holds.
PLANT_HOME_FILE='mv /opt/data/profiles/platform /opt/data/profiles/platform.old &&
  echo "the model put a file here" > /opt/data/profiles/platform &&
  stat -c %F /opt/data/profiles/platform'
check "the running sandbox refuses the plant" "Device or resource busy" \
  "$("${SSH[@]}" "$PLANT_HOME_FILE" 2>&1)"
planted=$(plant_offline "$PLANT_HOME_FILE" 2>&1)
check "a volume can carry the file in the first place" "regular file" "$planted"
start_sandbox || exit 1
check "the prepare step says it moved it aside" "was not a directory" "$PREPARE_LOG"
check "the home root is a directory the agent owns again" "directory 1000" \
  "$(docker exec "$NAME" stat -c '%F %u' /opt/data/profiles/platform 2>&1)"
check_trees_from_image "after the planted file"
# Moved, not deleted. Broken state either way, but it is the model's own byte.
check "and the displaced copy is still readable" "the model put a file here" \
  "$("${SSH[@]}" 'cat /opt/data/profiles/platform.displaced-*' 2>&1)"

echo
echo "== 11. a directory where the marker belongs must not wedge the start either =="
# The same wedge by the opposite input, and the reason section 10's displacement
# is deliberately not applied here: $DATA/.sandbox has to end up a regular file,
# so "displace anything that is not a directory" would move the marker aside on
# every start. `cat >` fails with EISDIR against a directory, before sshd starts.
planted=$("${SSH[@]}" 'rm -f /opt/data/.sandbox &&
  mkdir -p /opt/data/.sandbox &&
  echo "the model put this here" > /opt/data/.sandbox/kept &&
  stat -c %F /opt/data/.sandbox' 2>&1)
check "the model can plant the directory in the first place" "directory" "$planted"
start_sandbox || exit 1
check "the start says it moved it aside" "was not a regular file" \
  "$(docker logs "$NAME" 2>&1)"
check "the marker is a regular file again" "regular file" \
  "$(docker exec "$NAME" stat -c '%F' /opt/data/.sandbox 2>&1)"
check "and holds the marker text" "shell sandbox's /opt/data" \
  "$("${SSH[@]}" 'cat /opt/data/.sandbox' 2>&1)"
check "the displaced directory kept its contents" "the model put this here" \
  "$("${SSH[@]}" 'cat /opt/data/.sandbox.displaced-*/kept' 2>&1)"

echo
echo "== 12. opening the agent pod's board from here must fail, not return empty =="
# The board lives on the agent pod's volume, and this container's /opt/data is a
# different one. sqlite3 CREATES a database it cannot find, so before the
# tripwire a worker that opened it got no error, no tables and exit 0 -- an empty
# board it had no reason to disbelieve. One did, spent 25 minutes concluding the
# board was unreachable, and left the 0-byte file behind for every later worker
# on the volume. Asserted through sqlite3 rather than `stat` because a directory
# is the mechanism and the raise is the requirement.
start_sandbox || exit 1
check "sqlite3 refuses the path" "unable to open database file" \
  "$("${SSH[@]}" 'python3 -c "
import sqlite3
sqlite3.connect(\"/opt/data/kanban.db\").execute(\"select name from sqlite_master\")
"' 2>&1)"
check "and says where the board actually is" "kanban_show" \
  "$("${SSH[@]}" 'cat /opt/data/kanban.db/NOT-THE-AGENT-POD-DATABASE.txt' 2>&1)"
# The tripwire is the model's, like everything in its home but the image trees.
# Root-owned would buy nothing -- the directory is what makes sqlite3 raise --
# and a worker that removes one has read the note saying why it is there. The
# next start puts it back.
check "the model can still remove a tripwire in a profile home" "gone" \
  "$("${SSH[@]}" 'rm -rf /opt/data/profiles/platform/kanban.db && echo gone' 2>&1)"
# What it cannot remove is the home around the trees. rm -rf descends into each
# read-only tree, fails on the first file there, and leaves every directory above
# it standing: profiles/ and profiles/platform are pinned so the trees cannot
# go with them.
removed=$("${SSH[@]}" 'rm -rf /opt/data/profiles; echo "rc=$?"' 2>&1)
check "a profile home cannot be removed from under its mounts" "Read-only file system" "$removed"
check_absent "and rm says it failed" "rc=0" "$removed"
check "the home and its trees are still there" "intact" \
  "$("${SSH[@]}" 'test -d /opt/data/profiles/platform && test -f /opt/data/profiles/platform/scripts/forge.py && echo intact' 2>&1)"

echo
docker image inspect "$IMAGE" --format '{{len .RootFS.Layers}} {{.Size}}' 2>/dev/null |
  awk '{printf "== %s layers, %.0f MB ==\n", $1, $2/1024/1024}'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
