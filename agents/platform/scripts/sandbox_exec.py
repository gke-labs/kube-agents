#!/usr/bin/env python3
"""Run a cluster command in the shell sandbox rather than in the agent pod.

The agent image carries no `kubectl`, `gcloud`, `gh` or `git`, in any form: not
the binaries, and not the credential-proxy shims that used to stand in for them.
`deploy/docker/Dockerfile` step 2 removed the first, the note where the symlinks
used to be removed the second, and the guard at the end of the `platform` stage
fails the build if either comes back. Agent-side code with a reason to invoke one
— the platform MCP server, the cluster-agent scripts, `gke_endpoint.py`,
`forge.py`, `resolver.py` — calls `run()` here instead of `subprocess.run`, and
the command executes in the sandbox.

Several of those files run on both sides of the boundary: `resolver.py poll` is a
subprocess of the agent pod's cron gate, while `resolver.py claim` is invoked by
the model from a shell that is already in the sandbox. One call site serves both,
because `sandbox_enabled()` is false in the sandbox — the managed config it reads
is an agent-pod file — and `run()` then executes locally.

Two things about this module are load-bearing and easy to undo by accident.

It connects as `hermes`, not as `terminal.ssh_user`. That setting is the login
Hermes gives the model's shell, and it owns its own home directory in the
sandbox; bash sources `~/.bashrc` even for a non-interactive `ssh host cmd`, so
a caller authenticating as it would run the model's startup file before its own
command and could be handed forged output as a trusted tool result. Debian's
stock non-interactive guard at the top of `.bashrc` hides this, and the model
can delete the guard. `deploy/sandbox/Dockerfile` creates the second account.

It does not build the ssh subprocess environment from `os.environ`. The agent
pod holds `API_SERVER_KEY` and `SESSION_KV_API_KEY`, and `_run_env()` in
`agent_common_server.py` — the helper most of these call sites used to pass —
is `{**os.environ, "HOME": "/tmp"}`. Nothing crosses today, because the
sandbox's `sshd_config` sets `PermitUserEnvironment no` and `AcceptEnv LANG
LC_*`, but that is the remote end declining what this end should not offer.
Variables the remote command genuinely needs go through `remote_env`, which
renders them into the command line rather than into the client's environment.

`remote_env` is therefore for paths and names, never for secrets. A command
line is the process's `/proc/<pid>/cmdline`, which every account in the sandbox
can read — the model's own shell included, and it is the party this boundary
exists to keep away from the agent pod's credentials. The two callers pass
`KUBECONFIG`, a path to a file the sandbox already has. A caller with a secret
to hand across has no route here and should not invent one: the credential
proxy holds credentials so that the sandbox never does.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

# The sandbox login for trusted agent-pod callers. Not terminal.ssh_user; see
# the module docstring.
SANDBOX_PRINCIPAL = "hermes"

# The model's own account, and the `principal=` argument almost no caller must
# pass. One key authorises both logins, so this is a username on the command
# line rather than a second credential, and the separation the module docstring
# describes is the only thing keeping them apart.
#
# Two callers pass it, both for the same reason: they have to write inside a
# tree that is `agent:agent` to the leaves, which uid 1001 cannot do.
# `kanban_workspace_gc.py` unlinks scratch workspaces; `cluster_agent_profile.py`
# writes one profile's kubeconfig. The alternative in either case — loosening
# the modes so a shared group could reach in — buys a wider grant than the
# narrower login does, and leaves uid 1001 writing into a tree uid 1000 owns,
# which is a symlink-follow waiting to happen.
#
# What makes it safe there does not generalise: neither caller consumes the
# command's output as a fact about the cluster, and a `.bashrc` that hijacked
# either would be doing to uid 1000's own files what uid 1000 can already do. A
# caller that reads a command's output and believes it must use the default.
TERMINAL_PRINCIPAL = "agent"

MANAGED_CONFIG_PATH = os.environ.get("HERMES_MANAGED_CONFIG_PATH", "/etc/hermes/config.yaml")

# Where a command runs in the sandbox when the caller names no directory.
#
# It has to be inside the credential proxy's workspace root or every proxied
# command fails before it starts: the shim posts its own `os.getcwd()`, and the
# proxy raises "working directory is outside the shared workspace" for anything
# outside (credential_proxy.py, `_execute`). sshd would otherwise drop this
# module's login in /home/hermes, which is outside it — so the four credentialed
# binaries are unreachable from the agent pod without this.
#
# The operator publishes the real value as `terminal.workspace_root`, because it
# is the only party that knows both sides. This constant is the fallback for a
# managed config written before that key existed, and matches what the operator
# sets. Deliberately NOT `HERMES_HOME`: that names a directory in the agent pod,
# and this one is in the sandbox. They are the same string today only because
# deploy/sandbox/Dockerfile creates /opt/data in the sandbox on purpose.
DEFAULT_SANDBOX_CWD = "/opt/data"

# This pod's own home, when HERMES_HOME is unset — the data volume the agent
# container keeps its state on. The same string as DEFAULT_SANDBOX_CWD and a
# separate constant on purpose: that one names a directory on the far side of
# the connection, this one names a directory on this side.
_DEFAULT_HERMES_HOME = "/opt/data"

# Where the host key the sandbox presents is remembered, relative to that home.
_KNOWN_HOSTS_DIR = ".ssh"
_KNOWN_HOSTS_NAME = "known_hosts"

# ssh reserves 255 for its own failures, and a remote command is free to exit
# 255 as well. The two are told apart by what ssh says on stderr when it is the
# one failing, which is the only signal available: a wrapper that appended its
# own exit-code sentinel to stdout would corrupt the output of every command
# that returns anything but text.
_SSH_LEVEL_ERRORS = re.compile(
    r"(ssh: connect to host|Connection (refused|closed|timed out|reset)|"
    r"Could not resolve hostname|Permission denied \(publickey|"
    r"Host key verification failed|kex_exchange_identification|"
    r"Operation timed out|No route to host|Network is unreachable|"
    # A connection that died mid-command rather than failing to open. The
    # first is what ServerAliveCountMax prints when the sandbox pod is
    # evicted under an open session; the second is sshd going away during the
    # banner exchange, which a rolling StatefulSet update produces routinely;
    # the third is the client noticing the socket is gone. Missing them made
    # an unreachable sandbox report as the command exiting 255, which callers
    # render as a cluster fault rather than a retryable one.
    r"Timeout, server not responding|ssh_exchange_identification|"
    r"client_loop: send disconnect|closed by remote host)",
    re.IGNORECASE,
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CONNECT_OPTIONS = (
    # No user ssh config: this connection is fully described here, and a config
    # file appearing under the agent pod's HOME must not be able to redirect it.
    "-F", "/dev/null",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    # Without these a connection to an evicted pod can sit half-open, and a call
    # that should have failed in seconds blocks until the caller's own timeout.
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    # get_cc_pod_diagnostics alone makes three calls. Multiplexing pays the key
    # exchange once and reuses the connection for the rest of the burst.
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=60s",
)


class SandboxMisconfigured(RuntimeError):
    """The managed config is present and unreadable, so where to run is unknown.

    Raised rather than defaulted, because the default is the agent pod and
    running there is the thing the sandbox exists to prevent.
    """


class SandboxUnavailable(RuntimeError):
    """ssh could not reach the sandbox, so the command never ran.

    Distinct from the command running and failing: the caller can retry this
    one, and a diagnostic that reports it as a cluster problem is wrong.
    """


def _load_terminal_config(path: str | None = None) -> dict:
    """Read the `terminal:` block from the operator-managed Hermes config.

    A file that is not there is `{}` and no sandbox — that is the ordinary
    answer inside the sandbox itself, where the managed config is an agent-pod
    file that was never mounted, and it is what lets one call site serve both
    sides of the boundary.

    A file that *is* there and cannot be read is an error. Answering `{}` to
    that reads as "no sandbox configured", and `run()` then executes the
    command locally — model-authored code in the credentialed pod, which is
    the arrangement this module exists to end. The failure is silent, and it
    fires exactly when the operator-managed config is broken.
    """
    config_path = path or MANAGED_CONFIG_PATH
    if not os.path.exists(config_path):
        return {}
    try:
        import yaml  # noqa: PLC0415 — see the ImportError branch below
    except ImportError as exc:
        # Only reachable with a managed config present, so this is the agent
        # pod without PyYAML rather than the sandbox without a config.
        raise SandboxMisconfigured(
            f"{config_path} exists but PyYAML is not installed, so the terminal "
            "backend cannot be read"
        ) from exc
    try:
        with open(config_path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SandboxMisconfigured(f"{config_path} could not be read: {exc}") from exc
    terminal = loaded.get("terminal")
    return terminal if isinstance(terminal, dict) else {}


def sandbox_enabled(path: str | None = None) -> bool:
    """True when the managed config points the shell at an SSH sandbox."""
    return _load_terminal_config(path).get("backend") == "ssh"


def _control_path_dir() -> str:
    """A short, writable directory for the multiplexing control socket.

    Short matters: a unix socket path is capped near 104 bytes and `%C` is
    already a hash, so the directory is the part with room to overflow.
    """
    base = os.environ.get("TMPDIR", "/tmp")
    directory = os.path.join(base, ".sandbox-ssh")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError:
        return ""
    return directory


def _known_hosts_file() -> str:
    """The host-key store, and the directory it has to exist in.

    `StrictHostKeyChecking=accept-new` writes the sandbox's key here the first
    time and refuses a changed one after that. Nothing creates
    `$HERMES_HOME/.ssh`, and with no directory to write into ssh prints
    "Failed to add the host to the list of known hosts" and connects anyway:
    every connection is then a first connection, and the option that reads
    like host-key pinning pins nothing. The warning goes to stderr, which
    `run()` returns to a caller reading it as the command's own output.

    Falls back to the control-socket directory, which is in TMPDIR and so
    remembers the key for the life of the pod rather than the life of the
    volume. Less than the volume gives, more than nothing.
    """
    home = os.environ.get("HERMES_HOME", _DEFAULT_HERMES_HOME)
    for directory in (os.path.join(home, _KNOWN_HOSTS_DIR), _control_path_dir()):
        if not directory:
            continue
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError:
            continue
        return os.path.join(directory, _KNOWN_HOSTS_NAME)
    return ""


def _remote_command(argv: list[str], remote_env: dict[str, str] | None, cwd: str | None) -> str:
    """Render argv into one string for the sandbox's login shell to parse.

    ssh has no argv-preserving mode — the remote shell always re-parses — so
    every element is quoted here. This is a correctness requirement rather than
    a boundary one: the model already has a shell in the sandbox, so it gains
    nothing by injecting into this one, but a pod name containing a quote must
    not silently become a different command.
    """
    parts: list[str] = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")
    if remote_env:
        parts.append("env")
        for name, value in remote_env.items():
            if not _ENV_NAME.match(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
            parts.append(f"{name}={shlex.quote(str(value))}")
    parts.extend(shlex.quote(arg) for arg in argv)
    return " ".join(parts)


def ssh_argv(argv: list[str], *, remote_env: dict[str, str] | None = None,
             cwd: str | None = None, path: str | None = None,
             principal: str = SANDBOX_PRINCIPAL) -> list[str]:
    """Build the full ssh command line for `argv`. Exposed for tests."""
    terminal = _load_terminal_config(path)
    host = terminal.get("ssh_host")
    if not host:
        raise SandboxUnavailable("managed config names no terminal.ssh_host")

    command = ["ssh", *_CONNECT_OPTIONS,
               "-o", "StrictHostKeyChecking=accept-new"]
    known_hosts = _known_hosts_file()
    if known_hosts:
        command += ["-o", f"UserKnownHostsFile={known_hosts}"]

    control_dir = _control_path_dir()
    if control_dir:
        command += ["-o", f"ControlPath={os.path.join(control_dir, '%C')}"]

    key = terminal.get("ssh_key")
    if key:
        # IdentitiesOnly stops ssh offering any agent-held key first and
        # tripping MaxAuthTries before it reaches the one that works.
        command += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    port = terminal.get("ssh_port")
    if port:
        command += ["-p", str(port)]

    if principal not in (SANDBOX_PRINCIPAL, TERMINAL_PRINCIPAL):
        raise ValueError(f"not a sandbox login: {principal!r}")
    command.append(f"{principal}@{host}")
    # A caller that named a directory gets it; everyone else gets the workspace
    # root rather than wherever sshd happens to drop the login. See
    # DEFAULT_SANDBOX_CWD for why the difference is load-bearing.
    if cwd is None:
        published = terminal.get("workspace_root")
        cwd = str(published) if published else DEFAULT_SANDBOX_CWD
    command.append(_remote_command(argv, remote_env, cwd))
    return command


def _client_env() -> dict[str, str]:
    """The environment for the ssh client. Deliberately not `os.environ`."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        # ssh reads HOME for a config this call already suppressed with -F, but
        # an unset HOME makes it complain rather than proceed.
        "HOME": os.environ.get("TMPDIR", "/tmp"),
    }
    for passthrough in ("LANG", "TMPDIR"):
        value = os.environ.get(passthrough)
        if value:
            env[passthrough] = value
    return env


