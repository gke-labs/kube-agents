#!/bin/bash
# ForceCommand for the sandbox's `agent` account. It repairs three things the
# SSH crossing drops and then runs the command sshd would have run anyway: the
# working directory the incoming command is about to cd into, which nothing
# creates on this side; when that directory is a kanban workspace, the
# HERMES_KANBAN_TASK and HERMES_KANBAN_WORKSPACE variables; and HERMES_HOME
# with the kubeconfig pinned in it, from the profile the client named or,
# failing that, from the directory when it is inside a profile home. All three
# are set in the worker's process environment on the agent pod, and no part of
# the SSH backend forwards a process environment. The second and third are
# documented at export_kanban_vars, export_forwarded_profile_home and
# export_profile_home below.
#
# Why it exists. Hermes wraps every terminal command in a preamble whose cd line
# is
#
#     builtin cd -- <cwd> || exit 126
#
# (tools/environments/base.py). Under the local and Docker backends that
# directory is on the same filesystem as the process that created it, so the cd
# always succeeds. Under the SSH backend it is not: tools/environments/ssh.py
# defines no _wrap_command of its own, and its _ensure_remote_dirs creates only
# ~/.hermes and three children — so any other cwd has to already exist on this
# side of the connection, and nothing puts it there. The kanban dispatcher is
# the case that bites. hermes_cli/kanban_db.py mkdirs a per-card scratch
# workspace on the agent pod's PVC and hands the path to the worker as
# TERMINAL_CWD; the sandbox has a different ReadWriteOnce PVC, so every command
# a delegated card runs exits 126 with no output and no explanation.
#
# Upstream treats this as a known defect with no fix in main:
# NousResearch/hermes-agent#86413 (terminal.cwd carries no filesystem
# namespace) and #62169 (the hard exit). The two proposed patches, #62189 and
# #62405, fall back to $HOME instead of creating the directory, which would
# turn the symptom from a loud failure into every card silently running in
# /home/agent. Creating the directory is the right answer against current main
# and against that change, which is why the fix lives here rather than waiting.
#
# It lives in this image rather than in a Hermes source patch so the repository
# gains no new anchor into upstream source. The cost is that the wrapper parses
# a string base.py owns: if that line changes shape, the drift warning at the
# bottom is what says so. See docs/designs/agent-shell-sandboxing.md.
set -u

# The data root sshd handed this session, and where profile homes hang off it.
# Captured before anything rewrites HERMES_HOME: the drop-in
# deploy/sandbox/entrypoint.sh writes sets it to the root, and
# export_profile_home below narrows it to one profile.
SANDBOX_DATA_ROOT=${HERMES_HOME:-/opt/data}
# The directory profile homes hang off, under the data root on both pods
# (sandbox_mirror.PROFILES_DIR), and the component a forwarded profile home is
# read back from.
PROFILES_DIR_NAME=profiles
PROFILES_ROOT="$SANDBOX_DATA_ROOT/$PROFILES_DIR_NAME"
# What cluster_agent_profile.py's step 3 writes into a profile home, on this
# volume. Name matched with sandbox_mirror.CREDENTIALS and cluster_preflight.sh.
PINNED_KUBECONFIG_NAME=kubeconfig.yaml
# The profile home the agent pod's ssh client says it is speaking for: the
# worker's own HERMES_HOME, sent as HERMES_PROFILE_HOME by the agent image's
# client (deploy/docker/ssh-wrapper.sh puts it in the client's environment,
# deploy/docker/ssh_config.d/10-sandbox-profile-home.conf sends it) and
# accepted for this account by sshd_config. Empty when the client sent nothing:
# an agent image without the pair, a HERMES_HOME the client had unset, or a
# caller that is not Hermes' backend.
FORWARDED_PROFILE_HOME=${HERMES_PROFILE_HOME-}

warn() { printf 'sandbox-session-command: %s\n' "$1" >&2; }

