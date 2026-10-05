#!/bin/sh
# `git` in the sandbox: the credential-free binary at /opt/vcs/libexec/git,
# with the settings vcs_client.local_git() applies to its own calls applied to
# the agent's too. It cannot reach a forge either way -- the image deleted the
# transports -- so what this adds is about the repository in hand, not the
# network. A hook needs no config entry to run, and a working copy's own
# `core.hooksPath` would point git at one; `-c` outranks every config file, so
# the empty, root-owned directory named here is where git looks whatever the
# repository says. `core.fsmonitor` is the other setting that names a program
# git runs on an ordinary `status`.
#
# Not a boundary. A repository-local `filter.<name>.clean` still executes, since
# no `-c` can unset a name it does not know, and the agent can call the binary
# by its absolute path. The containment is the sandbox itself: an unprivileged
# user with no credential. docs/designs/version-control-support.md has the
# argument.
export GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false
exec /opt/vcs/libexec/git \
    -c core.hooksPath=/opt/vcs/share/no-hooks \
    -c core.fsmonitor=false \
    -c protocol.ext.allow=never \
    "$@"
