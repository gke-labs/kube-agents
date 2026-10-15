#!/usr/bin/env python3
# cluster_agent_profile.py - Manage per-cluster Cluster Agent Hermes profiles.
#
# The Platform Agent runs this from its terminal to dynamically create, delete,
# list, and resolve the name of the Cluster Agent profile for a specific GKE
# cluster, inside its own pod. One profile per managed cluster; it persists until
# the cluster is deleted.
#
# Mechanism (verified against the shipped Hermes CLI):
#   - `hermes profile create <name>` registers an isolated profile and stores its
#     home at $HERMES_HOME/profiles/<name>. Because HERMES_HOME is the data PVC
#     (/opt/data) in the pod, profiles persist across restarts automatically.
#   - We then overlay the baked Cluster Agent template (/opt/cluster-template/:
#     SOUL.md, AGENTS.md, config.yaml, skills/) onto that home, pin a kubeconfig
#     scoped to the target cluster, and write the cluster identity into USER.md.
#   - Delegation runs on the shared kanban board (assignee = the profile name);
#     this script only manages profile lifecycle + name resolution, not dispatch.

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import sandbox_exec
from gke_endpoint import EndpointDecision, endpoint_decision
from profile_scaffold import (
    HERMES_BIN,
    backfill_cron_file,
    ensure_profile,
    is_scaffolded,
    overlay_template,
    profiles_base,
)

