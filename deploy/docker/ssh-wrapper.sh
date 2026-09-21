#!/bin/sh
# ssh for the agent image, installed as /usr/local/bin/ssh ahead of the real
# client on PATH. It puts the profile this process speaks for into the client's
# environment and execs the client; it changes nothing else.
#
# HERMES_HOME in this container names the profile home of the process running
# a command -- a Cluster Agent card's worker sees its own, the gateway sees the
# root. The shell sandbox needs that name on every connection, because a card's
# working directory does not carry it, and the only channel that survives
# Hermes' connection sharing is a SendEnv'd variable read from the environment
# of the ssh process that asked for the session:
# deploy/docker/ssh_config.d/10-sandbox-profile-home.conf says why SetEnv,
# expanded in the client, does not. So the variable has to be in that
# environment, set from HERMES_HOME as it is when ssh is spawned -- which is
# also the one moment that is right whether Hermes handed the worker its
# HERMES_HOME at spawn (kanban_db.py does) or rewrote it in-process after
# start. A mirror at interpreter start would miss the second.
#
# Mirrored exactly: set from HERMES_HOME when that is set and non-empty, unset
# otherwise, so an inherited value never outlives the HERMES_HOME it came from
# and an unset HERMES_HOME sends nothing. The variable is only ever offered to
# the sandbox host the drop-in names, and the sandbox reads it as a profile
# name and refuses anything that is not one of the homes it has.
#
# The two trusted callers on this pod, sandbox_exec.py and sandbox_mirror.py,
# pass -F /dev/null and go through here too; with no SendEnv in their config
# the variable stays in their environment and crosses nowhere.
set -u

# The client this wrapper stands in front of. Absolute, so a PATH that lists
# this directory first cannot make the wrapper exec itself.
readonly REAL_SSH=/usr/bin/ssh

if [ -n "${HERMES_HOME:-}" ]; then
  HERMES_PROFILE_HOME=$HERMES_HOME
  export HERMES_PROFILE_HOME
else
  unset HERMES_PROFILE_HOME
fi
exec "$REAL_SSH" "$@"
