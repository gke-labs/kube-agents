#!/bin/bash
# ForceCommand for the sandbox's `agent` account. It repairs three things the
# SSH crossing drops and then runs the command sshd would have run anyway: the
# working directory the incoming command is about to cd into, which nothing
# creates on this side; when that directory is a kanban workspace, the
# HERMES_KANBAN_TASK and HERMES_KANBAN_WORKSPACE variables; and when it is
# inside a profile home, HERMES_HOME and the kubeconfig pinned there. All three
# are set in the worker's process environment on the agent pod, and no part of
# the SSH backend forwards a process environment. The second and third are
# documented at export_kanban_vars and export_profile_home below.
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
PROFILES_ROOT="$SANDBOX_DATA_ROOT/profiles"
# What cluster_agent_profile.py's step 3 writes into a profile home, on this
# volume. Name matched with sandbox_mirror.CREDENTIALS and cluster_preflight.sh.
PINNED_KUBECONFIG_NAME=kubeconfig.yaml

warn() { printf 'sandbox-session-command: %s\n' "$1" >&2; }

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

# HERMES_HOME, and the kubeconfig pinned inside it, recovered the same way and
# for the same reason.
#
# In the agent container HERMES_HOME names the *profile* home — a worker on the
# platform profile sees `<root>/profiles/platform`, a Cluster Agent sees its own
# — and Hermes sets it in the worker's process environment. Nothing carries it
# across the SSH connection, so the drop-in the entrypoint writes has to name a
# single static value, and it names the root. Every profile-scoped script then
# reads the wrong tree: cluster_preflight.sh is the one that shows, checking
# `<root>/USER.md` and `<root>/kubeconfig.yaml` — the default profile's — and
# reporting the Cluster Agent has no identity, or worse, passing on an identity
# that is not its own.
#
# The cwd is where that information survives, exactly as it is for the kanban
# variables above: a Cluster Agent's cwd is its profile home or a kanban
# workspace beneath it. Anything not under `<root>/profiles/<name>` leaves
# HERMES_HOME as sshd set it, which is what the default profile wants.
#
# PLATFORM_AGENT_HOME is deliberately left alone. It names the agent's data
# root, not a profile home — gitops_workspace.agent_home() says why, and a clone
# under a profile home would fall outside the credential proxy's workspace root.
export_profile_home() { # export_profile_home <resolved cwd>
  local path=$1 rest name home kubeconfig
  case $path in
  "$PROFILES_ROOT"/*) rest=${path#"$PROFILES_ROOT"/} ;;
  *) return 0 ;;
  esac
  name=${rest%%/*}
  case $name in
  "" | . | ..) return 0 ;;
  esac
  home="$PROFILES_ROOT/$name"
  [ -d "$home" ] || return 0
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
    export_profile_home "$resolved"
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