def run(argv: list[str], *, remote_env: dict[str, str] | None = None,
        local_env: dict[str, str] | None = None,
        cwd: str | None = None, timeout: float | None = None,
        check: bool = False, path: str | None = None,
        stdin: str | None = None,
        principal: str = SANDBOX_PRINCIPAL) -> subprocess.CompletedProcess:
    """Run `argv` in the sandbox and return the finished process.

    `principal` selects the sandbox login and should be left alone; see
    `TERMINAL_PRINCIPAL` for the two callers that do not.

    `remote_env` names the variables the command itself needs; they are
    rendered into the remote command line. `local_env` replaces the environment
    of the local fallback for the one caller that has to subtract a variable
    rather than add one — see `_default_runner` in `gke_endpoint.py`, where a
    forwarded `KUBECONFIG` turns a `describe` into a guaranteed HTTP 400. It has
    no remote counterpart because the remote command inherits nothing from here.

    `stdin` is how a document reaches the command without a file. A caller in
    the agent pod that wants `gh` to publish a pull-request body cannot name a
    path: `gh` runs two hops away, in the credential proxy's filesystem, and
    the only thing all three processes share is the byte stream. So the body
    travels as text on fd 0 and the argv says `--body-file -`. ssh forwards fd 0
    to the remote command, and `-T` in `_CONNECT_OPTIONS` keeps it a plain pipe
    rather than a pty that would rewrite the bytes in transit.

    Falls back to running locally when no sandbox is configured. Two different
    situations reach that branch and it is right for both. In the agent pod it
    means the install turned the sandbox off, and the image carries no
    credentialed binary, so the call fails with "command not found" — the honest
    report, and why the fallback is a plain `subprocess.run` rather than an error
    raised here. In the sandbox it is the normal case: `resolver.py` and
    `forge.py` also run there, there is no managed config to read, and local is
    where the command belongs.

    Raises SandboxUnavailable when ssh itself could not connect.
    """
    # `input=None` is not "no stdin". subprocess only redirects fd 0 when it is
    # given something to redirect it to, so with no `stdin` the child inherits
    # this process's — and in the agent pod that fd is platform_mcp_server.py's
    # JSON-RPC channel to its client. ssh reads ahead on fd 0, so a request the
    # client pipelined behind the tool call is swallowed by the ssh that call
    # started: the server never sees it and the client waits for a reply to a
    # message that no longer exists. A caller that passes a document still gets
    # it on a pipe; everyone else gets /dev/null.
    stdin_kwargs: dict[str, object] = (
        {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    )

    if not sandbox_enabled(path):
        base = local_env if local_env is not None else {**os.environ, "HOME": "/tmp"}
        return subprocess.run(argv, capture_output=True, text=True, check=check,
                              timeout=timeout, cwd=cwd,
                              env={**base, **(remote_env or {})}, **stdin_kwargs)

    command = ssh_argv(argv, remote_env=remote_env, cwd=cwd, path=path,
                       principal=principal)
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=timeout, env=_client_env(), **stdin_kwargs)
    if completed.returncode == 255 and _SSH_LEVEL_ERRORS.search(completed.stderr or ""):
        raise SandboxUnavailable(
            f"could not reach the shell sandbox: {(completed.stderr or '').strip()}"
        )
    # The argv the caller passed, not the ssh wrapper. A CalledProcessError or a
    # log line quoting `.args` should name the command that failed rather than
    # the transport that carried it, and callers that already inspect `.args`
    # keep working unchanged. Set before the `check` raise so both paths agree.
    completed.args = argv
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, argv, completed.stdout, completed.stderr
        )
    return completed