TEMPLATE_DIR = Path(os.environ.get("CLUSTER_TEMPLATE_DIR", "/opt/cluster-template"))
SHARED_PLUGINS_DIR = Path(os.environ.get("SHARED_PLUGINS_DIR", "/opt/defaults/plugins"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Operator-rendered config overlays and profile-targeted plugin image volumes. The
# entrypoint applies both at pod startup; a profile scaffolded here appears later, so it
# has to pick them up itself (see create_profile steps 2c/2d).
OVERLAY_DIR = Path(os.environ.get("PROFILE_OVERLAY_DIR", "/opt/agent-config"))
PLUGIN_MOUNT_ROOT = Path(os.environ.get("PLUGIN_MOUNT_ROOT", "/opt/agent-plugins"))
# deploy/shared/sandbox_mirror.py, which the image stages here alongside
# profile_overlay.py and profile_plugins.py. Named by path rather than imported:
# it is a CLI, the entrypoint runs it the same way, and step 2e wants its exit
# code and its log line rather than a return value.
SANDBOX_MIRROR = Path(
    os.environ.get("SANDBOX_MIRROR_SCRIPT", "/opt/defaults/scripts/sandbox_mirror.py")
)
# Comfortably past the mirror's own `--wait 30` plus one SSH round trip per
# cluster profile, so this timeout only fires when the mirror itself is stuck.
SANDBOX_MIRROR_TIMEOUT_SECONDS = 120
ENV_HERMES_OTEL_ENABLED = "HERMES_OTEL_ENABLED"
ENV_OTEL_SDK_DISABLED = "OTEL_SDK_DISABLED"
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = profiles_base(HERMES_HOME)

# Files/dirs from the template to overlay onto the created profile home.
OVERLAY_ITEMS = ("SOUL.md", "AGENTS.md", "CAPABILITIES.md", "config.yaml", "skills")
MAX_NAME_LEN = 63

# Non-cluster profiles that live under $HERMES_HOME/profiles but are never
# managed as Cluster Agents: the front-door router (`default`) and the Platform
# Agent itself (`platform`). Reconciliation must never touch these.
RESERVED_PROFILES = frozenset({"default", "platform"})

# How the scaffold checks that gcloud's kubeconfig exists on the side that will
# read it. An absolute path because a builtin `test` would be the sandbox
# shell's, and a round number of seconds because one stat over a multiplexed
# connection either answers immediately or the connection is gone.
KUBECONFIG_PROBE = "/usr/bin/test"
KUBECONFIG_PROBE_TIMEOUT_SECONDS = 30

# What the reconcile engine and the roster read as "this profile is finished".
USER_MD_NAME = "USER.md"
# The one connection attempt the scaffold makes after pinning the kubeconfig,
# so an unreachable cluster is reported here, with the endpoint it was reached
# over, rather than discovered by the first Cluster Agent run. `kubectl
# version` is the cheapest authenticated GET and is on the credential proxy's
# read allowlist. The request timeout is the API server's, so an unreachable
# cluster answers in about 5 s. The outer bound is deliberately shorter than
# the broker's 60 s admission wait: a probe that sits in the broker's queue
# that long says the broker is saturated, not that the cluster is unreachable,
# and the scaffold logs "did not finish" rather than a diagnostic -- and the
# reconcile sweep, which scaffolds clusters one after another, is not held for
# a minute per cluster on a busy broker.
CONNECTIVITY_PROBE_REQUEST_TIMEOUT = "5s"
CONNECTIVITY_PROBE_TIMEOUT_SECONDS = 45
# What kubectl prints when it never reached the API server, as opposed to an
# answer it did not like (401, 403, NotFound). The endpoint remedy is about
# reaching the server, so it is offered only for the first kind.
# kubectl's generic "Unable to connect to the server:" prefix is deliberately
# absent: it also fronts x509 and auth-plugin failures, which the remedy cannot
# fix, and the network causes behind it are matched on their own.
CONNECTIVITY_FAILURE_RE = re.compile(
    r"i/o timeout|timed out|context deadline exceeded|Client\.Timeout|no route to host|"
    r"connection refused|network is unreachable|TLS handshake timeout|dial tcp",
    re.IGNORECASE,
)
# The credential-proxy shim's own messages ("credential proxy unavailable:
# ... Connection refused", "credential proxy: kubeconfig is unreadable") are
# the shim or the broker, not the cluster's list. Matched on the shim's
# prefixes, not the words: preflight's cap text mentions "the credential
# proxy's admission wait" and is a real timeout.
# "Error from server" fronts every answer the API server gave, a degraded
# etcd's "request timed out" included: the server was reached, so the list is
# not what stands in the way whatever words follow.
NOT_A_CONNECTION_FAILURE_RE = re.compile(
    r"credential proxy( token)? unavailable|credential proxy error|credential proxy:|"
    r"Error from server",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(f"[CLUSTER-PROFILE] {msg}", file=sys.stderr)


def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for agent-pod subprocesses: HOME -> /tmp and HERMES_HOME pinned.

    Not for anything crossing into the sandbox. It carries the agent pod's whole
    environment, `API_SERVER_KEY` included; `sandbox_exec.run` takes the handful
    of variables a remote command needs through `remote_env` instead — paths and
    names only, since that route renders them into the sandbox's process table.
    """
    return {**os.environ, "HOME": "/tmp", "HERMES_HOME": str(HERMES_HOME), **(extra or {})}


def _validate(value: str, field: str) -> None:
    if not value or not re.match(r"^[a-zA-Z0-9._-]+$", value):
        raise SystemExit(f"ERROR: invalid {field}: {value!r}")


def profile_name(project: str, cluster: str, location: str) -> str:
    """Derive a stable, sanitized profile name for a target cluster.

    Mirrors the kubeconfig naming convention in platform_mcp_server.switch_kube_context.
    """
    raw = f"cluster-{project}-{cluster}-{location}".lower()
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", raw)).strip("-")
    if len(name) > MAX_NAME_LEN:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_NAME_LEN - 9]}-{digest}"
    return name


def profile_home(name: str) -> Path:
    return PROFILES_BASE / name


def _inject_cluster_identity(home: Path, project: str, cluster: str, location: str) -> None:
    """Write a machine-readable cluster_identity block into the profile's config.yaml.

    Records the target project/cluster/location as structured identity metadata — robust
    vs the sanitized/hashed profile name. (Re-dumping drops the template's comments in this
    per-profile copy, which is fine.) The stamp is load-bearing: `read_cluster_identity` below
    is how cluster_agent_reconcile.py matches a profile to its live cluster, and a profile
    without it is skipped — neither matched nor pruned. It is also what the proposed handover
    producer would read (docs/designs/agent-communication.md, design of record, not implemented).
    """
    import yaml  # lazy: only needed on the scaffold path, keeps the module importable without pyyaml

    config_path = home / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    data["cluster_identity"] = {"project": project, "cluster": cluster, "location": location}
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def read_cluster_identity(home: Path) -> dict[str, str] | None:
    """Read the ``cluster_identity`` block stamped in ``config.yaml``.

    Returns ``{"project": ..., "cluster": ..., "location": ...}``, or None if
    missing/unparseable or the block is absent/incomplete. Raises if the config
    cannot be read (permissions, encoding) or parses to something other than a
    mapping. This is the robust, machine-readable inverse of
    :func:`_inject_cluster_identity` — reconciliation reads it rather than trying
    to reverse the sanitized/hashed profile name.
    """
    import yaml  # lazy: keeps the module importable without pyyaml on pure-lookup paths

    config_path = home / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return None
    identity = data.get("cluster_identity")
    if not isinstance(identity, dict):
        return None
    project, cluster, location = identity.get("project"), identity.get("cluster"), identity.get("location")
    if not (project and cluster and location):
        return None
    return {"project": str(project), "cluster": str(cluster), "location": str(location)}


def _pin_kubeconfig_env(home: Path, kubeconfig: Path) -> None:
    """Pin KUBECONFIG for the dispatcher-spawned worker via the profile's ``.env``.

    A worker launched as ``hermes -p <name>`` rewrites HERMES_HOME to this profile
    home and loads ``<home>/.env`` at startup (Hermes ``get_env_path()`` ==
    ``get_hermes_home()/".env"``), so this is what actually exports KUBECONFIG on
    the dispatch path — the gateway's spawn env never sets it. Without it a
    dispatched Cluster Agent runs kubectl against the wrong/absent cluster.

    Idempotent: rewrites the ``KUBECONFIG`` line in place and preserves any other
    lines already present in ``.env``.
    """
    env_path = home / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    kept = [ln for ln in existing.splitlines(keepends=True) if not ln.startswith("KUBECONFIG=")]
    env_path.write_text("".join(kept) + f"KUBECONFIG={kubeconfig}\n", encoding="utf-8")


def _pin_terminal_env(home: Path, name: str) -> None:
    """Copy the managed terminal settings into the profile's ``.env`` and check them.

    The terminal scope a scheduled run executes under ignores the managed config, so
    without this the profile's cron jobs fall back to Hermes' default local backend
    instead of the sandbox; terminal_env_pin.py has the detail. The entrypoint sweeps every profile
    at start-up, but a cluster profile is created long after that.

    Fatal, and it takes USER.md with it: a re-scaffold may have left one from an earlier
    run, and cluster_agent_reconcile's _scaffold_gaps and the roster both count a profile
    with USER.md as finished. Without it the next reconcile retries the scaffold.
    """
    try:
        from terminal_env_pin import pin  # lazy: imports Hermes

        pin(home)
    except Exception as exc:
        (home / USER_MD_NAME).unlink(missing_ok=True)
        raise SystemExit(
            f"ERROR: scheduled runs in profile '{name}' would not use the managed ssh "
            f"terminal: {exc}. The profile is left unfinished; the next reconcile retries it."
        ) from exc


def _pin_otel_endpoint(home: Path, name: str) -> None:
    """Point this profile's hermes_otel copy at the collector the operator resolved.

    The profile just copytree'd /opt/defaults/plugins, so it carries the endpoint baked
    into the image — and hermes_otel does not read OTEL_EXPORTER_OTLP_ENDPOINT. The
    entrypoint sweeps every profile that exists at startup, but a cluster profile is
    created at onboarding time, long after that, so it has to do this for itself.

    Side benefit: this is the first time a cluster profile gets a service.name at all.

    Never fatal — a deployment without the plugin, or with unwritable telemetry config, is
    still a working Cluster Agent.
    """
    try:
        from otel_config import apply  # lazy, as with yaml above

        config = home / "plugins" / "hermes_otel" / "config.yaml"
        if not config.exists():
            return
        source = SHARED_PLUGINS_DIR / "hermes_otel" / "config.yaml"
        hermes_otel_env = os.environ.get(ENV_HERMES_OTEL_ENABLED, "").strip().lower()
        if hermes_otel_env == "false":
            disabled = True
        elif hermes_otel_env == "true":
            disabled = False
        else:
            disabled = os.environ.get(ENV_OTEL_SDK_DISABLED, "").strip().lower() == "true"

        apply(
            config,
            service_name=os.environ.get("OTEL_SERVICE_NAME") or None,
            endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            source_path=source if source.exists() else None,
            disabled=disabled,
        )
    except Exception as e:  # noqa: BLE001 - telemetry must not fail the scaffold
        log(f"{name}: configuring OpenTelemetry failed ({e}); telemetry config unchanged")


def kubeconfig_landed(kubeconfig: Path) -> bool:
    """Whether the kubeconfig exists and is non-empty, wherever kubectl runs.

    Which filesystem that is depends on the install: with a sandbox it is the
    sandbox's volume and this pod cannot stat it, without one it is this pod's
    own. Asked the same way the credential was fetched, so the answer describes
    the same side.
    """
    if not sandbox_exec.sandbox_enabled():
        return kubeconfig.is_file() and kubeconfig.stat().st_size > 0
    try:
        probe = sandbox_exec.run(
            [KUBECONFIG_PROBE, "-s", str(kubeconfig)],
            check=False,
            timeout=KUBECONFIG_PROBE_TIMEOUT_SECONDS,
        )
    except (sandbox_exec.SandboxUnavailable, OSError, subprocess.TimeoutExpired):
        # The question could not be asked. Reported as an unwritten kubeconfig
        # rather than as a scaffold that finished, because a sandbox that has
        # gone away since the fetch is the case this check is for.
        return False
    return probe.returncode == 0


def _push_sandbox_layout(name: str) -> None:
    """Run the mirror's skeleton pass so this profile exists in the sandbox.

    The shell, the file tools and gcloud all run over SSH in the sandbox pod,
    which mounts its own volume at the same absolute path as this one. It has
    the machine home and nothing below it, and it cannot create the rest for
    itself: the profile list is on this pod's PVC. The entrypoint pushes the
    layout for every profile that exists at startup (step 5.7), and a profile
    scaffolded here exists long after that -- including the kubeconfig directory
    step 3 writes into.

    The same pass delivers each cluster profile's USER.md
    (sandbox_mirror.push_cluster_identities), which is why create_profile runs
    it twice: at step 2e the directory has to exist before gcloud writes into
    it, and at step 5 USER.md finally exists to deliver.

    ``--skeleton-only``: the one-shot migration of the model's files is the
    entrypoint's job and has a marker of its own. A brand new profile has no
    files to move.

    Never fatal. An install with no sandbox exits 0 from the script itself, and
    a sandbox that is down leaves a profile whose shell starts in the machine
    home -- which is where it starts anyway.
    """
    if not SANDBOX_MIRROR.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(SANDBOX_MIRROR),
             "--agent-home", str(HERMES_HOME), "--skeleton-only", "--wait", "30"],
            check=True,
            capture_output=True,
            text=True,
            timeout=SANDBOX_MIRROR_TIMEOUT_SECONDS,
        )
        log(f"{name}: pushed the profile layout into the shell sandbox")
    except Exception as e:  # noqa: BLE001 - no sandbox, or a sandbox that is down
        log(f"{name}: could not push the profile layout into the shell sandbox ({e}); "
            "the next container start retries it")


def _endpoint_bullets(decision: EndpointDecision | None) -> str:
    """The decision as `- key: value` bullets for USER.md, or "" with none.

    Four keys, the last only when there is something to say: `endpoint`,
    `endpoint-address`, `authorized-networks`, `endpoint-remedy`.
    cluster_preflight.sh reads them into check 5's failure report, so the
    keys are a contract with its user_md_field()/user_md_text().
    """
    if decision is None:
        return ""
    lines = [
        f"- endpoint: {decision.kind}",
        f"- endpoint-address: {decision.address}",
        f"- authorized-networks: {decision.authorized_networks_text()}",
    ]
    if decision.remedy:
        lines.append(f"- endpoint-remedy: {decision.remedy}")
    return "\n".join(lines) + "\n"


def _remedy_note(decision: EndpointDecision | None) -> str:
    """The sentence that keeps the remedy bullet from reading as an order.

    USER.md is the Cluster Agent's startup context, and the remedy is written
    for a cluster that may well answer; without this it reads as something to
    go and do.
    """
    if decision is None or not decision.remedy:
        return ""
    return ("The `endpoint-remedy` bullet applies only if `kubectl` cannot reach the API server;\n"
            "a cluster that answers needs nothing done.\n")


def _probe_connectivity(name: str, kubeconfig: Path, decision: EndpointDecision | None) -> bool:
    """One authenticated GET against the pinned kubeconfig; log why it failed.

    Never raises and never fails the scaffold: a cluster that is unreachable
    now may be reachable once the operator acts on the remedy, and a profile
    that was refused here would only be retried on the next tick with the
    same result. The log line is the diagnostic
    docs/designs/private-endpoint-selection.md asks for: the endpoint kind and
    address the kubeconfig names, the authorized-networks list, and the
    remedy, followed by kubectl's own last line.
    """
    argv = ["kubectl", "version", f"--request-timeout={CONNECTIVITY_PROBE_REQUEST_TIMEOUT}"]
    try:
        # As TERMINAL_PRINCIPAL, like the get-credentials that wrote the file:
        # the kubeconfig is `agent`-owned and 0600, and the credential-proxy
        # shim reads it client-side before it talks to the broker. Under the
        # default login the shim refuses with "kubeconfig is unreadable" and
        # the probe would report a permissions problem as the cluster's.
        # sandbox_exec's rule that a reader of command output uses the default
        # login is knowingly bent here, and only here: what this call believes
        # drives one log line, never a decision, a file, or USER.md, so a
        # startup file that forged the answer on an old sandbox image would
        # cost one misleading line in the scaffold log.
        completed = sandbox_exec.run(
            argv,
            remote_env={"KUBECONFIG": str(kubeconfig)},
            local_env=_run_env({"KUBECONFIG": str(kubeconfig)}),
            timeout=CONNECTIVITY_PROBE_TIMEOUT_SECONDS,
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
        )
    except subprocess.TimeoutExpired:
        log(f"{name}: connectivity probe did not finish within "
            f"{CONNECTIVITY_PROBE_TIMEOUT_SECONDS}s; the Cluster Agent's preflight will report")
        return False
    except (sandbox_exec.SandboxUnavailable, OSError) as e:
        log(f"{name}: connectivity probe did not run ({e})")
        return False
    if completed.returncode == 0:
        return True
    # kubectl prints its client version first and the server error last.
    detail = ""
    for line in reversed((completed.stderr or "").splitlines()):
        if line.strip():
            detail = line.strip()
            break
    if decision is None:
        log(f"{name}: cannot reach the cluster API server. kubectl: {detail}")
        return False
    stderr = completed.stderr or ""
    connectivity = bool(CONNECTIVITY_FAILURE_RE.search(stderr)) and not NOT_A_CONNECTION_FAILURE_RE.search(stderr)
    remedy = f" {decision.remedy}" if decision.remedy and connectivity else ""
    log(f"{name}: cannot reach the cluster API server over its {decision.kind} endpoint "
        f"({decision.address}); authorized networks: {decision.authorized_networks_text()}."
        f"{remedy} kubectl: {detail}")
    return False


def create_profile(project: str, cluster: str, location: str) -> str:
    """Scaffold (idempotently) a Cluster Agent profile for a GKE cluster; return its name.

    Shared by the ``create`` CLI subcommand and the reconcile engine. Raises SystemExit on a
    hard failure (missing template, credential fetch failure).
    """
    for field, value in (("project", project), ("cluster", cluster), ("location", location)):
        _validate(value, field)
    name = profile_name(project, cluster, location)

    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"ERROR: cluster template dir not found: {TEMPLATE_DIR}")

    # 1. Register the profile with Hermes (idempotent) via the shared scaffold helper.
    description = f"Read-only Cluster Agent for GKE cluster {cluster} ({project}/{location})."
    home = ensure_profile(name, description, HERMES_HOME)

    # 2. Overlay the Cluster Agent persona, scoped config, and skills (+ shared plugins).
    overlay_template(home, TEMPLATE_DIR, SHARED_PLUGINS_DIR, items=OVERLAY_ITEMS)

    # 2-bis. Backfill default legacy risk onto any unannotated jobs in the profile's
    # cron store if one exists on the volume (e.g. on profile re-scaffold).
    cluster_cron = home / "cron" / "jobs.json"
    if cluster_cron.is_file():
        backfill_cron_file(cluster_cron)

    # 2a. Repoint the plugin copy the overlay just made at the resolved collector.
    _pin_otel_endpoint(home, name)

    # 2b. Stamp this cluster's identity into the profile config as structured identity
    #     metadata — never derived from the sanitized profile name.
    _inject_cluster_identity(home, project, cluster, location)

    # 2c. Link any plugin image volumes the operator mounted for this profile.
    # 2d. Apply the operator's config overlays: the cluster class overlay carrying
    #     spec.harness.tuning.cluster, plus this profile's own if a plugin targets it.
    #
    # Both are startup steps in docker-entrypoint.sh, and startup is not enough. Nothing
    # rolls the pod when a cluster is onboarded — the ConfigMap has not changed — so a
    # profile scaffolded here would otherwise run on Hermes' defaults (3 retries, 90
    # turns) however the CR is tuned, until an unrelated restart. That failure looks like
    # a run that stops mid-task, which is exactly what raising the limits prevents.
    #
    # Applied after the identity stamp: _inject_cluster_identity rewrites the whole
    # config, and the overlay's last-applied record has to describe the file as it
    # finally stands. Neither step is fatal — a deployment without the operator has no
    # /opt/agent-config at all, and a profile on image defaults still works.
    try:
        from profile_plugins import link_plugins  # lazy, as with yaml above

        linked = link_plugins(home, PLUGIN_MOUNT_ROOT, name)
        if linked:
            log(f"{name}: linked plugin volume(s): {', '.join(linked)}")
    except Exception as e:  # noqa: BLE001 - a missing mount must not fail the scaffold
        log(f"{name}: linking targeted plugin volumes failed ({e}); they will not load")

    try:
        from profile_overlay import sync_profile  # lazy, as with yaml above

        log(f"{name}: {sync_profile(home, OVERLAY_DIR)}")
    except Exception as e:  # noqa: BLE001 - a missing overlay dir must not fail the scaffold
        log(f"{name}: overlay sync failed ({e}); running on image defaults")

    # 2e. Give this profile a home in the shell sandbox.
    _push_sandbox_layout(name)

    # 3. Pin a kubeconfig scoped to the target cluster.
    #
    # The gcloud runs in the shell sandbox, so the file it writes lands there
    # rather than in the agent pod — which is where it is needed, because every
    # kubectl this profile goes on to run is a sandbox command too, reading the
    # KUBECONFIG that step 3b pins. Step 2e is what makes the directory it
    # writes into exist on that side; before that landed, this call named an
    # agent-pod path with no counterpart in the sandbox and gcloud said so.
    #
    # As TERMINAL_PRINCIPAL, as only the probe in step 6 otherwise is. Step 2e
    # mirrors the profile directory in as `agent:agent` 0755, so the default
    # `hermes` login (uid 1001) cannot create a file in it and gcloud exits on
    # EACCES. The alternative — making the directory group- or world-writable
    # so uid 1001 could write into a tree uid 1000 owns — is a symlink-follow
    # waiting to happen, and the file has to end up `agent`-readable regardless
    # because the Cluster Agent's own kubectl reads current-context out of it.
    # endpoint_decision below stays on the default login: it consumes gcloud's
    # output as a fact about the cluster, which is what TERMINAL_PRINCIPAL is
    # not for.
    kubeconfig = home / "kubeconfig.yaml"
    env = _run_env({"KUBECONFIG": str(kubeconfig)})
    # Onboarded clusters are arbitrary fleet members: some are reachable only
    # over the DNS endpoint, others only over the private endpoint on this
    # pod's own VPC, others publish a DNS endpoint that refuses external
    # traffic. gke_endpoint reads which before deciding, and the decision is
    # kept so USER.md and the probe below can say what was chosen.
    # `retry_own`: this call asks once and writes a permanent record, so it
    # must not inherit the backoff a failed own-cluster describe leaves for
    # the per-request callers, or one transient would blank the record of
    # every cluster scaffolded in the next minute of a reconcile sweep.
    decision = endpoint_decision(project, cluster, location, env=env, retry_own=True)
    # A provisional decision carries no flag, and the broker, with its own
    # view of the agent cluster, decides again for the unflagged fetch and may
    # splice --internal-ip. Recording this fallback would state an endpoint
    # the kubeconfig does not end up naming, in USER.md and in every preflight
    # report after it, so it is dropped here: no bullets, no remedy, and the
    # probe reports the connection alone.
    # A settled decision that names no address (a gcloud without the flag
    # decided nothing about this cluster) records nothing either.
    if decision is not None and (decision.provisional or not decision.address):
        decision = None
    endpoint_flags = list(decision.flags) if decision is not None else []
    try:
        sandbox_exec.run(
            [
                "gcloud", "container", "clusters", "get-credentials", cluster,
                f"--location={location}", f"--project={project}",
                *endpoint_flags,
            ],
            check=True, timeout=60,
            remote_env={"KUBECONFIG": str(kubeconfig)},
            local_env=env,
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"ERROR: failed to fetch credentials for '{cluster}': {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"ERROR: timed out fetching credentials for '{cluster}'.")
    except sandbox_exec.SandboxUnavailable as e:
        # The command never ran. Distinct from the cases above, which are gcloud
        # answering: a scaffold that reports a credential failure here would send
        # whoever reads it to IAM rather than to the sandbox pod.
        raise SystemExit(f"ERROR: could not reach the shell sandbox to fetch credentials "
                         f"for '{cluster}': {e}")
    except OSError as e:
        # `ssh` (or `gcloud`, unsandboxed) not on PATH or not executable — raised
        # before the process exists, so neither handler above sees it. Same failure
        # mode the `hermes` invocation in profile_scaffold.ensure_profile guards
        # against; keep it to one actionable line instead of a traceback in the
        # container log.
        raise SystemExit(f"ERROR: could not execute 'gcloud' to fetch credentials for '{cluster}': {e}")

    # 3a. Check that the file exists on the side that will read it.
    #
    # gcloud exiting 0 is not the same statement. It writes the kubeconfig in
    # the sandbox, and this pod cannot see that filesystem — so a step 2e that
    # was skipped, a --wait that expired, or a gcloud that wrote somewhere else
    # all leave a profile the scaffold calls finished and every later kubectl
    # fails against, with an error naming the cluster rather than the scaffold
    # that never gave it a credential.
    if not kubeconfig_landed(kubeconfig):
        raise SystemExit(
            f"ERROR: gcloud reported success for '{cluster}' but no kubeconfig is "
            f"at {kubeconfig} where kubectl will look for it. The profile is "
            "scaffolded; re-run this command once the shell sandbox is up."
        )

    # 3b. Pin KUBECONFIG for the dispatcher-spawned worker via the profile's .env.
    _pin_kubeconfig_env(home, kubeconfig)

    # 3c. Pin the managed terminal for the profile's scheduled runs, before USER.md
    # marks the profile finished.
    _pin_terminal_env(home, name)

    # 4. Write the fixed cluster identity into USER.md.
    #
    # Every field is a `- <key>: <value>` bullet because that is the only shape
    # cluster_preflight.sh's user_md_field() can read (it matches
    # `^[[:space:]]*-[[:space:]]*<key>:`). The kubeconfig line used to sit
    # outside the list with no leading `- `, which made it unparseable — and
    # since it is also the line a human reaches for when repairing a bad pin,
    # editing it achieved nothing at all.
    #
    # It stays informational even so: the pin the runtime honours is KUBECONFIG
    # in the profile's .env (step 3b), not this line. Repointing an agent means
    # re-running this scaffold, not editing USER.md.
    #
    # The endpoint bullets are the diagnostic
    # docs/designs/private-endpoint-selection.md describes; preflight check 5
    # reads them when the cluster does not answer.
    (home / USER_MD_NAME).write_text(
        "# Cluster Agent Context\n\n"
        "This Cluster Agent is permanently scoped to the following GKE cluster:\n\n"
        f"- project: {project}\n"
        f"- cluster: {cluster}\n"
        f"- location: {location}\n"
        f"- kubeconfig: {kubeconfig}\n"
        f"{_endpoint_bullets(decision)}\n"
        "The authoritative KUBECONFIG pin lives in this profile's `.env`; the\n"
        "`kubeconfig` bullet above records it for reference. To repoint this agent, re-run\n"
        "`cluster_agent_profile.py create` — do not hand-edit this file.\n"
        f"{_remedy_note(decision)}",
        encoding="utf-8",
    )

    # 5. Deliver that identity into the sandbox, where preflight reads it.
    #
    # cluster_preflight.sh runs over SSH like every other command this agent
    # issues, so it reads USER.md on the sandbox's volume and not the copy
    # written above. The mirror's skeleton pass is what carries it; step 2e ran
    # before this file existed, so it has to run once more now. Cheap and
    # idempotent -- the skeleton is mkdir -p and the identity write overwrites.
    _push_sandbox_layout(name)

    # 6. One connection attempt, now that the kubeconfig and the identity are
    # both where kubectl will look. Informational: see _probe_connectivity.
    _probe_connectivity(name, kubeconfig, decision)
    return name


def cmd_create(args: argparse.Namespace) -> None:
    print(create_profile(args.project, args.cluster, args.location))


def delete_profile(name: str) -> None:
    """Deregister a Hermes profile and remove its home directory.

    Tolerant of an already-absent profile (the ``hermes profile delete`` failure is
    logged, then the home is cleaned up regardless). Shared by the ``delete`` CLI
    subcommand and the reconcile engine.
    """
    home = profile_home(name)
    try:
        # Stays in the agent pod: `hermes` needs the profiles on the data PVC and
        # the gateway on loopback, and the sandbox image does not carry it.
        subprocess.run(
            [HERMES_BIN, "profile", "delete", name, "-y"],
            check=True, capture_output=True, text=True, timeout=30, env=_run_env(),
        )
    except Exception as e:  # noqa: BLE001 - tolerate an already-absent profile
        log(f"'hermes profile delete {name}' failed (continuing to clean up home): {e}")
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)


def list_profiles() -> list[str]:
    """Return sorted names of managed Cluster Agent profiles (excludes reserved profiles)."""
    if not PROFILES_BASE.is_dir():
        return []
    return sorted(
        p.name for p in PROFILES_BASE.iterdir() if p.is_dir() and p.name not in RESERVED_PROFILES
    )


def is_ready_profile(home: Path) -> bool:
    """A profile the dispatcher can hand a card to and its worker can serve.

    is_scaffolded, not is_dir: a plugin mount point can leave a directory under
    profiles/ that Hermes never registered, and a card assigned to it never runs.
    The scaffold artifacts too: create_profile registers the profile and stamps its
    identity before it fetches the credential and writes USER.md, so a scaffold that
    stopped in between is registered, and its worker blocks at preflight.
    """
    return is_scaffolded(home) and (home / USER_MD_NAME).is_file()


def list_ready_profiles() -> list[str]:
    """Return sorted names of active, fully scaffolded Cluster Agent profiles."""
    if not PROFILES_BASE.is_dir():
        return []
    return sorted(
        p.name
        for p in PROFILES_BASE.iterdir()
        if p.is_dir() and p.name not in RESERVED_PROFILES and is_ready_profile(p)
    )


def cmd_delete(args: argparse.Namespace) -> None:
    name = profile_name(args.project, args.cluster, args.location)
    delete_profile(name)
    print(name)


def cmd_list(args: argparse.Namespace) -> None:
    profiles = list_profiles() if getattr(args, "all", False) else list_ready_profiles()
    for name in profiles:
        print(name)


def cmd_name(args: argparse.Namespace) -> None:
    """Print the canonical profile name for a cluster — used as the kanban `assignee`.

    Delegation runs on the kanban board: the Platform Agent creates a card with
    `assignee=<this name>`, and the gateway's kanban dispatcher spawns the profile
    as a worker (`hermes -p <name> chat -q "work kanban task <id>"`). This is a pure,
    deterministic lookup (no side effects), so the assignee can be resolved anytime.
    """
    print(profile_name(args.project, args.cluster, args.location))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage per-cluster Cluster Agent Hermes profiles.")
    sub = parser.add_subparsers(dest="command", required=True)

    help_text = {
        "create": "Create (scaffold) a cluster profile",
        "delete": "Delete a cluster profile",
        "name": "Print the canonical profile name (kanban assignee) for a cluster",
    }
    for cmdname in ("create", "delete", "name"):
        sp = sub.add_parser(cmdname, help=help_text[cmdname])
        sp.add_argument("--project", required=True)
        sp.add_argument("--cluster", required=True)
        sp.add_argument("--location", required=True)

    list_parser = sub.add_parser("list", help="List active, fully scaffolded cluster profiles")
    list_parser.add_argument(
        "--all",
        action="store_true",
        help="Include incomplete/unscaffolded profiles (default: False, lists only ready profiles)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {"create": cmd_create, "delete": cmd_delete, "list": cmd_list, "name": cmd_name}
    handlers[args.command](args)


if __name__ == "__main__":
    main()
