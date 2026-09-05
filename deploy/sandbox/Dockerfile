# The agent shell sandbox: the container the agent's terminal, file and
# code-execution tools actually run in, reached over SSH from the agent pod.
# See docs/designs/agent-shell-sandbox.md for why the shell moved out of the
# agent container at all.
#
# Two things this image is deliberately not:
#
#   - It is not the agent image. Nothing here can reach /opt/data, the session
#     database, or a service account token, and that separation is the entire
#     point of #737. It holds only while this image stays free of anything that
#     would give code running here a path back — which is why the credential
#     CLIs below are wrappers and the guard at the end of that stage fails the
#     build if a real one ever appears.
#   - It is not a general-purpose developer image. It carries a shell, Python,
#     an SSH server, and the credential-proxy wrappers. Everything else is a
#     deliberate addition, reviewed on the same terms.
#
# Base args mirror deploy/docker/Dockerfile so this can be rebuilt behind a
# firewall from a mirrored base. The pins live in images.json and
# hack/check-image-inventory.sh fails CI if the two drift apart.
#
# python:3.14-slim rather than a bare distro: execute_code runs `python3
# script.py` in the sandbox (tools/code_execution_tool.py probes for it with
# `command -v python3` and refuses the environment without it), so a Python is
# not optional here. Taking one that is already in the inventory costs nothing
# and adds no new upstream to mirror.
ARG PYTHON_IMAGE=python
ARG PYTHON_VERSION=3.14-slim
FROM ${PYTHON_IMAGE}:${PYTHON_VERSION}

