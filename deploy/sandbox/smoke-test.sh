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
# shellcheck disable=SC2016
#   Remote commands are single-quoted throughout and that is the point: the
#   expansion has to happen in the sandbox, not in this shell. A double-quoted
#   `echo "$PATH"` would test the caller's PATH and pass.
#
# No `set -e`: a failing check is data this script reports, not a reason to
# abandon the run. `check` counts them and the exit status at the bottom is the
# verdict.
set -uo pipefail

IMAGE="${1:-agent-sandbox:latest}"
PORT="${2:-12222}"
NAME="sandbox-smoke-$$"
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
  docker rm -f "$NAME" "$NAME-nourl" "$NAME-badsshd" >/dev/null 2>&1
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

# Waits for sshd to answer rather than sleeping: the host-key generation on a
# first start is slow enough on a loaded runner to lose a fixed sleep to, and a
# flaky smoke test gets deleted rather than debugged.
start_sandbox() {
  docker rm -f "$NAME" >/dev/null 2>&1
  docker run -d --name "$NAME" -p "$PORT:2222" \
    -v "$WORK/keys:/etc/ssh-authorized:ro" \
    -v "$DATA_VOL:/opt/data" \
    -v "$SSHD_VOL:/var/lib/sandbox-sshd" \
    -e CREDENTIAL_PROXY_URL=http://127.0.0.1:9999 \
    -e CREDENTIAL_PROXY_TOKEN_FILE=/var/run/secrets/kubeagents/credential-proxy/token \
    "$IMAGE" >/dev/null
  for _ in $(seq 30); do
    ssh-keyscan -p "$PORT" -t ed25519 127.0.0.1 >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "FAIL  sandbox never accepted connections; logs follow" >&2
  docker logs "$NAME" >&2
  return 1
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

echo
echo "== 3. who may log in =="
check "the agent's key works" "agent" "$("${SSH[@]}" whoami 2>&1)"
# sshd's own default, which is the home. It is not where the agent works: Hermes
# is sent TERMINAL_CWD=/opt/data by the operator, because this home is the
# container's ephemeral overlay and everything written here is gone on the next
# pod recycle. The image cannot enforce that — asserted here so the two halves of
# the arrangement are visible together.
check "the session starts in the agent's home" "/home/agent" "$("${SSH[@]}" pwd 2>&1)"
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
# reaching in for a cluster command does not run as the login whose home the
# model owns; see deploy/sandbox/Dockerfile.
HERMES_SSH=(ssh "${SSH_OPTS[@]}" hermes@127.0.0.1)
check "the same key opens a hermes session" "hermes" "$("${HERMES_SSH[@]}" whoami 2>&1)"
check "hermes gets the forwarded proxy URL too" "http://127.0.0.1:9999" \
  "$("${HERMES_SSH[@]}" 'echo "$CREDENTIAL_PROXY_URL"' 2>&1)"
check "hermes gets the wrapper PATH too" "/opt/credential-proxy/bin/kubectl" \
  "$("${HERMES_SSH[@]}" 'command -v kubectl' 2>&1)"

# The property the whole account exists for. Bash sources ~/.bashrc for a
# non-interactive `ssh host cmd`, so a shim planted by the model in its own home
# would be executed by an agent-pod caller and its output returned as a trusted
# tool result. Written above Debian's non-interactive guard, because below it
# the guard hides the problem rather than fixing it.
"${SSH[@]}" 'mkdir -p ~/bin && printf "#!/bin/sh\necho FORGED\n" > ~/bin/kubectl && chmod +x ~/bin/kubectl' >/dev/null 2>&1
"${SSH[@]}" 'printf "export PATH=$HOME/bin:\$PATH\n%s" "$(cat ~/.bashrc)" > ~/.bashrc.new && mv ~/.bashrc.new ~/.bashrc' >/dev/null 2>&1
check_absent "a shim planted in the agent's home does not reach a hermes session" "FORGED" \
  "$("${HERMES_SSH[@]}" 'kubectl version' 2>&1)"
# Same command as the agent proves the planted shim actually works, so the check
# above is testing isolation rather than a typo in the setup.
check "the planted shim does run for the agent itself" "FORGED" \
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
# Generated on the host so the private half never has to be copied back out of
# the container: the model only needs the public half to authorise it.
ssh-keygen -q -t ed25519 -N '' -f "$WORK/rogue" -C sandbox-smoke-rogue
"${SSH[@]}" "printf '%s\n' '$(cat "$WORK/rogue.pub")' >> ~/.ssh/authorized_keys" >/dev/null 2>&1
check "the model can authorise a new key for its own account" "agent" \
  "$(ssh -i "$WORK/rogue" "${SSH_OPTS[@]:2}" agent@127.0.0.1 whoami 2>&1)"
# Asserted as a refusal rather than as the absence of "hermes" in the output:
# sshd's own denial names the account it refused ("hermes@127.0.0.1: Permission
# denied"), so a check for that substring can never pass however well the image
# behaves.
check "the same key does not open a hermes session" "Permission denied" \
  "$(ssh -i "$WORK/rogue" "${SSH_OPTS[@]:2}" hermes@127.0.0.1 whoami 2>&1)"

# Undo the sabotage: later sections use the agent session and would otherwise
# run against a hijacked PATH.
"${SSH[@]}" 'rm -rf ~/bin && sed -i "1{/^export PATH=/d}" ~/.bashrc && sed -i "/sandbox-smoke-rogue/d" ~/.ssh/authorized_keys' >/dev/null 2>&1

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
# follow it. The image stages them at /opt/defaults and the entrypoint syncs them
# onto the volume, because a PVC mounting over /opt/data would otherwise hide
# anything baked there.
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
# The delivery is image-owned. An edit the model makes to a skill script is gone
# at the next start, the same contract the agent pod's force-sync gives; section 6
# is where the restart happens and this is the marker it looks for.
"${SSH[@]}" 'echo "# planted" >> /opt/data/scripts/forge.py' >/dev/null 2>&1
check "the model can edit what it runs" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/data/scripts/forge.py' 2>&1)"

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
hermes_ssh() { # hermes_ssh <cwd-word> <command>: the shape base.py builds
  local script quoted
  script=$(printf 'builtin cd -- %s || exit 126\neval %s\n__hermes_ec=$?\nexit $__hermes_ec' \
    "$1" "'$2'")
  quoted=${script//\'/\'\"\'\"\'}
  "${SSH[@]}" "bash -c '$quoted'"
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
# took it for a literal path would create a directory named '$HOME'.
check "a bare ~ cwd resolves to this pod's home" "/home/agent" \
  "$(hermes_ssh '~' 'pwd' 2>&1)"
check "a \$HOME-relative cwd with a space stays one word" "/home/agent/smoke ws" \
  "$(hermes_ssh "\$HOME/'smoke ws'" 'pwd' 2>&1)"
check_absent "and nothing created a directory named for the variable" '$HOME' \
  "$("${SSH[@]}" 'ls -a / ~' 2>&1)"

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
"${SSH[@]}" "rm -rf $CP /opt/data/scratch/smoke-$$" >/dev/null 2>&1

echo
echo "== 5. credential-proxy wrappers =="
for cli in kubectl gcloud gh git; do
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
check "the wrappers are ahead of anything else on PATH" "/opt/credential-proxy/bin:" \
  "$("${SSH[@]}" 'echo "$PATH"' 2>&1)"
# A login shell runs /etc/profile, which overwrites PATH wholesale; profile.d is
# what puts the wrappers back. Both paths, because only one of them is sshd's.
check "PATH survives /etc/profile in a login shell" "/opt/credential-proxy/bin/kubectl" \
  "$("${SSH[@]}" 'bash -l -c "command -v kubectl"' 2>&1)"

echo
echo "== 5b. the version-control skill's local git =="
# A second git, off PATH, that the version-control skill reaches by absolute
# path to read a clone the broker unpacked here. Asserted as absent from PATH
# first, because that is the property the section is really about: the name
# `git` still belongs to the shim, and every caller in the tree that types it
# still means the shim.
check "the name git still resolves to the shim" "/opt/credential-proxy/bin/git" \
  "$("${SSH[@]}" 'command -v git' 2>&1)"
check "and in a login session too" "/opt/credential-proxy/bin/git" \
  "$("${SSH[@]}" "bash -l -c 'command -v git'" 2>&1)"
check "the local git is a real git" "git version" \
  "$("${SSH[@]}" '/opt/vcs/libexec/git --version' 2>&1)"
# The message, not the exit status: example.invalid resolves nowhere, so an
# https ls-remote fails on an image that still ships git-remote-https too, and a
# check on failure alone would pass there. "not a git command" is git's external
# dispatch failing to find the helper -- the shape it prints when
# /usr/lib/git-core/git exists, which it does here. The Dockerfile guard asserts
# the same thing at build time; this asserts it of the image that was actually
# pulled, over the transport the agent uses.
check "and cannot reach a network" "is not a git command" \
  "$("${SSH[@]}" '/opt/vcs/libexec/git ls-remote https://example.invalid/x.git' 2>&1)"
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
# written in section 3 and this one goes in the home the shell would default to.
"${SSH[@]}" 'touch ~/ephemeral-probe' >/dev/null 2>&1
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
check_absent "the ones in the home did not" "ephemeral-probe" \
  "$("${SSH[@]}" 'ls -a ~' 2>&1)"
# The other side of that line, and the reason step 1a replaces rather than merges:
# the skills, SOPs and shared scripts are image-owned, so the edit section 4b made
# to forge.py has to be gone. Merging would leave a script deleted from the image
# sitting on the volume looking current for as long as the PVC lives.
check_absent "the image-owned trees are back to the image's copy" "planted" \
  "$("${SSH[@]}" 'tail -1 /opt/data/scripts/forge.py' 2>&1)"

echo
echo "== 7. an unconfigured proxy warns, it does not crash =="
# Expected state until #737 Part C makes the credential proxy reachable from
# outside the agent pod: file and code-execution tools still have to work.
docker rm -f "$NAME-nourl" >/dev/null 2>&1
docker run -d --name "$NAME-nourl" -v "$WORK/keys:/etc/ssh-authorized:ro" "$IMAGE" >/dev/null
sleep 3
check "says so in the log" "CREDENTIAL_PROXY_URL is unset" "$(docker logs "$NAME-nourl" 2>&1)"
check "starts sshd anyway" "sshd" "$(docker exec "$NAME-nourl" ps -o comm= -p 1 2>&1)"
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
echo "== 9. a symlink the model plants under /opt/data must not survive a recycle =="
# The volume outlives the pod and uid 1000 owns every name on it, so a link
# written during one session is input to the *next* start — which runs as root
# and, before this guard, followed it. Two live paths: the marker file, which
# root writes with `cat >`, and the profile home root, which root chowns on its
# way back up to $DATA. Both are planted here as the model and both are aimed
# somewhere that would matter: /etc/ld.so.preload is loaded into every process
# sshd forks, and /opt holds the credential-proxy shims that start each
# session's PATH.
planted=$("${SSH[@]}" 'rm -rf /opt/data/profiles && ln -s /opt /opt/data/profiles &&
  rm -f /opt/data/.sandbox && ln -s /etc/ld.so.preload /opt/data/.sandbox &&
  ls -ld /opt/data/profiles /opt/data/.sandbox' 2>&1)
# Without this the whole section passes when the plant silently failed.
check "the model can plant the links in the first place" "/opt/data/profiles -> /opt" "$planted"
start_sandbox || exit 1
check "the start says it removed them" "removed a symlink at /opt/data/profiles" \
  "$(docker logs "$NAME" 2>&1)"
check "/opt is still root's" "0" "$(docker exec "$NAME" stat -c '%u' /opt 2>&1)"
check "the marker's target was never created" "absent" \
  "$(docker exec "$NAME" sh -c '[ -e /etc/ld.so.preload ] && echo present || echo absent' 2>&1)"
check "the marker is a real file again" "regular file" \
  "$(docker exec "$NAME" stat -c '%F' /opt/data/.sandbox 2>&1)"
check "and holds the marker text" "the shell sandbox's /opt/data" \
  "$(docker exec "$NAME" cat /opt/data/.sandbox 2>&1)"
check "the profile home is a real directory again" "directory" \
  "$(docker exec "$NAME" stat -c '%F' /opt/data/profiles 2>&1)"
check "owned by the agent" "1000" \
  "$(docker exec "$NAME" stat -c '%u' /opt/data/profiles 2>&1)"
check "with the image's trees back inside it" "scripts" \
  "$("${SSH[@]}" 'ls /opt/data/profiles/platform' 2>&1)"

echo
echo "== 10. a plain file where a home root belongs must not wedge the start =="
# Same volume, same uid 1000, one step sideways from section 9: a regular file
# rather than a link. The symlink pass does not reach it -- `rm` on a symlink is
# not `rm` on a file, deliberately -- and `install -d` exits 71 on a path that
# exists and is not a directory, which `set -e` turns into a start that never
# finishes. Nothing on the volume would have cleared it, so this was a permanent
# CrashLoopBackOff a session could arrange with one `touch`, and the pod you
# would exec into to undo it is the pod that is down.
planted=$("${SSH[@]}" 'rm -rf /opt/data/profiles/platform &&
  echo "the model put a file here" > /opt/data/profiles/platform &&
  stat -c %F /opt/data/profiles/platform' 2>&1)
check "the model can plant the file in the first place" "regular file" "$planted"
start_sandbox || exit 1
check "the start says it moved it aside" "was not a directory" \
  "$(docker logs "$NAME" 2>&1)"
check "the home root is a directory again" "directory" \
  "$(docker exec "$NAME" stat -c '%F' /opt/data/profiles/platform 2>&1)"
check "with the image's trees back inside it" "scripts" \
  "$("${SSH[@]}" 'ls /opt/data/profiles/platform' 2>&1)"
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
# Sections 9 and 10 plant with `rm -rf /opt/data/profiles`, so a root-owned
# read-only tripwire inside a profile home breaks them -- and sandbox_mirror.py
# with them. It is the model's volume; the tripwire is the model's too.
check "the model can still clear a profile home it stands in" "gone" \
  "$("${SSH[@]}" 'rm -rf /opt/data/profiles && echo gone' 2>&1)"

echo
docker image inspect "$IMAGE" --format '{{len .RootFS.Layers}} {{.Size}}' 2>/dev/null |
  awk '{printf "== %s layers, %.0f MB ==\n", $1, $2/1024/1024}'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