# narrow_to_profile <name>: HERMES_HOME to this volume's home for the profile,
# and KUBECONFIG to the kubeconfig pinned inside it when there is one. The tail
# the two derivations below share. Exports nothing and returns 1 for a name
# that is not one path component or that names no home on this volume: the
# caller decides whether that is worth a word.
narrow_to_profile() {
  local name=$1 home kubeconfig
  case $name in
  "" | . | .. | */*) return 1 ;;
  esac
  home="$PROFILES_ROOT/$name"
  [ -d "$home" ] || return 1
  export HERMES_HOME="$home"
  # KUBECONFIG the same way, and only when the file is there. A plain `kubectl`
  # reads it from the environment and nothing else pins it on this side, so
  # without this every command a Cluster Agent runs resolves to whatever context
  # the credential proxy last had rather than to its own cluster —
  # cluster_preflight.sh check 4 is written to catch exactly that. Exporting a
  # path to a file that does not exist would be worse than leaving it unset: it
  # turns "the profile has no credential" into an empty-config error from every
  # kubectl.
  kubeconfig="$home/$PINNED_KUBECONFIG_NAME"
  [ -f "$kubeconfig" ] || return 0
  export KUBECONFIG="$kubeconfig"
}

# HERMES_HOME from the profile the client named, which is the worker's own.
#
# The working directory, which export_profile_home below reads, cannot always
# say which profile is speaking. The kanban dispatcher puts every card's scratch
# workspace at `<root>/kanban/workspaces/<id>`, under no profile home, so a
# Cluster Agent card whose worker runs as `<root>/profiles/cluster-x` used to
# arrive here with nothing but the root to go on: HERMES_HOME stayed the root,
# cluster_preflight.sh check 1 read the default profile's USER.md and reported
# the Cluster Agent had no identity, and the card blocked on every dispatch.
# The agent image's ssh client now sends the worker's HERMES_HOME as
# HERMES_PROFILE_HOME on every connection, and this is where it is read.
#
# Read as a profile *name*, never as a path. The two pods' data roots are
# different volumes that happen to share a path, so the value is rebased: the
# component after the last `/profiles/` is the name, and the home is this
# volume's `<root>/profiles/<name>`, which has to exist already. The client
# therefore picks among the homes the sandbox already has and cannot point
# HERMES_HOME anywhere else — the same rule the cwd path applies. Every use of
# the value is a quoted expansion; nothing here evaluates it.
#
# Three ways to get nothing, and only two of them say so. The root itself, or a
# value with no `/profiles/` in it, is what a worker on the default profile
# sends and what it wants, so it is silent — the root is matched first, by
# value, because a data root can itself have a `profiles` component in its
# path and would otherwise read as a profile named for its last component. A
# name that is not one component (empty, `.`, `..`, a slash) is not a profile
# and is refused aloud. A well-formed name with no home here is a profile the
# sandbox has not received yet: sandbox_mirror.py creates every profile's home
# on the agent pod's start and pushes a Cluster Agent's identity when the
# profile is scaffolded, so a card dispatched inside that window falls back to
# the cwd derivation, and the message says why its preflight is about to read
# the wrong tree.
export_forwarded_profile_home() { # export_forwarded_profile_home <HERMES_PROFILE_HOME>
  local value=$1 name
  case $value in
  "" | "$SANDBOX_DATA_ROOT") return 1 ;;
  */"$PROFILES_DIR_NAME"/*) name=${value##*/"$PROFILES_DIR_NAME"/} ;;
  *) return 1 ;;
  esac
  case $name in
  "" | . | .. | */*)
    warn "ignoring the profile home the client named, which is not a profile: $value"
    return 1
    ;;
  esac
  if [ ! -d "$PROFILES_ROOT/$name" ]; then
    warn "profile $name is not mirrored into the sandbox yet (no $PROFILES_ROOT/$name); HERMES_HOME is derived from the working directory instead"
    return 1
  fi
  narrow_to_profile "$name"
}

# Before the working directory is looked at, and before the interactive branch:
# the profile is the session's, whatever the session runs.
profile_named=0
if export_forwarded_profile_home "$FORWARDED_PROFILE_HOME"; then
  profile_named=1
fi

cmd=${SSH_ORIGINAL_COMMAND-}

# No command means an interactive session. sshd would have started the login
# shell; ForceCommand replaces that, so start it here.
if [ -z "$cmd" ]; then
  exec /bin/bash -l
fi

# Recover the script from `bash -c '<script>'` or `bash -l -c '<script>'`, the
# only two shapes ssh.py's _run_bash sends. Its argument is shlex.quote'd and
# that has an exact inverse: strip the wrapping quotes, then turn every '"'"'
# back into a single quote.
#
# String surgery rather than `eval set -- "$cmd"`, deliberately. eval would
# expand any command substitution in the command line here, and then bash would
# run it again below — one `$(...)` in a path and the side effect happens twice.
script=
case $cmd in
"bash -c '"*"'") script=${cmd#bash -c \'} ;;
"bash -l -c '"*"'") script=${cmd#bash -l -c \'} ;;
*) script= ;;
esac
if [ -n "$script" ]; then
  script=${script%\'}
  script=${script//\'\"\'\"\'/\'}
fi

# The dispatcher sets HERMES_KANBAN_TASK and HERMES_KANBAN_WORKSPACE in the
# worker's process environment, and nothing carries them across the SSH
# connection: ssh.py has no environment handling at all, and the
# `terminal.env_passthrough` config key that would do it is read only by
# code_execution_tool.py and the local and Docker backends. So a worker on this
# backend sees both as empty, and the worker protocol's own instruction —
# `cd $HERMES_KANBAN_WORKSPACE`, unquoted — becomes a bare `cd`, which is not a
# no-op: it goes to $HOME. Three probe cards run in parallel demonstrated it,
# one of them writing its output into the shared /home/agent instead of its own
# workspace, with exit 0 and nothing in the output to say so.
#
# The workspace path is the one place that information does survive the
# crossing, because the cd target *is* the workspace. Recovering the two
# variables from it is deliberately conservative: it derives from the
# `<...>/workspaces/<task id>` prefix rather than the whole path, so a command
# the model runs from a subdirectory still reports the workspace itself, and it
# refuses anything that is not a task id under a kanban `workspaces/` directory
# rather than guessing. Wrong values would be worse than absent ones — a script
# that builds an absolute path from a wrong workspace writes outside it.
#
# Both shapes `workspaces_root()` produces are covered: `<home>/kanban/
# workspaces/<id>` for the default board and `<home>/kanban/boards/<slug>/
# workspaces/<id>` for every other.
export_kanban_vars() { # export_kanban_vars <resolved cwd>
  local path=$1 prefix rest tid
  prefix=${path%%/workspaces/*}
  [ "$prefix" != "$path" ] || return 0
  case $prefix in
  */kanban | */kanban/*) ;;
  *) return 0 ;;
  esac
  rest=${path#"$prefix"/workspaces/}
  tid=${rest%%/*}
  [[ $tid =~ ^t_[0-9a-f]+$ ]] || return 0
  export HERMES_KANBAN_WORKSPACE="$prefix/workspaces/$tid"
  export HERMES_KANBAN_TASK="$tid"
}

# HERMES_HOME, and the kubeconfig pinned inside it, recovered from the cwd the
# same way as the kanban variables and for the same reason — the fallback for a
# client that named no profile.
#
# In the agent container HERMES_HOME names the *profile* home — a worker on the
# platform profile sees `<root>/profiles/platform`, a Cluster Agent sees its own
# — and Hermes sets it in the worker's process environment. Nothing carries a
# process environment across the SSH connection, so the drop-in the entrypoint
# writes has to name a single static value, and it names the root. Every
# profile-scoped script then reads the wrong tree: cluster_preflight.sh is the
# one that shows, checking `<root>/USER.md` and `<root>/kubeconfig.yaml` — the
# default profile's — and reporting the Cluster Agent has no identity, or worse,
# passing on an identity that is not its own.
#
# The cwd is where that information survives when the client sends nothing: a
# command run from a profile home, or from a directory beneath one, belongs to
# that profile. Anything not under `<root>/profiles/<name>` leaves HERMES_HOME
# as sshd set it, which is what the default profile wants — and what a card's
# shared-root workspace gets too, which is why the forwarded name above exists.
#
# PLATFORM_AGENT_HOME is deliberately left alone. It names the agent's data
# root, not a profile home — gitops_workspace.agent_home() says why, and a clone
# under a profile home would fall outside the credential proxy's workspace root.
export_profile_home() { # export_profile_home <resolved cwd>
  local path=$1 rest
  case $path in
  "$PROFILES_ROOT"/*) rest=${path#"$PROFILES_ROOT"/} ;;
  *) return 0 ;;
  esac
  narrow_to_profile "${rest%%/*}" || return 0
}

found=0
if [ -n "$script" ]; then
  while IFS= read -r line; do
    case $line in
    *'builtin cd -- '*) ;;
    *) continue ;;
    esac
    dir=${line#*builtin cd -- }
    # Trim what base.py puts after the target: `|| exit 126` in the command
    # wrapper, `2>/dev/null || true` in the persistent-shell preamble.
    dir=${dir%% ||*}
    dir=${dir%% 2>*}
    [ -n "$dir" ] || continue
    found=1
    # The target is a shell word, not a path. _quote_cwd_for_cd emits a bare
    # `~`, `$HOME`, `$HOME/'a b'`, or a shlex.quote'd absolute path, so it has
    # to be expanded rather than used literally — and expanding it is what eval
    # is for. The guard is what stops a command substitution smuggled into a
    # path from running here as well as inside the command itself.
    case $dir in
    *'$('* | *'`'* | *';'* | *'&'* | *'|'* | *'<'* | *'>'*)
      warn "not creating a working directory from an expression: $dir"
      break
      ;;
    esac
    # `set --` rather than an assignment, so a target that expands to more than
    # one word cannot run its second word as a command. Exactly one word or the
    # wrapper does nothing: _quote_cwd_for_cd always emits one, and a target
    # that splits is a shape this script does not understand.
    if ! eval "set -- $dir" 2>/dev/null || [ "$#" -ne 1 ]; then
      warn "could not resolve the working directory: $dir"
      break
    fi
    resolved=$1
    # A failure here is not fatal, on purpose. A directory that cannot be
    # created leaves the pre-existing behaviour in place — the cd fails and the
    # command exits 126 — and this wrapper must never turn a command that would
    # have worked into one that does not.
    mkdir -p -- "$resolved" 2>/dev/null || warn "could not create $resolved"
    export_kanban_vars "$resolved"
    # The client's word beats the directory's. A worker's HERMES_HOME is the
    # profile it runs as; a cwd under some other profile's home is a place it
    # is working, not who it is.
    [ "$profile_named" -eq 1 ] || export_profile_home "$resolved"
    # Only the first one. The cd line comes before the `eval '<command>'` line
    # that carries the model's own text, so stopping here keeps a command that
    # merely mentions `builtin cd --` from directing an mkdir.
    break
  done <<<"$script"
fi

# No cd line in the script. Either this is not a wrapped command — the
# tar-over-ssh file sync is not, nor is scp — or base.py changed shape under a
# base-image bump and this script has quietly stopped doing anything.
# __hermes_ec is the marker that tells those two apart; it is emitted by the
# same function as the cd line, so a wrapper carrying one and not the other is
# drift and nothing else.
if [ "$found" -eq 0 ]; then
  case $script in
  *__hermes_ec*)
    warn "a Hermes command wrapper arrived with no 'builtin cd --' line; the remote-cwd fix is no longer being applied (deploy/sandbox/session-command.sh)"
    ;;
  esac
fi

# What sshd would have done with no ForceCommand set: the login shell, -c, the
# client's command string. Reproduced rather than approximated, so scp, tar and
# anything else that is not a Hermes wrapper behaves exactly as before.
exec /bin/bash -c "$cmd"