# openssh-server is baked rather than installed by the startup command. The
# prototype this image replaces ran `apt-get install openssh-server` in the
# Sandbox pod's args on every cold start: slow, and a hard dependency on
# reaching a Debian mirror from a namespace whose whole purpose is to have no
# egress worth having.
#
# No rsync, deliberately. Hermes' ssh backend moves files with tar over the
# SSH connection (tools/environments/ssh.py: `tar cf - -C /` on the way back,
# `tar xf -` on the way in) and never shells out to rsync, so shipping it would
# only widen the image.
#
# The rest of the list is deploy/docker/Dockerfile's own apt layer, minus what
# the sandbox must not have. Those packages are in the agent image because the
# agent's shell tools call them, and the shell is what moved here: a skill that
# pipes kubectl through jq works in the agent container today and has to keep
# working. bsdextrautils is the one addition rather than a copy — `column`
# appears 24 times across the shipped skills and is in neither image, because
# trixie split it out of util-linux.
#
# Left behind on purpose: apt-transport-https and gnupg exist in the agent
# image only to add Google's apt repository in the credential-proxy stage, and
# a sandbox that can add an apt repository is a sandbox that can install a real
# gcloud. ca-certificates is already in the python base.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bsdextrautils \
      curl \
      dnsutils \
      iputils-ping \
      jq \
      nano \
      openssh-server \
      patch \
      vim \
      wget && \
    rm -rf /var/lib/apt/lists/*

# The credential-free utility CLIs, at the versions the agent image carried
# before #737 moved them here. This is now their only home — deploy/docker's
# step 2 says so and its guard enforces it — so a version bump has one place to
# happen. k9s did not come across: it is a full-screen terminal UI, and every
# command here arrives over a non-interactive `ssh host -- cmd` with no tty, so
# it could not run even if a skill asked for it. Nothing in agents/ does.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -sSLf -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v4.53.3/yq_linux_${ARCH}" && \
    chmod +x /usr/local/bin/yq && \
    curl -sSLf -o /tmp/helm.tar.gz "https://get.helm.sh/helm-v3.15.2-linux-${ARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH}

# In the agent container `python3` resolves through PATH into the Hermes venv,
# so a shell command there gets these for free; here python3 is the bare base
# image. No skill script imports a third-party module today — the ones that do
# (credential_proxy.py, platform_mcp_server.py, session_kv_server.py,
# profile_cron_tick.py, cluster_agent_profile.py) all run in the agent pod — but
# that is an undeclared contract. Two packages rather than the venv's ~150: a
# script written on the fly parses YAML or makes an HTTP call, and reaches for
# the standard library for everything else.
#
# Not the google-cloud or kubernetes clients, which are the bulk of what is left
# there. Both authenticate before they are useful and the sandbox holds no
# credential for either to find, so they would import and then fail; a skill
# needing one goes through the wrapped CLIs, which is the whole arrangement.
RUN pip install --no-cache-dir pyyaml requests

# A real git with no credential and no way to reach a host.
#
# The sandbox holds no forge token, so anything that talks to a remote runs in
# the broker. The version-control skill is built the other way round: a
# repository arrives as a bundle and is unpacked here with no remote, so `git
# log`, `git show`, `git blame` and `ls-files --stage` against that clone are
# questions about objects this container already holds. Answering them here
# rather than in the broker is what keeps a repository's own history -- and the
# `.git/config` that came with it -- out of the container holding the token.
# See docs/designs/version-control-support.md.
#
# The binary lives at /opt/vcs/libexec/git and is on no PATH at all. It is
# reached by absolute path from vcs.py -- see KUBE_AGENTS_LOCAL_GIT there -- and
# by /usr/lib/git-core, which is git's compiled-in exec path rather than a PATH
# directory. A bare `git` typed by the model still resolves to the credential
# shim, exactly as it did before this layer existed.
#
# Giving the local binary the name `git` instead is the better arrangement and
# it is deliberately not done here. Every shipped caller of bare `git` today --
# gitops_workspace.py, submit_suggestion.py, audit_report.py -- means the shim,
# so the rename has to travel with those callers moving onto the verbs. It goes
# in the consumer-migration change; this one adds a binary and takes nothing
# away. docs/designs/version-control-support.md carries the reasoning, including
# the measurement that motivates it: given the version-control skill and a shim
# still owning the name, an agent answered 8 of 60 read probes through the shim
# alone and issued 4 credentialed clones through it.
#
# What will make putting this git on a PATH safe is that it has no transport,
# and that property is established now so the later change is a PATH edit and
# nothing more. The
# remote helpers are the whole of git's ability to reach a host -- `git fetch`,
# `git clone` and `git push` against an https URL all run git-remote-https --
# and they are separate executables in /usr/lib/git-core, so removing them
# leaves a git that reads and writes local repositories and cannot dial out.
# That is true of the four that reach a network -- git-remote-http is the binary
# and ftp, ftps and https are symlinks to it -- and it is not true of ext and fd,
# whose entries in that directory are symlinks to `git` itself because both are
# builtins. Deleting those two changes nothing: git dispatches a builtin from
# its own table without consulting the filesystem. What actually stops `ext::`
# is git's protocol allowlist, which has defaulted `protocol.ext.allow` to
# `never` since 2.12, and the probe below asserts that refusal by its own
# message rather than pretending an `rm` produced it. `fd::` needs a descriptor
# the parent already opened, so it reaches whatever the caller could reach
# anyway and opens nothing new.
#
# The four server-side helpers in /usr/bin go too, but only for tidiness. They
# are what a *remote* runs, and `git upload-pack` is a builtin of the front-end
# binary, so deleting /usr/bin/git-upload-pack does not remove the capability
# and never did. An earlier version of this layer claimed it did and guarded the
# claim with `git <helper> --help`, which git rewrites to `git help <helper>`
# and execs man; this image has no man, so all four probes exited 128 and the
# guard passed no matter what the image contained.
#
# So the guard is a live probe per transport, and it matches the message rather
# than the exit status. Every one of these URLs fails whatever the image does --
# example.invalid does not resolve -- so an assertion that only checks for
# failure passes on an image with the helpers still installed. The message is
# what separates "there is no transport" from "there is a transport and no
# network". The file:// probe at the end is the other half of that: it fails if
# the git here has been broken outright rather than disarmed, which is the way a
# guard like this otherwise starts passing for the wrong reason.
#
# The two shapes below are both "no helper". Which one git prints depends on
# whether /usr/lib/git-core/git exists -- it does here, so git's external
# dispatch finds it and reports an unknown subcommand; without it the message is
# the classic "Unable to find remote helper". Accept either, so this layer does
# not fail on an unrelated change to the exec-path symlink.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /opt/vcs/libexec && \
    mv /usr/bin/git /opt/vcs/libexec/git && \
    ln -sf /opt/vcs/libexec/git /usr/lib/git-core/git && \
    rm -f /usr/bin/git-receive-pack /usr/bin/git-shell \
          /usr/bin/git-upload-archive /usr/bin/git-upload-pack && \
    rm -f /usr/lib/git-core/git-remote-http /usr/lib/git-core/git-remote-https \
          /usr/lib/git-core/git-remote-ftp /usr/lib/git-core/git-remote-ftps && \
    /opt/vcs/libexec/git --version && \
    for helper in http https ftp ftps; do \
        if [ -e "/usr/lib/git-core/git-remote-${helper}" ]; then \
            echo "sandbox git still has a remote transport: ${helper}" >&2; \
            exit 1; \
        fi; \
        if ! /opt/vcs/libexec/git ls-remote "${helper}://example.invalid/x.git" 2>&1 \
             | grep -qE "'remote-${helper}' is not a git command|Unable to find remote helper"; then \
            echo "sandbox git still resolves a ${helper} transport" >&2; \
            /opt/vcs/libexec/git ls-remote "${helper}://example.invalid/x.git" >&2 2>&1 || true; \
            exit 1; \
        fi; \
    done && \
    if ! /opt/vcs/libexec/git ls-remote 'ext::sh -c whoami' 2>&1 \
         | grep -q "transport 'ext' not allowed"; then \
        echo "sandbox git does not refuse the ext transport" >&2; \
        exit 1; \
    fi && \
    /opt/vcs/libexec/git init -q /tmp/vcs-guard && \
    /opt/vcs/libexec/git ls-remote file:///tmp/vcs-guard >/dev/null && \
    rm -rf /tmp/vcs-guard

# The login the agent connects as. `usermod -p '*'` matters: useradd leaves a
# '!' in /etc/shadow, which sshd reads as a locked account and refuses even for
# public-key auth. '*' means "no password login is possible", which is what is
# actually wanted.
#
# /run/sshd is sshd's privilege-separation directory and must exist before it
# will start.
#
# /opt/data is the model's durable directory, created here so the image is
# runnable without a volume; when the StatefulSet mounts its PVC over the top,
# the entrypoint re-establishes ownership. It carries the same path as the agent
# pod's Hermes home on purpose — the SOPs, skills and model-written scripts that
# name /opt/data resolve on both sides of the SSH connection instead of failing
# on a directory that exists in only one pod. The two are separate volumes and
# nothing is copied between them; the marker file the entrypoint writes is how a
# script or a person tells which side they are on.
#
# /var/lib/sandbox-sshd holds the host keys and is root-owned, deliberately on a
# different volume from the one above. The model owns /opt/data's mount point, so
# it can rename any entry there — a private key on that volume is one the model
# can read today or displace tomorrow, whatever the mode on the file itself.
#
# /home/agent/.hermes is root-owned and unwritable, which is half of what closes
# the sandbox's one write channel back into the agent pod. Hermes' ssh backend
# keeps a file sync over this directory: it pushes at connect and, in
# FileSyncManager.sync_back, tars the whole tree back and copies anything new or
# changed onto the agent pod's Hermes home. Skills are in that tree, so a file
# the model writes here lands in the gateway's skills/ — instructions the next
# session loads into context, on a volume that outlives this pod. It costs
# nothing to stop: nothing in this image reads the pushed copy. The sandbox's
# authoritative skills come from /opt/defaults/skills (see the note further
# down), and what Hermes pushes here is the wrong profile's tree anyway.
#
# The other half is a mount, and it is the half that makes this hold. The mode
# here binds the directory; it does not bind the NAME. /home/agent is owned by
# uid 1000, and removing a directory takes write on the parent rather than on
# the directory, so `rmdir ~/.hermes && mkdir ~/.hermes` returns a writable one
# to the model and the sync resumes on the next command. The operator mounts an
# empty read-only volume over this path (shellSandboxHermesHomePath in
# k8s-operator/internal/controller/shell_sandbox_manifests.go), which makes the
# rmdir EBUSY. A build of this image run outside that StatefulSet keeps the mode
# and loses the guarantee.
#
# The far end degrades quietly, which is why this works from one side.
# _ensure_remote_dirs runs `mkdir -p` and ignores the exit status; the push then
# fails inside FileSyncManager.sync, which catches it, rolls its state back and
# logs a warning; and sync_back returns early on `not self._pushed_hashes and
# not self._synced_files` without opening a connection at all. The cost is that
# warning, once per sync cycle.
RUN useradd --create-home --home-dir /home/agent --shell /bin/bash --uid 1000 agent && \
    usermod -p '*' agent && \
    install -d -m 0700 -o agent -g agent /home/agent/.ssh && \
    install -d -m 0555 -o root -g root /home/agent/.hermes && \
    install -d -m 0755 /run/sshd && \
    install -d -m 0755 -o agent -g agent /opt/data && \
    install -d -m 0700 -o root -g root /var/lib/sandbox-sshd

# The second principal, for trusted code in the agent pod reaching in to run a
# cluster command. It exists because `agent` cannot be used for that: `agent`
# owns its own home directory, bash sources ~/.bashrc for a non-interactive
# `ssh host cmd`, and the model can therefore choose what an agent-pod caller
# sees — shadow a binary on PATH, and the forged output comes back as a trusted
# tool result. Debian's stock guard at the top of .bashrc returns early for
# non-interactive shells and hides this; the model owns the file and can delete
# the guard.
#
# Both principals are authorised by the same key. A second keypair would buy
# nothing: the private half lives only in the agent pod, which the model has no
# way to execute in once the shell, the file tools and execute_code are all
# over here.
#
# --no-user-group and an explicit gid keep this account out of the `agent`
# group. No skeleton files: /etc/skel/.bashrc in this home would be one more
# thing to reason about, and a non-interactive ssh command needs none of them.
# The home is 0700 and owned by hermes, so uid 1000 cannot read or write any
# part of it, .ssh included.
#
# .kubeconfigs is where the platform MCP server's `get-credentials` writes and
# its kubectl reads (platform_mcp_server._thread_kubeconfig_path). It has to be
# somewhere uid 1000 cannot write: a kubeconfig names an `exec` credential
# plugin — a binary and its argv — and kubectl runs it, so a kubeconfig the
# model can author is arbitrary code execution as the one account here it is not
# meant to reach. /opt/data and /tmp are both shared with `agent`. Created in
# the image rather than at first use, which keeps the runtime path to one round
# trip and the mode out of the hands of whatever creates it.
RUN groupadd --gid 1001 hermes && \
    useradd --no-create-home --no-user-group --gid 1001 --uid 1001 \
        --home-dir /home/hermes --shell /bin/bash hermes && \
    usermod -p '*' hermes && \
    install -d -m 0700 -o hermes -g hermes /home/hermes && \
    install -d -m 0700 -o hermes -g hermes /home/hermes/.ssh && \
    install -d -m 0700 -o hermes -g hermes /home/hermes/.kubeconfigs

# The credential-aware CLIs: symlinks to one client that forwards argv to the
# credential proxy over HTTP and prints back what the proxy's own copy of the
# binary produced. The agent never holds the credential.
#
# This is the only image that has them. The agent image carried the same four
# until #737 took them out along with every native CLI, so a credential-proxy
# call can now originate only here; the guard at the end of that image's
# `platform` stage fails the build if any of it reappears there.
#
# credential-proxy-path.sh stays under deploy/shared/, where it was when both
# images used it. It puts /opt/credential-proxy/bin ahead of the native
# binaries on the PATH of login shells. Hermes' ssh backend snapshots
# its environment once with `bash -l -c` and replays that snapshot for every
# later command (tools/environments/base.py), so a file in /etc/profile.d is
# read exactly the way it is for the local backend.
#
# The wrappers need CREDENTIAL_PROXY_URL to point at a reachable proxy. That is
# this pod's loopback when the proxy is a container of the sandbox StatefulSet,
# which the operator does when Workload Identity Federation is configured
# (docs/designs/agent-shell-sandboxing.md). Without it the variable is unset and
# these wrappers resolve, run, and report "credential proxy unavailable" — which
# is the honest answer, and a much better one than a kubectl that is silently
# absent.
COPY --chmod=0755 agents/platform/scripts/credential_proxy_client.py /usr/local/bin/credential-proxy-exec
COPY --chmod=0755 deploy/shared/credential-proxy-path.sh /etc/profile.d/credential-proxy-path.sh
COPY --chmod=0644 deploy/sandbox/sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 deploy/sandbox/entrypoint.sh /usr/local/bin/sandbox-entrypoint
# The ForceCommand the sshd_config names for the `agent` account. Root-owned and
# 0755: the model can run it and cannot rewrite it, which is the point, since
# sshd runs it for every session that account opens.
COPY --chmod=0755 deploy/sandbox/session-command.sh /usr/local/bin/sandbox-session-command

# What the agent's shell actually reads and runs, staged at /opt/defaults for the
# entrypoint to force-sync onto the volume. Same two-part arrangement as the agent
# image (deploy/shared/docker-entrypoint.sh steps 2a/2b): a PVC mounts over
# /opt/data and would otherwise shadow anything baked there, so the image keeps its
# copy somewhere the mount cannot reach and the entrypoint puts it in place on
# every start.
#
# It has to be here at all because the shell moved. A SKILL.md tells the model to
# run `"$HERMES_HOME"/scripts/github_token_refresh.py`; before #737 that resolved
# in the agent container, and now the command runs over SSH in this one. The three
# trees below are what those instructions name.
#
#   skills/     — the skill directories' own scripts/ and assets/. Discovery still
#                 happens in the agent pod, which reads SKILL.md from the platform
#                 profile and puts it in the prompt; what arrives here is only the
#                 part the resulting shell command touches.
#   governance/ — the SOPs. `fleet-audit` cites them by relative path
#                 (`governance/compliance_audit_sop.md`) and the model reads them
#                 with a file tool, which is also over SSH now.
#   scripts/    — the shared modules, as an allowlist rather than the directory.
#
# Hermes' own ssh backend separately syncs a skills tree to /home/agent/.hermes/skills.
# That one is not authoritative and nothing here reads it: iter_skills_files resolves
# its source from HERMES_HOME, which names the machine-level home rather than the
# active profile, so it uploads the *chat* profile's skills — 19 of the platform
# agent's own are missing from it, and the copies it does send lag the image. The
# baked tree below is what $HERMES_HOME and TERMINAL_CWD both point at.
COPY agents/platform/skills/ /opt/defaults/skills/
COPY agents/platform/governance/ /opt/defaults/governance/

# The shared-scripts allowlist: the entry points a SKILL.md names, plus the
# transitive closure of what they import. Enumerated rather than copied wholesale
# because most of agents/platform/scripts/ is agent-pod machinery — the MCP server,
# the gateway relay, the cron tick — and a copy of it here would be code that cannot
# work, sitting where the model will find it and try.
#
#   sandbox_exec.py        the both-sides-safe dispatch; inert here (no
#                          /etc/hermes/config.yaml, so it runs commands locally)
#   forge.py               GitHub REST client, imported by pr_conversation
#   pr_triggers.py         imported by pr_conversation, imports forge
#   github_token_refresh.py  invoked directly by submit-suggestion Step 5
#   wif_credentials.py     imported by github_token_refresh, and reached only by
#                          the branch the proxy container takes; here so that the
#                          import at the top of that script resolves
#   gitops_workspace.py    the leased clone every write skill uses
#   credential_proxy_client.py  imported by submit-suggestion for the content
#                          workspace client. The same file is also COPYed to
#                          /usr/local/bin/credential-proxy-exec above, as the
#                          kubectl/git/gh shim; that copy is not importable by
#                          module name, so it is staged here as well. Content
#                          mode is the one that works from this container at
#                          all: no path crosses the protocol, so nothing depends
#                          on the two ends sharing a filesystem, which they do
#                          not.
#   workspace_paths.py     the workspace protocol's path validator, imported by
#                          inspect-repository to re-check every name at the point
#                          it becomes a write. The broker's content_workspace
#                          delegates to this same file, so the two ends cannot
#                          drift into disagreeing about what is under `.git`
#   gke_endpoint.py        the DNS-endpoint decision gke-networking sends the model
#                          to read; stdlib only, and nothing in it is agent-pod-bound
#   cluster_preflight.sh   the Cluster Agent's first command on every kanban task
#                          (agents/cluster/SOUL.md); its kubectl is a proxy wrapper
#
# agents/platform/scripts/test_sandbox_delivery.py holds this list to the agents'
# instructions: a skill that starts calling a shared script the sandbox does not
# have fails that test rather than failing in a pod.
#
# Deliberately absent, and stubbed below instead: three scripts an agent is told to
# run that cannot run here. cluster_agent_profile.py and cluster_agent_reconcile.py
# shell out to `hermes profile create` and write /opt/data/profiles on the agent
# pod's PVC — cluster_agent_profile.py:325 says so of itself — and
# kanban_notify_propagate.py opens the kanban SQLite database in the agent pod's
# Hermes home. Leaving the paths empty reads as a broken image; the stub says what
# is actually true and exits non-zero.
COPY agents/platform/scripts/sandbox_exec.py \
     agents/platform/scripts/forge.py \
     agents/platform/scripts/pr_triggers.py \
     agents/platform/scripts/github_token_refresh.py \
     agents/platform/scripts/wif_credentials.py \
     agents/platform/scripts/gitops_workspace.py \
     agents/platform/scripts/credential_proxy_client.py \
     agents/platform/scripts/workspace_paths.py \
     agents/platform/scripts/gke_endpoint.py \
     agents/platform/scripts/cluster_preflight.sh \
     /opt/defaults/scripts/
COPY --chmod=0755 deploy/sandbox/agent-pod-only-stub.py /opt/defaults/scripts/cluster_agent_profile.py
COPY --chmod=0755 deploy/sandbox/agent-pod-only-stub.py /opt/defaults/scripts/cluster_agent_reconcile.py
COPY --chmod=0755 deploy/sandbox/agent-pod-only-stub.py /opt/defaults/scripts/kanban_notify_propagate.py

# Three things, in one layer.
#
# The tests come out. The agent image ships them because its copy of the tree is
# also what CI and a developer shell read; nothing here runs them, and
# test_audit_report.py alone is 354 KB. Stripping tests and __pycache__ takes the
# staged skills tree from 2.3 MB to 0.9 MB.
#
# Then the shebang guard. Four scripts in this set used to start
# `#!/opt/hermes/.venv/bin/python3` — a path that exists in the agent image and not
# in this one, so `./audit_report.py` here died with "no such file or directory"
# naming an interpreter rather than the script. They now use /usr/bin/env, and this
# fails the build if one comes back. python:3.14-slim has no /usr/bin/python3 at
# all, so there is no near-miss to fall through to.
#
# Then the executable bit, which `chmod -R u=rwX` does not set — X preserves an
# existing one rather than adding it, so a script committed 644 arrives here
# unrunnable. Five of the ten staged scripts were, `gitops_workspace.py` among
# them, and `./gitops_workspace.py` over ssh answered `Permission denied`. That
# is the same failure the shebang rewrite above was for, one step later: the
# design has these executed directly, so the mode has to say so. The agent image
# does exactly this to the same tree.
#
# Then ownership. /opt/data is agent-owned and the entrypoint copies these into it,
# so the model can edit what it runs and lose the edit at the next restart — the
# same contract the agent pod's force-sync gives. That is not a trust boundary: the
# `hermes` principal must never execute anything under /opt/data, because uid 1000
# owns that mount point and can rename any directory in it aside. What `hermes`
# runs comes off PATH, out of root-owned /opt/credential-proxy/bin.
RUN find /opt/defaults -name '__pycache__' -type d -prune -exec rm -rf {} + && \
    find /opt/defaults -name 'test_*.py' -type f -delete && \
    find /opt/defaults -type d -name tests -prune -exec rm -rf {} + && \
    if grep -rl '^#!/opt/hermes/' /opt/defaults; then \
        echo "the files above name the agent image's venv interpreter, which does not exist here" >&2; \
        exit 1; \
    fi && \
    chown -R agent:agent /opt/defaults && \
    chmod -R u=rwX,go=rX /opt/defaults && \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + && \
    find /opt/defaults/skills -path '*/scripts/*' -type f -exec chmod 755 {} +

# The same guard the agent image runs, for the same reason and with more at
# stake: a real gcloud or kubectl in this image is a credential path out of the
# sandbox that no network policy would see. /opt/credential-proxy/bin is not on
# the build PATH — profile.d puts it there at login — so `command -v` here can
# only be finding a native binary.
#
# `gh` keeps its shim here. Taking it away is the right end state -- a forge CLI
# on PATH is the route out of a forge-neutral abstraction and the model takes
# it, answering through `gh api` on a quarter of the write probes with the
# version-control skill loaded -- but every `gh` caller in the tree still means
# this shim, so its removal travels with them in the consumer-migration change
# rather than landing here ahead of them.
RUN mkdir -p /opt/credential-proxy/bin && \
    for binary in gcloud kubectl gh git; do \
        ln -s /usr/local/bin/credential-proxy-exec "/opt/credential-proxy/bin/${binary}"; \
    done && \
    for binary in gcloud kubectl gh git; do \
        if command -v "${binary}" >/dev/null 2>&1; then \
            echo "unexpected credential-aware CLI in the sandbox image: ${binary}" >&2; \
            exit 1; \
        fi; \
    done

# Not 22: sshd runs on an unprivileged port so this image has the option of
# running as a non-root user later without the port being what blocks it.
EXPOSE 2222

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
