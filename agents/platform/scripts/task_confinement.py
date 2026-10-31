#!/usr/bin/env python3
"""Confine a profile worker's kubectl and gcloud to its task's target (#3889).

A profile worker's tools are the credential-proxy shim, so every credentialed
call it makes arrives at the shared broker as an argv. The dispatcher's
``proxyToken`` (a credential-proxy task credential, task_credential.py) names
the one cluster the task was admitted against, and this module holds each argv
to it before the broker runs anything:

  * **kubectl**: every ``--context``, every ``--kubeconfig`` (a context name by
    the time it arrives; the shim resolves the path in its own pod) and the
    request's ``kubeconfigContext`` must name the target. A call naming none of
    them is given the target's context; it never falls through to the host
    cluster the broker's own kubeconfig names. ``--cluster``, ``--server``,
    ``--user``, ``--insecure-skip-tls-verify`` and the other flags that
    redirect a credential or supply one are refused by name here, as well as by
    the session flag allowlist that runs first.
  * **gcloud**: ``--project`` must be the target project, and a call without
    one gets ``--project=<target>`` (never the broker's default project).
    ``--cluster``, ``--location``, ``--region`` and ``--zone``, where present,
    must be the target's. ``container clusters get-credentials`` must name the
    target cluster, location and project exactly, with no "run as given"
    fallback when the target can't be read. ``--impersonate-service-account``,
    ``--account`` and the other identity flags are refused, as are reads with
    no project scope (the PROJECTLESS lists below). A ``projects/<id>/...``
    resource name in any argument must name the target project.
  * **gcloud is an allowlist of its own.** command_policy's read list says
    what is read-only; GCLOUD_CONFINED_COMMANDS says, for each read this
    module understands, how the command names a project: by ``--project``
    alone, by a positional project id, by a metrics scope, by an Artifact
    Registry or Container Registry image path. A read-list entry missing
    here is refused for a profile caller, so a command added to the read list
    later fails closed until someone says how it names a project.
  * **Local-only commands** (ruling (d), as narrowed to what command_policy
    admits): ``gcloud version``, ``--help`` on a command, ``kubectl version
    --client``, and the ``kubectl config`` reads ``get-contexts`` and
    ``current-context`` (``config view`` is refused: ``--flatten`` prints
    credentials). The policy is asked, not mirrored: a local-only-shaped
    command it refuses (``--help`` on a write such as ``kubectl apply``, or
    on a surface its help escape doesn't cover such as ``gcloud init``) is
    refused with its own rule and message. They run
    isolated, against no kubeconfig and with no get-credentials handling,
    with or without a target (``get-credentials <cluster> --help`` is an
    isolated help read). ``gcloud config``
    and ``gcloud auth`` are refused whole, with or without one: ``config
    list``, ``config get-value`` and ``auth list`` print the broker's own
    account and default project, and ``config set``/``unset``/
    ``configurations`` and every ``kubectl config`` write (``use-context``,
    ``set-context``, ...) change what a later bare command targets.
  * **One parse, one canonical argv.** Each argv is read once, with the
    CLI's flag arity, into the Confined structure the broker's executor
    runs from: command words, flags with their values, the target. A flag
    whose detached value starts with ``-``, or whose value starts with
    ``--``, is refused rather than guessed at, as is an unknown flag. The
    argv the broker runs is rebuilt from that parse: the command words
    first, then every flag as one ``--name=value`` token, then positionals.
    So a check downstream that matches contiguous words (get-credentials)
    or scans for a flag sees what this module saw, and the executor does
    not parse again.
  * **Injection is unconditional.** Every credentialed kubectl call carries
    ``--context=<target>`` and runs on the target's kubeconfig; every
    credentialed gcloud call carries ``--project=<target>``, and a GKE call
    ``--location`` (and a node-pool call ``--cluster``) for the target. So a
    stored default changed by some path this module missed (a gcloud property,
    a kubeconfig's ``current-context``) still can't redirect a call: an
    explicit flag beats both. The caller's own ``--context`` and
    ``--kubeconfig`` (each checked to name the target) are dropped from the
    argv; the one ``--context`` the broker runs is the injected one.
  * **No target** (no ``proxyToken``): local-only commands and nothing
    else, run against no kubeconfig at all.

This is a confinement layer over IAM, not a sandbox. What it cannot parse it
lists as a known limit rather than pretending to cover:

  * a resource named by project *number* (``projects/123456789``) is refused,
    not resolved, since the target is a project id;
  * a project, cluster or location named inside a free-text value (a
    ``--filter`` or ``--format`` expression such as
    ``resource.labels.project_id=other``) is not parsed;
  * resource forms that aren't ``projects/...`` paths, in commands this
    module admits as naming the project by flag alone (a ``logging read``
    filter, say); the forms it does parse are the allowlist's: a positional
    project id, a metrics scope, and ``LOCATION-docker.pkg.dev/PROJECT/...``
    and ``[REGION.]gcr.io/PROJECT/...`` image paths;
  * a zonal read under a regional target (``--zone us-central1-a`` for a
    ``us-central1`` cluster) is refused, since the location must match
    exactly;
  * what a permitted read returns is bounded by the broker identity's IAM, as
    it always was; this narrows which project and cluster a profile names, and
    the IAM grant is still the outer bound.

``storage`` is refused outright for the same reason ``projects list`` is: a
bucket name is global, so a ``gs://`` path names no project to hold it to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import command_policy
from credential_proxy_client import (
    CONTEXT_FLAG,
    END_OF_FLAGS,
    KUBECONFIG_FLAG,
    ClusterTarget,
    parse_gke_context,
)

# The refusal rule id the broker reports for a confinement refusal. A
# local-only-shaped command the read-only policy refuses (_is_local_only)
# carries the policy's rule instead, and the broker answers it with the
# credential denylist's rule when one matches.
RULE_TASK_TARGET = "caller.task-target"

# kubectl flags that redirect the credential (to another server or cluster
# entry) or supply one. All are already outside the session flag allowlist,
# which runs first; they are refused here by name as well so the confinement
# does not rest on an allowlist edit someone makes for another reason.
KUBECTL_REDIRECT_FLAGS = frozenset({
    "--cluster", "--server", "-s", "--user", "--insecure-skip-tls-verify",
    "--token", "--username", "--password", "--client-certificate", "--client-key",
    "--certificate-authority", "--tls-server-name", "--kuberc",
    "--as", "--as-group", "--as-uid", "--as-user-extra",
})

# kubectl reads that touch no cluster: --help on a command and the client's
# own version (`kubectl version` without --client asks the API server). No
# local verbs: `kubectl help` and `kubectl options` are outside
# command_policy's read list, so the broker would refuse them anyway.
KUBECTL_HELP_FLAGS = frozenset({"--help", "-h"})
# The `kubectl config` verbs that only read the kubeconfig and that
# command_policy.KUBECTL_READ_VERBS admits. `config view` is not one
# (`--flatten` prints the credentials), nor are get-clusters and get-users.
# Every other verb (use-context, set-context, set-cluster, set-credentials,
# set, unset, delete-*, rename-context, and whatever a later kubectl adds)
# writes the kubeconfig, and so changes what a later command naming no
# context reaches: refused.
KUBECTL_CONFIG_READ_VERBS = frozenset({"get-contexts", "current-context"})

# gcloud flags that choose the identity a call runs as. command_policy refuses
# them too (_IMPERSONATION_FLAGS, _GCLOUD_IDENTITY_FLAGS); here a profile
# caller has no expected value for any of them, so any presence is refused.
GCLOUD_IDENTITY_FLAGS = frozenset({
    "--impersonate-service-account", "--account", "--configuration",
    "--access-token-file", "--credential-file-override",
    "--authorization-token-file", "--authority-selector",
    # Reads its flags from a file on the broker, where --project or an
    # identity flag would be out of sight.
    "--flags-file",
})

# Scopes above a project. A read naming one has no project to hold to the
# target, so it is refused rather than given --project.
GCLOUD_ORG_SCOPE_FLAGS = frozenset({"--organization", "--folder", "--billing-account"})

# The location and cluster flags a gcloud read may carry. Each value must be
# the target's.
GCLOUD_LOCATION_FLAGS = frozenset({"--location", "--region", "--zone", "-z"})
GCLOUD_CLUSTER_FLAG = "--cluster"
GCLOUD_PROJECT_FLAG = "--project"
GCLOUD_BILLING_PROJECT_FLAG = "--billing-project"
GCLOUD_SCOPE_FLAG = "--scope"

# Ruling (c): reads with no project scope. A surface here is refused whole,
# for the explicit message; GCLOUD_CONFINED_COMMANDS refuses them anyway.
# `auth` and `config` are refused earlier still (GCLOUD_BROKER_STATE_SURFACES),
# with or without a target.
GCLOUD_PROJECTLESS_SURFACES = frozenset({
    "organizations",           # the org itself
    "resource-manager",        # folders, org policies, tags above a project
    "billing",                 # billing accounts are not project resources
    "access-context-manager",  # org-level access policies
    "scc",                     # Security Command Center is org- or folder-scoped
    "identity",                # Cloud Identity groups: the customer's directory
    "domains",
    "storage",                 # bucket names are global; gs:// names no project
    "auth",                    # print-access-token and friends: the broker's credential
    "config",                  # the broker's own configuration
    "info",                    # the broker's account, config and paths
    "components",
    "init",
    "meta",
})
# Ruling (c), command by command, within surfaces that are otherwise
# project-scoped.
GCLOUD_PROJECTLESS_COMMANDS = frozenset({
    ("projects", "list"),
    ("projects", "search"),
})

# gcloud commands that touch nothing remote (ruling (d)), as far as
# command_policy admits them: `gcloud help` and `gcloud topic` are outside its
# read list.
GCLOUD_LOCAL_WORDS = frozenset({"version"})
# `gcloud config` and `gcloud auth` are refused whole for a profile caller:
# the reads print the broker's own account and default project, and the
# writes change what a later bare command targets.
GCLOUD_BROKER_STATE_SURFACES = frozenset({"config", "auth"})
# The `gcloud config` verbs that only read. Every other one (set, unset,
# configurations activate/create/delete, ...) writes the configuration a later
# bare command reads its project, account, region, zone or cluster from:
# refused, with or without a target.
GCLOUD_CONFIG_READ_VERBS = frozenset({"get", "get-value", "list"})
# The GKE groups whose commands take --location, and the one that takes
# --cluster; injected for the target when absent (see _confine_gcloud).
GCLOUD_CONTAINER_LOCATED_GROUPS = frozenset({"clusters", "node-pools", "operations"})
GCLOUD_CONTAINER_CLUSTER_GROUPS = frozenset({"node-pools"})
# --help short-circuits to a synopsis before the command acts (measured;
# command_policy relies on the same). --version is not here: command_policy
# refuses a bare `gcloud --version`, and beside a command nothing measured
# says gcloud stops before acting, so there it is confined like any flag.
GCLOUD_LOCAL_FLAGS = frozenset({"--help", "-h"})
# Single-dash gcloud flags, spelt long in the canonical argv.
GCLOUD_LONG_NAMES = {"-h": "--help", "-z": "--zone", "-q": "--quiet"}
# The values a boolean flag written `--flag=value` counts as set by. Anything
# else (`--help=false`) is not the flag, so it doesn't make a command local.
TRUE_VALUES = (None, "true")

# How a confined gcloud read names a project, for GCLOUD_CONFINED_COMMANDS.
NAMES_BY_FLAG = "flag"            # --project only (checked or injected); positionals are in-project names
NAMES_BY_POSITIONAL = "positional"  # every positional is the project id (or projects/<id>)
NAMES_BY_METRICS_SCOPE = "metrics-scope"  # <id> or locations/<loc>/metricsScopes/<id>
NAMES_BY_IMAGE = "image"          # <loc>-docker.pkg.dev/<project>/... or [<region>.]gcr.io/<project>/...
NAMES_BY_SCOPE = "scope"          # --scope=projects/<id>, required
NAMES_A_CLUSTER = "cluster"       # container clusters: the positional is the cluster (see below)

# The gcloud reads a profile caller may run, each with how it names a project.
# Keyed like command_policy.GCLOUD_READ_COMMANDS (release track included), and
# a subset of it: a read the list admits and this table doesn't is refused,
# so adding a read to command_policy never widens a profile until it is
# classified here. Left out on purpose: `projects list`, `billing budgets
# list`, `info`, and `config`/`auth` (project-less, or the broker's own
# state).
GCLOUD_CONFINED_COMMANDS: dict[tuple[str, ...], str] = {
    ("alpha", "monitoring", "policies", "describe"): NAMES_BY_FLAG,
    ("alpha", "monitoring", "policies", "list"): NAMES_BY_FLAG,
    ("artifacts", "docker", "images", "describe"): NAMES_BY_IMAGE,
    ("artifacts", "repositories", "describe"): NAMES_BY_FLAG,
    ("artifacts", "repositories", "list"): NAMES_BY_FLAG,
    ("asset", "search-all-resources"): NAMES_BY_SCOPE,
    ("beta", "compute", "advice", "calendar-mode"): NAMES_BY_FLAG,
    ("beta", "compute", "advice", "capacity"): NAMES_BY_FLAG,
    ("beta", "compute", "advice", "capacity-history"): NAMES_BY_FLAG,
    ("beta", "monitoring", "metrics-scopes", "describe"): NAMES_BY_METRICS_SCOPE,
    ("beta", "monitoring", "policies", "describe"): NAMES_BY_FLAG,
    ("beta", "monitoring", "policies", "list"): NAMES_BY_FLAG,
    **{("compute", *verb): NAMES_BY_FLAG for verb in (
        ("addresses", "describe"), ("addresses", "list"), ("backend-services", "list"),
        ("disks", "describe"), ("disks", "list"), ("firewall-rules", "describe"), ("firewall-rules", "list"),
        ("forwarding-rules", "describe"), ("forwarding-rules", "list"),
        ("instance-groups", "managed", "describe"), ("instance-groups", "managed", "list"),
        ("instances", "describe"), ("instances", "get-serial-port-output"), ("instances", "list"),
        ("machine-types", "list"), ("networks", "describe"), ("networks", "list"),
        ("networks", "subnets", "describe"), ("networks", "subnets", "list"), ("networks", "subnets", "list-usable"),
        ("project-info", "describe"), ("regions", "describe"), ("regions", "list"), ("reservations", "list"),
        ("routers", "describe"), ("routers", "get-nat-mapping-info"), ("routers", "get-status"), ("routers", "list"),
        ("security-policies", "list"), ("snapshots", "describe"), ("snapshots", "list"),
        ("sole-tenancy", "node-groups", "list"), ("sole-tenancy", "node-groups", "list-nodes"),
        ("target-pools", "list"),
    )},
    # The host project is the positional: `list-associated-resources HOST`.
    ("compute", "shared-vpc", "list-associated-resources"): NAMES_BY_POSITIONAL,
    ("container", "ai", "profiles", "list"): NAMES_BY_FLAG,
    ("container", "ai", "profiles", "manifests", "create"): NAMES_BY_FLAG,
    ("container", "ai", "profiles", "models", "list"): NAMES_BY_FLAG,
    ("container", "clusters", "describe"): NAMES_A_CLUSTER,
    ("container", "clusters", "get-credentials"): NAMES_A_CLUSTER,
    ("container", "clusters", "list"): NAMES_A_CLUSTER,
    ("container", "fleet", "rollouts", "list"): NAMES_BY_FLAG,
    ("container", "fleet", "rolloutsequences", "describe"): NAMES_BY_FLAG,
    ("container", "fleet", "rolloutsequences", "list"): NAMES_BY_FLAG,
    ("container", "get-server-config"): NAMES_BY_FLAG,
    ("container", "node-pools", "describe"): NAMES_BY_FLAG,
    ("container", "node-pools", "list"): NAMES_BY_FLAG,
    ("container", "operations", "list"): NAMES_BY_FLAG,
    ("logging", "read"): NAMES_BY_FLAG,
    ("monitoring", "policies", "describe"): NAMES_BY_FLAG,
    ("monitoring", "policies", "list"): NAMES_BY_FLAG,
    ("projects", "describe"): NAMES_BY_POSITIONAL,
    ("projects", "get-iam-policy"): NAMES_BY_POSITIONAL,
}
_LONGEST_CONFINED_COMMAND = max(len(key) for key in GCLOUD_CONFINED_COMMANDS)

# A metrics scope's full name, and the two registry hosts' image paths: the
# project is the first path segment after the host.
_METRICS_SCOPE_NAME = re.compile(r"\Alocations/[^/]+/metricsScopes/([^/]+)\Z")
_IMAGE_PATH = re.compile(
    r"\A(?:https?://)?(?:[a-z0-9-]+-docker\.pkg\.dev|docker\.pkg\.dev|(?:[a-z]+\.)?gcr\.io)/([^/@:]+)/"
)
# Release tracks precede the surface (command_policy._GCLOUD_RELEASE_TRACKS,
# plus preview, which gcloud also ships).
GCLOUD_RELEASE_TRACKS = frozenset({"alpha", "beta", "preview"})

# A `projects/<id>` path segment, and a GKE cluster's full resource name, in
# any argument: a positional, a flag value, a URL. The id stops at the next
# separator a resource name or an argument list can carry.
_EMBEDDED_PROJECT = re.compile(r"(?:^|[^A-Za-z0-9_-])projects/([^/\s,;'\"()\]]+)")
_EMBEDDED_CLUSTER = re.compile(
    r"(?:^|[^A-Za-z0-9_-])projects/[^/\s]+/(?:locations|zones)/([^/\s]+)/clusters/([^/\s,;'\"()\]]+)"
)


class ConfinementRefusal(Exception):
    """The argv names something outside the task's target, or can't be read.

    The message is for the caller and the log: it names the flag or the rule,
    and the target where saying it helps the model correct the call. It never
    quotes the task credential. ``rule`` is RULE_TASK_TARGET, except for a
    local-only-shaped command the read-only policy refuses (_is_local_only):
    then it is the policy's rule, the message is the policy's, and
    ``policy_argv`` is the canonical argv the policy judged, so the broker can
    answer as its gates would: the credential denylist's rule where one
    matches, the read-only policy gate's own refusal otherwise.
    """

    def __init__(self, message: str, rule: str = RULE_TASK_TARGET, policy_argv: list[str] | None = None):
        super().__init__(message)
        self.rule = rule
        self.policy_argv = policy_argv


@dataclass(frozen=True)
class Confined:
    """What the broker runs for a confined call: the one parse of its argv.

    ``argv`` is canonical (the command words, then each flag as one
    ``--name=value`` token, then positionals) and carries the injected
    ``--context`` or ``--project``. ``kubeconfig_context`` is the context
    kubectl runs against, always the target's for a credentialed kubectl
    call, and None for gcloud and for an isolated call. ``isolated`` means a
    local-only command, or no target: run against no kubeconfig, with no
    get-credentials handling. ``get_credentials`` says the command is a
    credentialed ``container clusters get-credentials`` (never set on an
    isolated call: ``get-credentials --help`` is a help read). The executor reads these fields
    for a profile call rather than parsing ``argv`` again.
    """

    argv: list[str]
    kubeconfig_context: str | None
    isolated: bool
    executable: str
    words: tuple[str, ...]
    flags: tuple[tuple[str, str | None], ...]
    target: ClusterTarget | None
    get_credentials: bool


def target_from_claims(project: str, location: str, cluster: str) -> ClusterTarget | None:
    """The claims' target as a GKE context triple, or None if it isn't one.

    The token's target fields are bounded printable ASCII; a GKE context name
    is narrower (parse_gke_context). A target that doesn't round-trip through
    that grammar can't be named to kubectl, so the caller treats it as no
    target at all.
    """
    target = ClusterTarget(project=project, location=location, cluster=cluster)
    return target if parse_gke_context(target.context_name) == target else None


# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------


def _kubectl_tables():
    # The session flag allowlist's tables, which every profile kubectl argv
    # has already passed (session_kubectl_flag_refusal). Imported here, not at
    # module scope, because credential_proxy imports this module.
    import credential_proxy

    return (
        credential_proxy.SESSION_KUBECTL_VALUE_FLAGS,
        credential_proxy.SESSION_KUBECTL_BOOLEAN_FLAGS,
        credential_proxy.SESSION_KUBECTL_SHORTHANDS,
    )


def _detached_value(argv: list[str], index: int, name: str) -> str:
    """The value a flag takes from the next element, or a refusal.

    A missing value, or one that starts with `-`, is ambiguous: the CLI may
    read it as the value and a second reader as a flag (`-n --kubeconfig=x`),
    or the other way round. Refused, so nothing downstream has to agree with
    a guess; `--name=value` says it unambiguously.
    """
    if index >= len(argv):
        raise ConfinementRefusal(f"{name} needs a value")
    value = argv[index]
    if value.startswith("-"):
        raise ConfinementRefusal(
            f"the value after {name} starts with '-', so it can't be told from a flag; "
            f"write {name}=<value> for a profile task"
        )
    return value


def _attached_value(name: str, value: str) -> str:
    """An `=value` (or attached shorthand) value, refused if it reads as a long flag."""
    if value.startswith("--"):
        raise ConfinementRefusal(f"the value of {name} looks like a flag, which a profile task may not pass")
    return value


def _flag_token(name: str, value: str | None) -> str:
    return name if value is None else f"{name}={value}"


def _kubectl_walk(argv: list[str]) -> tuple[list[tuple[str, str | None]], list[str], list[str] | None]:
    """kubectl's flags (name, value), bare words, and what follows `--`.

    pflag's grammar: a value flag takes `=value` or the next token; a
    single-dash token is a cluster of shorthands, each boolean consuming
    nothing and the first value-taking one swallowing the rest of the token or
    the next element. Shorthands are reported by their long name. A redirect
    flag is reported by name wherever it sits. An unknown flag is reported
    with no value: its arity is unknown, and the caller refuses it. A value
    that could be a flag is refused here (_detached_value, _attached_value).
    The third element is the words after `--` (None when there is none).
    """
    value_flags, _, shorthands = _kubectl_tables()
    flags: list[tuple[str, str | None]] = []
    words: list[str] = []
    rest: list[str] | None = None
    index = 1
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == END_OF_FLAGS:
            rest = list(argv[index:])
            break
        if token.startswith("--"):
            name, separator, value = token.partition("=")
            if separator:
                flags.append((name, _attached_value(name, value)))
            elif name in value_flags:
                flags.append((name, _detached_value(argv, index, name)))
                index += 1
            else:
                flags.append((name, None))
            continue
        if token.startswith("-") and len(token) > 1:
            cluster = token[1:]
            position = 0
            while position < len(cluster):
                letter = cluster[position]
                long_name = shorthands.get(letter)
                if long_name is None:
                    flags.append(("-" + letter, None))
                    break
                if long_name not in value_flags:
                    flags.append((long_name, None))
                    position += 1
                    continue
                attached = cluster[position + 1:]
                if attached.startswith("="):
                    attached = attached[1:]
                if attached:
                    flags.append((long_name, _attached_value(long_name, attached)))
                else:
                    flags.append((long_name, _detached_value(argv, index, long_name)))
                    index += 1
                break
            continue
        words.append(token)
    return flags, words, rest


def _is_local_only(
    executable: str, flags: list[tuple[str, str | None]], words: list[str], canonical: list[str],
) -> bool:
    """The one local-only decision, for kubectl and gcloud alike (#4298).

    A command is local-only when its shape says so (``--help``, ``version``,
    the ``kubectl config`` reads) *and* command_policy admits its canonical
    argv. A local-only-shaped command the policy refuses (``kubectl apply
    --help``, ``gcloud init --help``) is refused here with the policy's own
    rule and message, with or without a target, rather than run isolated into
    a refusal under another rule, or promised by a confinement message.
    """
    shaped = _kubectl_is_local(flags, words) if executable == "kubectl" else _gcloud_is_local(flags, words)
    if not shaped:
        return False
    decision = command_policy.evaluate(canonical)
    if not decision.allowed:
        raise ConfinementRefusal(decision.message, rule=decision.rule_id, policy_argv=list(canonical))
    return True


def _kubectl_is_local(flags: list[tuple[str, str | None]], words: list[str]) -> bool:
    if words and any(name in KUBECTL_HELP_FLAGS and value in TRUE_VALUES for name, value in flags):
        return True
    if words[:1] == ["config"]:
        return len(words) > 1 and words[1] in KUBECTL_CONFIG_READ_VERBS
    if words[:1] == ["version"]:
        return any(name == "--client" and value in (None, "true") for name, value in flags)
    return False


def _confine_kubectl(argv: list[str], kubeconfig_context: str | None, target: ClusterTarget | None) -> Confined:
    flags, words, rest = _kubectl_walk(argv)
    value_flags, boolean_flags, _ = _kubectl_tables()
    for name, _ in flags:
        if name in KUBECTL_REDIRECT_FLAGS:
            raise ConfinementRefusal(
                f"{name} is refused for a profile task: the cluster and the credential are the task's, not the caller's"
            )
        # The session allowlist has refused anything else already; refused
        # here too, so the walk above never steps over a flag whose arity it
        # guessed.
        if name not in value_flags and name not in boolean_flags:
            raise ConfinementRefusal(f"{name} is not a flag the broker can read for a profile task")
    # Before the target check, so it holds with or without one, and before
    # the help check: a write is refused however it is spelled.
    if words[:1] == ["config"] and len(words) > 1 and words[1] not in KUBECTL_CONFIG_READ_VERBS:
        raise ConfinementRefusal(
            f"kubectl config {words[1][:32]} changes the kubeconfig a later command reads, or prints its "
            "credentials, so it is refused for a profile task; kubectl config get-contexts and "
            "current-context still run"
        )
    contexts = [value for name, value in flags if name == CONTEXT_FLAG]
    kubeconfigs = [value for name, value in flags if name == KUBECONFIG_FLAG]
    # The canonical argv: the words, then the caller's flags as single
    # tokens, then whatever followed `--`. The caller's --context and
    # --kubeconfig are checked below and dropped; the target's --context is
    # the one the broker runs.
    kept = [_flag_token(name, value) for name, value in flags if name not in (CONTEXT_FLAG, KUBECONFIG_FLAG)]
    tail = [END_OF_FLAGS, *rest] if rest is not None else []
    isolated_argv = [argv[0], *words, *kept, *tail]
    local = _is_local_only("kubectl", flags, words, isolated_argv)

    def isolated() -> Confined:
        return Confined(
            argv=isolated_argv, kubeconfig_context=None, isolated=True, executable="kubectl",
            words=tuple(words), flags=tuple(flags), target=target, get_credentials=False,
        )

    if target is None:
        if not local:
            raise ConfinementRefusal(
                "this profile task has no target cluster (no valid task credential), so only local-only "
                "commands run, the ones the read-only policy admits: kubectl version --client, kubectl "
                "config get-contexts and current-context, and --help on a command it admits"
            )
        if contexts or kubeconfigs or kubeconfig_context:
            raise ConfinementRefusal("this profile task has no target cluster, so a command may not name one")
        return isolated()
    for value in contexts:
        if value != target.context_name:
            raise ConfinementRefusal(
                f"--context must name this task's cluster, {target.context_name}"
            )
    for value in kubeconfigs:
        if value is None or parse_gke_context(value.strip()) != target:
            raise ConfinementRefusal(
                f"--kubeconfig must name this task's cluster, {target.context_name}"
            )
    if kubeconfig_context is not None and parse_gke_context(kubeconfig_context.strip()) != target:
        raise ConfinementRefusal(
            f"the kubeconfig's context must be this task's cluster, {target.context_name}"
        )
    # A local-only command runs isolated with a target too: it needs no
    # cluster, so it is given none.
    if local:
        return isolated()
    # The target's context, every time, whatever the call named (each name
    # was checked equal to it above): without this the broker resolves the
    # host cluster the operator named for its own kubeconfig. And it is
    # named in argv as well: the kubeconfig the broker hands kubectl is the
    # target's, and --context makes kubectl select the target's entry in it
    # whatever its current-context says, so a default changed by a write
    # this module failed to refuse can't move the call.
    confined = [argv[0], *words, f"{CONTEXT_FLAG}={target.context_name}", *kept, *tail]
    return Confined(
        argv=confined, kubeconfig_context=target.context_name, isolated=False, executable="kubectl",
        words=tuple(words), flags=tuple(flags), target=target, get_credentials=False,
    )


# ---------------------------------------------------------------------------
# gcloud
# ---------------------------------------------------------------------------


def _gcloud_walk(argv: list[str]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """gcloud's flags (name, value) and positionals, with command_policy's arity.

    The same arity command_policy._gcloud_words_and_flag uses, except that a
    detached value that looks like a flag (`--format --help`) is refused
    rather than read as the value (_detached_value), as is an `=value` that
    starts with `--`. An unknown flag is refused rather than stepped over:
    its arity is unknown, so nothing after it can be placed. Everything after
    `--` is positional.
    """
    flags: list[tuple[str, str | None]] = []
    words: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == END_OF_FLAGS:
            words.extend(argv[index:])
            break
        if token.startswith("-") and len(token) > 1:
            name, separator, value = token.partition("=")
            if name in GCLOUD_IDENTITY_FLAGS or name in GCLOUD_ORG_SCOPE_FLAGS:
                # Each takes a value, so `--billing-account help billing ...`
                # is the flag and its value, and `help` is not a word. (The
                # value isn't checked for a leading `-`: the flag is refused
                # whatever it holds, and its own message says why.)
                if not separator:
                    value = argv[index] if index < len(argv) else ""
                    index += 1
                flags.append((name, value))
                continue
            if name in command_policy._GCLOUD_BOOLEAN_FLAGS:
                flags.append((name, _attached_value(name, value) if separator else None))
                continue
            if name in command_policy._GCLOUD_FLAGS_WITH_VALUE:
                if separator:
                    value = _attached_value(name, value)
                else:
                    value = _detached_value(argv, index, name)
                    index += 1
                flags.append((name, value))
                continue
            raise ConfinementRefusal(
                f"{name} is not a flag the broker can read for a profile task, so the command can't be confined"
            )
        words.append(token)
    return flags, words


def _gcloud_surface_words(words: list[str]) -> list[str]:
    start = 0
    while start < len(words) and words[start] in GCLOUD_RELEASE_TRACKS:
        start += 1
    return words[start:]


def _gcloud_is_local(flags: list[tuple[str, str | None]], words: list[str]) -> bool:
    if any(name in GCLOUD_LOCAL_FLAGS and value in TRUE_VALUES for name, value in flags):
        return True
    surface = _gcloud_surface_words(words)
    return bool(surface) and surface[0] in GCLOUD_LOCAL_WORDS


def _is_get_credentials_command(words: list[str]) -> bool:
    return _gcloud_surface_words(words)[:3] == ["container", "clusters", "get-credentials"]


def _gcloud_flag_token(name: str, value: str | None) -> str:
    return _flag_token(GCLOUD_LONG_NAMES.get(name, name), value)


def _gcloud_confined_command(words: list[str]) -> tuple[tuple[str, ...], str] | None:
    """The longest GCLOUD_CONFINED_COMMANDS key the words start with, and its mode."""
    for length in range(min(len(words), _LONGEST_CONFINED_COMMAND), 0, -1):
        mode = GCLOUD_CONFINED_COMMANDS.get(tuple(words[:length]))
        if mode is not None:
            return tuple(words[:length]), mode
    return None


def _is_target_project(value: str, target: ClusterTarget) -> bool:
    return value in (target.project, f"projects/{target.project}")


def _check_embedded(value: str, target: ClusterTarget, where: str) -> None:
    for project in _EMBEDDED_PROJECT.findall(value):
        if project != target.project:
            raise ConfinementRefusal(
                f"{where} names projects/{project[:64]}; a profile task reaches only projects/{target.project}"
            )
    for location, cluster in _EMBEDDED_CLUSTER.findall(value):
        if location != target.location or cluster != target.cluster:
            raise ConfinementRefusal(
                f"{where} names another cluster; a profile task reaches only {target.context_name}"
            )


def _confine_gcloud(argv: list[str], kubeconfig_context: str | None, target: ClusterTarget | None) -> Confined:
    flags, words = _gcloud_walk(argv)
    names = [name for name, _ in flags]
    surface = _gcloud_surface_words(words)
    # Before the local-only check and the target check, so each holds with or
    # without a target and `--help` beside one changes nothing.
    for name in names:
        if name in GCLOUD_IDENTITY_FLAGS:
            raise ConfinementRefusal(
                f"{name} is refused for a profile task: the identity a call runs as is the broker's, not the caller's"
            )
        if name in GCLOUD_ORG_SCOPE_FLAGS:
            raise ConfinementRefusal(
                f"{name} names a scope above a project; a profile task reaches only its own project"
            )
    if surface[:1] == ["config"] and len(surface) > 1 and surface[1] not in GCLOUD_CONFIG_READ_VERBS:
        raise ConfinementRefusal(
            f"gcloud config {surface[1][:32]} changes the configuration a later command reads its project, "
            "account or location from, so it is refused for a profile task"
        )
    if surface[:1] and surface[0] in GCLOUD_BROKER_STATE_SURFACES:
        raise ConfinementRefusal(
            f"gcloud {surface[0]} shows the broker's own account and configuration, so it is refused for a "
            "profile task; the task's project is the one its calls carry"
        )
    # A local-only command is run canonical like the rest, and a `--` would
    # put a word where the reordering can't keep it, so it is refused. Before
    # the policy is asked: the policy would judge that reordered argv, in
    # which a word after `--` has become a flag.
    if END_OF_FLAGS in argv and _gcloud_is_local(flags, words):
        raise ConfinementRefusal("a local-only gcloud command for a profile task takes no --")
    isolated_argv = [argv[0], *words, *(_gcloud_flag_token(name, value) for name, value in flags)]
    local = _is_local_only("gcloud", flags, words, isolated_argv)
    get_credentials = _is_get_credentials_command(words)
    if target is None:
        if not local:
            raise ConfinementRefusal(
                "this profile task has no target project (no valid task credential), so only local-only "
                "commands run, the ones the read-only policy admits: gcloud version, and --help on a "
                "command it admits"
            )
        if kubeconfig_context:
            raise ConfinementRefusal("this profile task has no target cluster, so a command may not name one")
    elif kubeconfig_context is not None and parse_gke_context(kubeconfig_context.strip()) != target:
        raise ConfinementRefusal(
            f"the kubeconfig's context must be this task's cluster, {target.context_name}"
        )
    if local:
        # Isolated, with a target too: a local-only command reaches nothing,
        # so it is given no credential path at all -- no kubeconfig, and no
        # get-credentials handling, which would describe whatever cluster
        # the argv names. So `get-credentials <cluster> --help` is an
        # isolated help read, get_credentials cleared: the executor refuses
        # a structure that is isolated and get-credentials both. A `--` was
        # refused above.
        return Confined(
            argv=isolated_argv, kubeconfig_context=None, isolated=True, executable="gcloud", words=tuple(words),
            flags=tuple(flags), target=target, get_credentials=False,
        )

    if surface and surface[0] in GCLOUD_PROJECTLESS_SURFACES:
        raise ConfinementRefusal(
            f"gcloud {surface[0]} reads outside any one project, and a profile task reaches only project {target.project}"
        )
    if tuple(surface[:2]) in GCLOUD_PROJECTLESS_COMMANDS:
        raise ConfinementRefusal(
            f"gcloud {' '.join(surface[:2])} reads outside any one project, and a profile task reaches only "
            f"project {target.project}"
        )
    confined_command = _gcloud_confined_command(words)
    if confined_command is None:
        raise ConfinementRefusal(
            "this gcloud command is not one the broker can confine to a profile task's project "
            f"(GCLOUD_CONFINED_COMMANDS); a profile task reaches only project {target.project}"
        )
    command, mode = confined_command
    positionals = words[len(command):]

    projects = [value for name, value in flags if name == GCLOUD_PROJECT_FLAG]
    for value in projects:
        if value != target.project:
            raise ConfinementRefusal(f"--project must be this task's project, {target.project}")
    for value in (value for name, value in flags if name == GCLOUD_BILLING_PROJECT_FLAG):
        if value != target.project:
            raise ConfinementRefusal(f"--billing-project must be this task's project, {target.project}")
    for name, value in flags:
        if name in GCLOUD_LOCATION_FLAGS and value != target.location:
            raise ConfinementRefusal(f"{name} must be this task's location, {target.location}")
        if name == GCLOUD_CLUSTER_FLAG and value != target.cluster:
            raise ConfinementRefusal(f"--cluster must be this task's cluster, {target.cluster}")
    scopes = [value for name, value in flags if name == GCLOUD_SCOPE_FLAG]
    for value in scopes:
        if value != f"projects/{target.project}":
            raise ConfinementRefusal(f"--scope must be this task's project, projects/{target.project}")
    if mode == NAMES_BY_SCOPE and not scopes:
        raise ConfinementRefusal(
            f"gcloud {' '.join(command)} needs --scope=projects/{target.project} for a profile task"
        )
    if mode == NAMES_BY_POSITIONAL:
        for value in positionals:
            if not _is_target_project(value, target):
                raise ConfinementRefusal(
                    f"gcloud {' '.join(command)} names another project; a profile task reaches only {target.project}"
                )
    if mode == NAMES_BY_METRICS_SCOPE:
        for value in positionals:
            scope = _METRICS_SCOPE_NAME.match(value)
            if (scope.group(1) if scope else value) != target.project:
                raise ConfinementRefusal(
                    f"the metrics scope must be this task's project, {target.project}"
                )
    if mode == NAMES_BY_IMAGE:
        for value in positionals:
            image = _IMAGE_PATH.match(value)
            if image is None or image.group(1) != target.project:
                raise ConfinementRefusal(
                    "the image must be a <location>-docker.pkg.dev or gcr.io path in this task's project, "
                    f"{target.project}"
                )
    if surface[:2] == ["container", "clusters"] and len(surface) > 2:
        verb, rest = surface[2], surface[3:]
        if verb == "get-credentials" and len(rest) != 1:
            # The parsed target must be the token's, and there is no "run as
            # given" fallback: a cluster gcloud would default is one this side
            # can't see. (A missing location is the target's, injected below.)
            raise ConfinementRefusal(
                "get-credentials for a profile task must name the cluster: "
                f"gcloud container clusters get-credentials {target.cluster}"
            )
        if verb != "list" and rest and rest[0] != target.cluster:
            _check_embedded(rest[0], target, "the cluster argument")
            if not _EMBEDDED_CLUSTER.search(rest[0]):
                raise ConfinementRefusal(f"the cluster must be this task's cluster, {target.cluster}")
    for word in words:
        _check_embedded(word, target, "an argument")
    for name, value in flags:
        if value:
            _check_embedded(value, target, name)

    # Unconditional: every credentialed call names the target's project,
    # never the broker's default project (the bootstrap set gcloud's to the
    # host cluster's) or whatever a stored property says. The caller's own
    # --project, checked equal above, is dropped for the one written here.
    project_flags = [f"{GCLOUD_PROJECT_FLAG}={target.project}"]
    # The same for a GKE call's location and a node-pool call's cluster,
    # which gcloud otherwise takes from compute/region, compute/zone and
    # container/cluster.
    command_flags: list[str] = []
    if surface[:1] == ["container"] and surface[1:2] and surface[1] in GCLOUD_CONTAINER_LOCATED_GROUPS:
        if not any(name in GCLOUD_LOCATION_FLAGS for name in names):
            command_flags.append(f"--location={target.location}")
        if surface[1] in GCLOUD_CONTAINER_CLUSTER_GROUPS and GCLOUD_CLUSTER_FLAG not in names:
            command_flags.append(f"{GCLOUD_CLUSTER_FLAG}={target.cluster}")
    # The canonical argv: the command words, contiguous, then every flag as
    # one token, then the positionals (after a `--` if the caller wrote
    # one). A flag between the command words (`container clusters
    # --location=x get-credentials`) is legal gcloud, but a matcher that
    # wants the words contiguous (the executor's get-credentials check, the
    # shim's) would miss it; written this way, every reader sees the command
    # this module confined.
    kept = [_gcloud_flag_token(name, value) for name, value in flags if name != GCLOUD_PROJECT_FLAG]
    separator = [END_OF_FLAGS] if END_OF_FLAGS in argv else []
    confined = [argv[0], *command, *project_flags, *kept, *command_flags, *separator, *positionals]
    return Confined(
        argv=confined, kubeconfig_context=None, isolated=False, executable="gcloud", words=tuple(words),
        flags=tuple(flags), target=target, get_credentials=get_credentials,
    )


def confine(argv: list[str], kubeconfig_context: str | None, target: ClusterTarget | None) -> Confined:
    """Hold a profile caller's argv to ``target``, or raise ConfinementRefusal.

    ``target`` is None when the caller presented no valid task credential;
    then only local-only commands pass. ``argv[0]`` is kubectl or gcloud: the
    profile role's executables (ROLE_EXECUTABLES) are checked before this.
    """
    if not argv:
        raise ConfinementRefusal("an empty command")
    if argv[0] == "kubectl":
        return _confine_kubectl(argv, kubeconfig_context, target)
    if argv[0] == "gcloud":
        return _confine_gcloud(argv, kubeconfig_context, target)
    raise ConfinementRefusal(f"{argv[0][:32]} is not an executable a profile task may run")
