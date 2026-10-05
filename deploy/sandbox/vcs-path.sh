# shellcheck shell=sh
# /etc/profile resets PATH for every login shell, and Hermes' ssh backend takes
# its environment snapshot with `bash -l -c`, so the sshd SetEnv that puts
# /opt/vcs/bin on PATH does not survive into that snapshot on its own. This puts
# it back. entrypoint.sh's SANDBOX_PATH covers the non-login commands.
export PATH="/opt/vcs/bin:${PATH}"
